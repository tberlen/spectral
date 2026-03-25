"""
C-You Spectral Collector

Receives UDP spectral streams from UniFi AP listeners,
tags by AP/office, computes occupancy intensity, stores in TimescaleDB.
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import asyncpg
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('collector')

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://cyou:REDACTED_DB_PASS@localhost:5433/cyou')
UDP_PORT = int(os.environ.get('UDP_PORT', '8766'))

# Frequency band classification
def classify_radio(freq):
    if freq < 3000:
        return '2.4ghz'
    elif freq < 5900:
        return '5ghz'
    else:
        return '6ghz'


class OccupancyDetector:
    """Detects occupancy from spectral data per AP.

    Maintains a rolling baseline of spectral energy and detects
    deviations that indicate human presence.
    """

    def __init__(self):
        # Per-AP state: {ap_id: {radio: {...}}}
        self.state = defaultdict(lambda: defaultdict(lambda: {
            'baseline_bins': None,
            'baseline_count': 0,
            'baseline_energy': 0.0,
            'recent_energy': [],
            'intensity': 0.0,
            'last_update': 0,
        }))

    def update(self, ap_id, radio, sample):
        """Process a spectral sample and return occupancy intensity (0.0-1.0)."""
        s = self.state[ap_id][radio]
        now = time.time()

        # Compute total energy from bins
        bins = sample.get('b', [])
        energy = sum(v for _, v in bins) if bins else 0
        nonzero = sample.get('nz', len(bins))

        # Update rolling energy window (last 30 seconds)
        s['recent_energy'].append((now, energy, nonzero))
        s['recent_energy'] = [(t, e, n) for t, e, n in s['recent_energy'] if now - t < 30]

        # Build baseline from first 60 samples (assumed quiet period)
        if s['baseline_count'] < 60:
            s['baseline_energy'] = (
                (s['baseline_energy'] * s['baseline_count'] + energy) /
                (s['baseline_count'] + 1)
            )
            s['baseline_count'] += 1
            s['intensity'] = 0.0
            return 0.0

        # Compute deviation from baseline
        if s['baseline_energy'] > 0:
            avg_recent = np.mean([e for _, e, _ in s['recent_energy'][-10:]])
            deviation = (avg_recent - s['baseline_energy']) / max(s['baseline_energy'], 1)
            avg_nonzero = np.mean([n for _, _, n in s['recent_energy'][-10:]])

            # Combine energy deviation + bin count change
            intensity = min(1.0, max(0.0, deviation * 0.5 + (avg_nonzero / 500) * 0.5))
        else:
            intensity = 0.0

        s['intensity'] = intensity
        s['last_update'] = now
        return intensity

    def get_intensity(self, ap_id, radio=None):
        if radio:
            return self.state[ap_id][radio]['intensity']
        # Average across radios
        radios = self.state[ap_id]
        if not radios:
            return 0.0
        return np.mean([r['intensity'] for r in radios.values()])


class SpectralCollector:
    def __init__(self):
        self.db = None
        self.detector = OccupancyDetector()
        self.ap_cache = {}  # ip -> ap_id mapping
        self.sample_count = 0
        self.store_interval = 10  # Store 1 in N samples to DB

    async def start(self):
        log.info('Connecting to database...')
        self.db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
        log.info('Database connected')

        await self._load_ap_cache()

        # Start UDP listener
        loop = asyncio.get_event_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: SpectralProtocol(self),
            local_addr=('0.0.0.0', UDP_PORT)
        )
        log.info(f'Listening for spectral data on UDP port {UDP_PORT}')

        # Periodic tasks
        asyncio.create_task(self._occupancy_writer())
        asyncio.create_task(self._ap_cache_refresher())

        # Keep running
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            transport.close()

    async def _load_ap_cache(self):
        rows = await self.db.fetch('SELECT id, ip_address FROM access_points')
        self.ap_cache = {str(row['ip_address']): row['id'] for row in rows}
        log.info(f'Loaded {len(self.ap_cache)} APs into cache')

    async def _ap_cache_refresher(self):
        while True:
            await asyncio.sleep(30)
            await self._load_ap_cache()

    async def _occupancy_writer(self):
        """Periodically write occupancy state to DB."""
        while True:
            await asyncio.sleep(10)
            try:
                for ap_id in list(self.detector.state.keys()):
                    for radio, state in self.detector.state[ap_id].items():
                        if state['last_update'] > 0:
                            await self.db.execute(
                                '''INSERT INTO ap_occupancy (time, ap_id, intensity, radio)
                                   VALUES (NOW(), $1, $2, $3)''',
                                ap_id, state['intensity'], radio
                            )
            except Exception as e:
                log.error(f'Occupancy write error: {e}')

    async def handle_sample(self, data, addr):
        """Process a spectral sample from an AP."""
        try:
            sample = json.loads(data)
        except json.JSONDecodeError:
            return

        src_ip = addr[0]
        ap_id = self.ap_cache.get(src_ip)
        if ap_id is None:
            # Unknown AP - log periodically
            if self.sample_count % 100 == 0:
                log.warning(f'Unknown AP: {src_ip}')
            return

        freq = sample.get('f', 0)
        radio = classify_radio(freq)

        # Update occupancy detector
        self.detector.update(ap_id, radio, sample)

        # Store subset to DB
        self.sample_count += 1
        if self.sample_count % self.store_interval == 0:
            try:
                bins = sample.get('b', [])
                nonzero = sample.get('nz', len(bins))
                max_val = sample.get('mv', 0)
                max_idx = sample.get('mi', 0)

                await self.db.execute(
                    '''INSERT INTO spectral_readings
                       (time, ap_id, freq, noise_floor, rssi, max_scale, max_mag,
                        tsf, nonzero_bins, max_bin_val, max_bin_idx, radio)
                       VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)''',
                    ap_id, freq,
                    sample.get('n', 0), sample.get('r', 0),
                    0, 0,
                    sample.get('t', 0),
                    nonzero, max_val, max_idx,
                    radio
                )
            except Exception as e:
                if self.sample_count % 100 == 0:
                    log.error(f'DB write error: {e}')

        # Update last seen
        if self.sample_count % 100 == 0:
            try:
                await self.db.execute(
                    '''UPDATE access_points SET listener_last_seen = NOW(),
                       listener_status = 'deployed'
                       WHERE id = $1''',
                    ap_id
                )
            except Exception:
                pass

            log.info(
                f'AP {ap_id} ({src_ip}): {self.sample_count} samples, '
                f'intensity={self.detector.get_intensity(ap_id):.2f}'
            )


class SpectralProtocol(asyncio.DatagramProtocol):
    def __init__(self, collector):
        self.collector = collector

    def datagram_received(self, data, addr):
        asyncio.ensure_future(self.collector.handle_sample(data, addr))


async def main():
    collector = SpectralCollector()
    await collector.start()


if __name__ == '__main__':
    asyncio.run(main())
