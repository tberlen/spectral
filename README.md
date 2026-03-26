# Spectral

**WiFi Spectral Occupancy Monitoring for Multi-Office Environments**

Spectral uses the built-in spectral scan hardware in UniFi access points to detect physical human presence in offices. No cameras, no badges, no extra hardware — just the APs you already have.

## How It Works

### The Science

Every WiFi radio chip has a **spectral scan** engine that captures the RF energy across the frequency band as a **Fast Fourier Transform (FFT)**. This produces a detailed picture of all radio energy in the environment — not just WiFi packets, but the complete electromagnetic spectrum in the 2.4GHz, 5GHz, and 6GHz bands.

**Human bodies absorb and reflect radio waves.** When people are present in a room, the spectral signature changes:

- **FFT bin energy levels shift** as bodies absorb RF energy
- **The number of active frequency bins changes** as reflections create new multipath patterns
- **Noise floor variations** increase with human movement

Spectral captures these FFT snapshots at ~85 samples/second per AP and compares them against a **baseline** captured when the office is empty. The deviation from baseline = occupancy intensity.

This is fundamentally different from counting WiFi clients:
- **Spectral detects people even without devices** (visitors, people with phones off)
- **No MAC addresses or personal data needed** for basic occupancy
- **Works through walls and partitions** — RF energy propagates everywhere
- **Can't be defeated by MAC randomization**

### Data Capture Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│ UniFi AP                                                        │
│                                                                 │
│  qca_spectral ──netlink──> spectral_listener ──UDP──> Collector │
│  (kernel module)    protocol 17    (C binary)                   │
│                                                                 │
│  mca-dump ──SSH poll──> AP Manager ──> Database                 │
│  (client data)          (every 30s)                             │
└─────────────────────────────────────────────────────────────────┘
```

1. **Qualcomm's `qca_spectral` kernel module** triggers spectral scans on the radio hardware
2. **`spectraltool`** configures scan parameters (count, interval, FFT size)
3. **Spectral data arrives via netlink protocol 17** as binary messages with a `0xdeadbeef` signature
4. **Our `spectral_listener` binary** (C, statically compiled for ARMv7) parses the netlink messages, extracts frequency, noise floor, RSSI, and all FFT bins
5. **Streams compact JSON via UDP** to the central collector, including the AP's hostname, IP, and radio MAC for identification
6. **Collector compares against baseline** and computes occupancy intensity (0.0 = empty, 1.0 = crowded)

### Spectral Message Format

Each netlink message is ~3,012 bytes containing:

| Offset | Field | Description |
|--------|-------|-------------|
| 0x00 | Signature | `0xdeadbeef` (Qualcomm spectral) |
| 0x08 | Frequency | Center frequency in MHz |
| 0x28 | TSF | Hardware timestamp (microseconds) |
| 0x50 | Noise Floor | Background noise level (dBm) |
| 0x51 | RSSI | Received signal strength |
| 0x184 | MAC Address | Radio MAC (identifies the AP/band) |
| 0x190+ | FFT Bins | Up to 1,024 frequency bins |

### What We Also Capture

In addition to spectral data, the AP Manager periodically polls **`mca-dump`** from each AP via SSH. This is the same JSON payload the AP sends to the UniFi Controller and includes:

- **Connected client list** with MAC, hostname, IP, RSSI, signal quality
- **802.1X identity** (RADIUS username) for WPA-Enterprise networks
- **Per-client TX/RX rates**, uptime, and roaming state

This enables the **"Who's Here"** feature — seeing actual people/device names, first-in/last-out tracking, and search.

## Features

### Occupancy Detection
- **Heatmap overlay** on office floor plans showing presence intensity per AP zone
- **Occupied/Vacant** status per office on the global view
- **Configurable sensitivity** per office (slider from 0.1x to 5.0x)
- **Baseline calibration** — capture empty-office signature manually or on schedule
- **Baselines persist** in database and survive service restarts

### Who's Here
- **Real-time client list** with hostnames, IPs, signal strength, connected AP
- **802.1X identity** shows actual usernames for WPA-Enterprise networks
- **First In / Last Out** tracking per day (excludes static devices)
- **Search** by name, hostname, or MAC address
- **Static device detection** — auto-marks 24h+ uptime devices (TVs, printers); manual marking with labels

### AP Management
- **Subnet discovery** — scan an IP range to find all UniFi APs
- **One-click deploy** — compiles, transfers, and starts the listener on any AP
- **Health monitoring** — per-AP heartbeat with live/stale/dead indicators
- **Auto-remediation** — detects rebooted APs and redeploys
- **Latency-aware deploys** — scales timeouts for high-latency international links
- **Binary verification** — checks file size after transfer, verifies process started

### Dashboard
- **Global view** — all offices at a glance with Occupied/Vacant badges
- **Office map view** — floor plan with draggable AP markers and spectral heatmap
- **Floor plan upload** — crop tool with optional blueprint cleanup (OpenCV)
- **Per-office settings** — timezone, default SSH credentials, baseline schedule
- **Connection banner** — alerts when data feed is lost

## Validated Hardware

| Model | Architecture | Kernel | Radios | Status |
|-------|-------------|--------|--------|--------|
| U7 Pro Max | ARMv7 | 5.4.213 | 2.4GHz, 5GHz, 6GHz (tri-band) | Full support (spectral + health API) |
| U6-IW | ARMv7 | 5.4.164 | 2.4GHz, 5GHz | Spectral works; health API thread issue (fallback to SSH check) |

All UniFi APs with Qualcomm radios (`qca_spectral` + `qca_ol` kernel modules) should work. The spectral listener binary is the same across models — same architecture, same netlink protocol, same struct layout.

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  UniFi APs   │     │  UniFi APs   │     │  UniFi APs   │
│  Cambridge   │     │  Singapore   │     │  Office N    │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │ UDP                │ UDP                │ UDP
       │ (spectral)         │ (spectral)         │ (spectral)
       └────────────┬───────┴────────────────────┘
                    │
          ┌─────────▼──────────┐
          │  spectral-collector │  Receives spectral streams
          │  (Python/asyncio)  │  Occupancy detection engine
          │  Port 8766/udp     │  Baseline management
          └─────────┬──────────┘
                    │
          ┌─────────▼──────────┐
          │  spectral-db       │  TimescaleDB (PostgreSQL)
          │  Port 5433         │  Spectral data, baselines,
          │                    │  clients, offices, APs
          └─────────┬──────────┘
                    │
     ┌──────────────┼──────────────┐
     │              │              │
┌────▼─────┐  ┌────▼─────┐  ┌────▼─────┐
│ spectral │  │ spectral │  │ spectral │
│ dashboard│  │ manager  │  │ manager  │
│ Port 8080│  │ Port 8081│  │ (SSH to  │
│ (Web UI) │  │ (Deploy, │  │  APs for │
│          │  │  health) │  │  clients)│
└──────────┘  └──────────┘  └──────────┘
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Spectral Listener | C (static ARMv7 binary, ~786KB) |
| Collector | Python 3.11, asyncio, asyncpg, numpy |
| AP Manager | Python 3.11, aiohttp, sshpass |
| Dashboard | Python 3.11, aiohttp, Jinja2, OpenCV |
| Database | TimescaleDB (PostgreSQL 16) |
| Frontend | Vanilla JavaScript, HTML5 Canvas |
| Deployment | Docker Compose |
| Cross-compiler | gcc-arm-linux-gnueabi (in Docker) |

## Quick Start

```bash
# Clone
git clone http://your-gitlab/root/spectral.git
cd spectral/docker

# Start all services
docker compose up -d --build

# Open dashboard
open http://localhost:8080
```

1. Click **Manage** to add an office
2. Go to the office, click **Settings** to set default SSH credentials
3. Click **Discover APs** and enter your AP subnet (e.g. `10.68.30.0/24`)
4. Add the discovered APs, then click **Install** on each to deploy the listener
5. Run **Baseline All** when the office is empty to calibrate

## Network Requirements

| Port | Protocol | Direction | Purpose |
|------|----------|-----------|---------|
| 8766 | UDP | AP -> Server | Spectral data stream |
| 8080 | TCP | AP <- Server | Listener health API (on AP) |
| 22 | TCP | Server -> AP | SSH for deploy and client collection |
| 8080 | TCP | Browser -> Server | Dashboard |

All offices must be reachable from the server (IPsec, VPN, or direct routing).

## Privacy

- **Spectral occupancy detection requires no personal data** — it measures RF energy patterns, not individual devices
- **Client tracking (Who's Here)** uses data already on the AP — no additional monitoring infrastructure
- **All processing is local** — no data sent to external services
- **No cameras or physical sensors** required
- **Blueprint cleanup uses OpenCV locally** — images never leave the server
