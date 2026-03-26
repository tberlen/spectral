"""
Spectral Collector

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
    deviations that indicate human presence. Supports manual baseline
    capture and per-office sensitivity settings.
    """

    def __init__(self):
        # Per-AP state: {ap_id: {radio: {...}}}
        self.state = defaultdict(lambda: defaultdict(lambda: {
            'baseline_energy': 0.0,
            'baseline_nonzero': 0.0,
            'baseline_locked': False,
            'baseline_samples': 0,
            'baseline_time': None,
            'recent_energy': [],
            'intensity': 0.0,
            'last_update': 0,
        }))
        self.sensitivity = {}  # office_id -> float (0.1 = low, 1.0 = default, 3.0 = high)
        self.ap_office_map = {}  # ap_id -> office_id
        self.capturing_baseline = {}  # ap_id -> {'target': N, 'start_time': t}
        self._pending_saves = []  # ap_ids with baselines to persist

    def set_sensitivity(self, office_id, value):
        """Set sensitivity for an office. Higher = more sensitive."""
        self.sensitivity[office_id] = max(0.1, min(5.0, value))

    def get_sensitivity(self, office_id):
        return self.sensitivity.get(office_id, 1.0)

    def start_baseline(self, ap_id, duration_seconds=60):
        """Start capturing baseline for an AP. Resets existing baseline.
        Uses time-based capture - collects for duration_seconds regardless of sample count."""
        end_time = time.time() + duration_seconds
        for radio in list(self.state[ap_id].keys()):
            s = self.state[ap_id][radio]
            s['baseline_energy'] = 0.0
            s['baseline_nonzero'] = 0.0
            s['baseline_locked'] = False
            s['baseline_samples'] = 0
            s['baseline_time'] = None
        self.capturing_baseline[ap_id] = {
            'end_time': end_time,
            'duration': duration_seconds,
            'start_time': time.time()
        }

    def get_baseline_status(self, ap_id):
        """Get baseline capture status for an AP."""
        if ap_id in self.capturing_baseline:
            info = self.capturing_baseline[ap_id]
            now = time.time()
            elapsed = int(now - info['start_time'])
            remaining = max(0, int(info['end_time'] - now))
            duration = info['duration']
            for radio, s in self.state[ap_id].items():
                return {
                    'status': 'capturing',
                    'samples': s['baseline_samples'],
                    'elapsed': elapsed,
                    'remaining': remaining,
                    'duration': duration,
                }
            return {'status': 'capturing', 'samples': 0, 'elapsed': elapsed, 'remaining': remaining, 'duration': duration}

        for radio, s in self.state[ap_id].items():
            if s['baseline_locked']:
                return {
                    'status': 'locked',
                    'energy': s['baseline_energy'],
                    'nonzero': s['baseline_nonzero'],
                    'samples': s['baseline_samples'],
                    'time': s['baseline_time'],
                }
        return {'status': 'none'}

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

        # Baseline capture mode (time-based)
        if ap_id in self.capturing_baseline and not s['baseline_locked']:
            count = s['baseline_samples']
            s['baseline_energy'] = (s['baseline_energy'] * count + energy) / (count + 1)
            s['baseline_nonzero'] = (s['baseline_nonzero'] * count + nonzero) / (count + 1)
            s['baseline_samples'] = count + 1

            if now >= self.capturing_baseline[ap_id]['end_time']:
                s['baseline_locked'] = True
                s['baseline_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
                all_done = all(
                    self.state[ap_id][r]['baseline_locked']
                    for r in self.state[ap_id]
                )
                if all_done:
                    del self.capturing_baseline[ap_id]
                    self._pending_saves.append(ap_id)
            s['intensity'] = 0.0
            return 0.0

        # Auto-baseline if no manual baseline set (first 60 samples)
        if not s['baseline_locked'] and s['baseline_samples'] < 60:
            count = s['baseline_samples']
            s['baseline_energy'] = (s['baseline_energy'] * count + energy) / (count + 1)
            s['baseline_nonzero'] = (s['baseline_nonzero'] * count + nonzero) / (count + 1)
            s['baseline_samples'] = count + 1
            if s['baseline_samples'] >= 60:
                s['baseline_locked'] = True
                s['baseline_time'] = time.strftime('%Y-%m-%d %H:%M:%S') + ' (auto)'
            s['intensity'] = 0.0
            return 0.0

        # Compute deviation from baseline
        if s['baseline_energy'] > 0:
            avg_recent = np.mean([e for _, e, _ in s['recent_energy'][-10:]])
            deviation = (avg_recent - s['baseline_energy']) / max(s['baseline_energy'], 1)
            avg_nonzero = np.mean([n for _, _, n in s['recent_energy'][-10:]])
            nonzero_deviation = (avg_nonzero - s['baseline_nonzero']) / max(s['baseline_nonzero'], 1)

            # Apply sensitivity multiplier
            office_id = self.ap_office_map.get(ap_id)
            sens = self.sensitivity.get(office_id, 1.0) if office_id else 1.0

            raw = (deviation * 0.5 + nonzero_deviation * 0.5) * sens
            intensity = min(1.0, max(0.0, raw))
        else:
            intensity = 0.0

        s['intensity'] = intensity
        s['last_update'] = now
        return intensity

    def get_intensity(self, ap_id, radio=None):
        if radio:
            return self.state[ap_id][radio]['intensity']
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
        self.unknown_count = 0
        self.store_interval = 10  # Store 1 in N samples to DB
        self.start_time = time.time()
        self.last_sample_time = 0
        self.per_ap_counts = {}  # ip -> count
        self.per_ap_last_seen = {}  # ip -> timestamp

    async def start(self):
        log.info('Connecting to database...')
        self.db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
        log.info('Database connected')

        await self._load_ap_cache()
        await self._load_baselines()

        # Start UDP listener
        loop = asyncio.get_event_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: SpectralProtocol(self),
            local_addr=('0.0.0.0', UDP_PORT)
        )
        log.info(f'Listening for spectral data on UDP port {UDP_PORT}')

        # Start health HTTP server
        from aiohttp import web
        app = web.Application()
        app.router.add_get('/health', self._handle_health)
        app.router.add_post('/baseline/{ap_id}', self._handle_baseline)
        app.router.add_get('/baseline/{ap_id}', self._handle_baseline_status)
        app.router.add_post('/sensitivity/{office_id}', self._handle_sensitivity)
        app.router.add_get('/sensitivity/{office_id}', self._handle_get_sensitivity)
        app.router.add_post('/schedule/{office_id}', self._handle_set_schedule)
        app.router.add_get('/schedule/{office_id}', self._handle_get_schedule)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8767)
        await site.start()
        log.info('Health API on port 8767')

        # Periodic tasks
        asyncio.create_task(self._occupancy_writer())
        asyncio.create_task(self._ap_cache_refresher())
        asyncio.create_task(self._baseline_saver())
        asyncio.create_task(self._baseline_scheduler())

        # Keep running
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            transport.close()

    async def _handle_health(self, request):
        """Health endpoint showing data flow status."""
        import json as _json
        now = time.time()
        uptime = int(now - self.start_time)
        last_ago = int(now - self.last_sample_time) if self.last_sample_time else None
        receiving = last_ago is not None and last_ago < 10

        ap_details = {}
        for ip, count in self.per_ap_counts.items():
            ap_id = self.ap_cache.get(ip)
            last_seen = self.per_ap_last_seen.get(ip)
            ap_details[ip] = {
                'ap_id': ap_id,
                'registered': ap_id is not None,
                'samples': count,
                'intensity': float(self.detector.get_intensity(ap_id)) if ap_id else 0,
                'last_seen_seconds_ago': int(now - last_seen) if last_seen else None,
            }

        body = {
            'status': 'receiving' if receiving else 'no_data',
            'uptime_seconds': uptime,
            'total_samples': self.sample_count,
            'unknown_samples': self.unknown_count,
            'registered_aps': len(self.ap_cache),
            'last_sample_seconds_ago': last_ago,
            'per_ap': ap_details
        }
        from aiohttp import web
        return web.json_response(body)

    async def _handle_baseline(self, request):
        """Start baseline capture for an AP."""
        from aiohttp import web
        ap_id = int(request.match_info['ap_id'])
        data = await request.json() if request.can_read_body else {}
        duration = int(data.get('duration', 60))
        self.detector.start_baseline(ap_id, duration_seconds=duration)
        return web.json_response({'status': 'capturing', 'ap_id': ap_id, 'duration': duration})

    async def _handle_baseline_status(self, request):
        """Get baseline status for an AP."""
        from aiohttp import web
        ap_id = int(request.match_info['ap_id'])
        status = self.detector.get_baseline_status(ap_id)
        return web.json_response(status)

    async def _handle_sensitivity(self, request):
        """Set sensitivity for an office."""
        from aiohttp import web
        office_id = int(request.match_info['office_id'])
        data = await request.json()
        value = float(data.get('sensitivity', 1.0))
        self.detector.set_sensitivity(office_id, value)
        return web.json_response({'office_id': office_id, 'sensitivity': self.detector.get_sensitivity(office_id)})

    async def _handle_get_sensitivity(self, request):
        """Get sensitivity for an office."""
        from aiohttp import web
        office_id = int(request.match_info['office_id'])
        return web.json_response({'office_id': office_id, 'sensitivity': self.detector.get_sensitivity(office_id)})

    async def _handle_set_schedule(self, request):
        """Set baseline schedule for an office."""
        from aiohttp import web
        office_id = int(request.match_info['office_id'])
        data = await request.json()
        cron_time = data.get('time', '02:00')
        duration = int(data.get('duration', 300))
        enabled = data.get('enabled', True)
        await self.db.execute(
            '''INSERT INTO baseline_schedules (office_id, cron_time, duration_seconds, enabled)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (office_id) DO UPDATE SET
               cron_time = $2, duration_seconds = $3, enabled = $4''',
            office_id, cron_time, duration, enabled
        )
        return web.json_response({'office_id': office_id, 'time': cron_time, 'duration': duration, 'enabled': enabled})

    async def _handle_get_schedule(self, request):
        """Get baseline schedule for an office."""
        from aiohttp import web
        office_id = int(request.match_info['office_id'])
        row = await self.db.fetchrow(
            'SELECT cron_time, duration_seconds, enabled, last_run FROM baseline_schedules WHERE office_id = $1',
            office_id
        )
        if row:
            return web.json_response({
                'time': str(row['cron_time']),
                'duration': row['duration_seconds'],
                'enabled': row['enabled'],
                'last_run': row['last_run'].isoformat() if row['last_run'] else None
            })
        return web.json_response({'time': '02:00', 'duration': 300, 'enabled': False, 'last_run': None})

    async def _load_baselines(self):
        """Load saved baselines from DB on startup."""
        rows = await self.db.fetch('SELECT ap_id, radio, baseline_energy, baseline_nonzero, samples, captured_at FROM baselines')
        count = 0
        for row in rows:
            s = self.detector.state[row['ap_id']][row['radio']]
            s['baseline_energy'] = row['baseline_energy']
            s['baseline_nonzero'] = row['baseline_nonzero']
            s['baseline_samples'] = row['samples']
            s['baseline_locked'] = True
            s['baseline_time'] = row['captured_at'].strftime('%Y-%m-%d %H:%M:%S')
            count += 1
        log.info(f'Loaded {count} saved baselines')

    async def _baseline_saver(self):
        """Persist completed baselines to DB."""
        while True:
            await asyncio.sleep(5)
            while self.detector._pending_saves:
                ap_id = self.detector._pending_saves.pop(0)
                try:
                    for radio, s in self.detector.state[ap_id].items():
                        if s['baseline_locked']:
                            await self.db.execute(
                                '''INSERT INTO baselines (ap_id, radio, baseline_energy, baseline_nonzero, samples)
                                   VALUES ($1, $2, $3, $4, $5)
                                   ON CONFLICT (ap_id, radio) DO UPDATE SET
                                   baseline_energy = $3, baseline_nonzero = $4, samples = $5, captured_at = NOW()''',
                                ap_id, radio, s['baseline_energy'], s['baseline_nonzero'], s['baseline_samples']
                            )
                    log.info(f'Saved baseline for AP {ap_id}')
                except Exception as e:
                    log.error(f'Failed to save baseline for AP {ap_id}: {e}')

    async def _baseline_scheduler(self):
        """Run scheduled baseline captures."""
        from datetime import datetime
        while True:
            await asyncio.sleep(30)
            try:
                now = datetime.now()
                schedules = await self.db.fetch(
                    'SELECT office_id, cron_time, duration_seconds, last_run FROM baseline_schedules WHERE enabled = TRUE'
                )
                for sched in schedules:
                    cron_time = sched['cron_time']
                    # Check if current time matches schedule (within 1 minute)
                    if now.hour == cron_time.hour and now.minute == cron_time.minute:
                        last_run = sched['last_run']
                        if last_run and (now - last_run.replace(tzinfo=None)).total_seconds() < 3600:
                            continue  # Already ran recently

                        office_id = sched['office_id']
                        duration = sched['duration_seconds']
                        log.info(f'Scheduled baseline for office {office_id} ({duration}s)')

                        # Get all APs for this office
                        aps = await self.db.fetch(
                            'SELECT id FROM access_points WHERE office_id = $1', office_id
                        )
                        for ap in aps:
                            self.detector.start_baseline(ap['id'], duration_seconds=duration)

                        await self.db.execute(
                            'UPDATE baseline_schedules SET last_run = NOW() WHERE office_id = $1', office_id
                        )
            except Exception as e:
                log.error(f'Scheduler error: {e}')

    async def _load_ap_cache(self):
        rows = await self.db.fetch('SELECT id, ip_address, office_id FROM access_points')
        self.ap_cache = {str(row['ip_address']): row['id'] for row in rows}
        for row in rows:
            if row['office_id']:
                self.detector.ap_office_map[row['id']] = row['office_id']
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

        # Use AP's self-reported IP if available, fall back to UDP source
        ap_ip = sample.get('ip', addr[0])
        now = time.time()
        self.last_sample_time = now
        self.per_ap_counts[ap_ip] = self.per_ap_counts.get(ap_ip, 0) + 1
        self.per_ap_last_seen[ap_ip] = now

        ap_id = self.ap_cache.get(ap_ip)
        if ap_id is None:
            self.unknown_count += 1
            if self.unknown_count % 100 == 0:
                log.warning(f'Unknown AP: {ap_ip} (hostname={sample.get("h","?")}, {self.unknown_count} total unknown)')
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
                f'AP {ap_id} ({ap_ip}): {self.sample_count} samples, '
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
