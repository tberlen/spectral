# Spectral - WiFi Spectral Occupancy Monitoring Service

## Project Overview

Service-oriented occupancy monitoring system using UniFi AP spectral data. Deploys lightweight spectral listeners on UniFi APs to detect human presence via WiFi spectral analysis. Designed for multi-office corporate environments.

## Use Cases

- Know when office floors are empty or occupied (heatmap view)
- Global view of all offices at a glance (occupied/empty)
- Conference room occupancy during hosted events
- Per-AP zone-level presence detection
- Automated AP listener deployment and health monitoring

## Architecture

### Components

1. **Spectral Listener** (`spectral/`) - C binary deployed to each UniFi AP
   - Captures FFT data from qca_spectral kernel module via netlink protocol 17
   - Streams spectral JSON over UDP to collector
   - Built-in HTTP health API (/status, /health) reporting state + configured server IP
   - Cross-compiled for ARMv7 (static binary)

2. **AP Manager** (`ap-manager/`) - AP lifecycle management service
   - AP registry (IP, SSH creds, office assignment, map coordinates)
   - Watchdog loop: health check → SSH remediation → redeploy
   - Detects stale server IP from AP status and pushes updated listener
   - Handles AP reboots automatically (listener lives in /tmp, lost on reboot)

3. **Collector** (`collector/`) - Spectral data ingestion
   - Receives UDP streams from all APs
   - Tags data by AP/office
   - Filters by frequency/MAC to separate radio bands
   - Stores in TimescaleDB

4. **Dashboard** (`dashboard/`) - Web UI
   - **Office Map View** (primary): Floor plan with APs positioned, spectral heatmap overlay
   - **Global View** (secondary): All offices at a glance, occupied/empty indicators
   - Per-office pages (7 offices)

### Data Flow

```
UniFi AP (spectral_listener) --UDP--> Collector ---> TimescaleDB
                                                        |
AP Manager --SSH/HTTP--> UniFi APs            Occupancy Engine
                                                        |
                                                   REST API
                                                        |
                                                   Dashboard
```

## Hardware

### Validated UniFi AP Models

- **U7 Pro Max** - Primary target. ARMv7, Linux 5.4.213, LEDE 17.01.6
  - qca_spectral + qca_ol kernel modules (same as U6)
  - Netlink protocol 17, 0xdeadbeef signature
  - Tri-band: wifi0 (2.4GHz), wifi1 (5GHz), wifi2/wifi3 (6GHz)
  - spectraltool available at /usr/sbin/spectraltool
  - Shared netlink socket across all radios - filter by freq/MAC
  - Test AP: 10.68.30.16, user=REDACTED

- **U6-IW** - Proven in see-you barn project. Same driver stack, same binary.

### AP Limitations
- No SCP on AP - use base64 encoding to transfer files
- Minimal busybox environment (no python, no gcc)
- /tmp is writable but lost on reboot
- Binary deployed to /tmp/spectral_listener

### Build & Deploy Workflow
```bash
# Cross-compile on See You LXC (VMID 130, REDACTED_BUILD_SERVER)
scp spectral/spectral_listener.c root@REDACTED_BUILD_SERVER:/tmp/spectral_listener.c
ssh root@REDACTED_BUILD_SERVER 'arm-linux-gnueabi-gcc -O2 -Wall -static -o /tmp/spectral_listener /tmp/spectral_listener.c'

# Deploy to AP via base64 (no SCP on AP)
ssh root@REDACTED_BUILD_SERVER 'base64 /tmp/spectral_listener' > /tmp/sl.b64
sshpass -p '<password>' ssh <user>@<ap-ip> \
  'cat > /tmp/sl.b64 && base64 -d /tmp/sl.b64 > /tmp/spectral_listener && chmod +x /tmp/spectral_listener && rm /tmp/sl.b64' < /tmp/sl.b64
```

## Source Control

- **Home GitLab:** http://REDACTED_INTERNAL_IP
  - Username: root
  - API Token: REDACTED_GITLAB_TOKEN
  - Repo: http://REDACTED_INTERNAL_IP/root/c-you
  - Branch: main
- **Work GitLab:** gitlab.iqt.org (push here when ready for production)

## Infrastructure

- **Build server:** See You LXC (VMID 130, REDACTED_BUILD_SERVER) - has arm-linux-gnueabi-gcc
- **Proxmox:** REDACTED_PROXMOX_IP:8006, 3-node cluster
- **Network:** All 7 offices connected via IPsec tunnels - all APs reachable
- **Related project:** see-you (barn monitoring) at /Users/tberlen/claude/see-you

## Offices

7 offices total, each with its own floor plan and AP set. Details TBD.

## Tech Stack

- **Listener:** C (ARMv7 static binary)
- **Backend:** Python (async - aiohttp, asyncpg)
- **Database:** TimescaleDB (PostgreSQL)
- **Frontend:** Vanilla JS, HTML5 Canvas for heatmap
- **Deployment:** Docker Compose
