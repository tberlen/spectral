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

### Network Security & AP Locking

**What's exposed to the network:**

| Port | Service | Exposed | Contains sensitive data? |
|------|---------|---------|------------------------|
| 8080 | Dashboard | Yes (browser UI) | Occupancy data only (no PII) |
| 8766/udp | Collector | Yes (APs send data here) | Raw spectral FFT bins (no PII) |
| 8081 | AP Manager | No (Docker internal) | Client MACs, hostnames, 1x identities |
| 5432 | Database | No (Docker internal) | Everything |
| 8767 | Collector API | No (Docker internal) | Occupancy state |

**What's on each AP (port 8080):**

Each AP runs the spectral listener with a built-in HTTP server:
- `/health` — Always open. Returns `{"status":"ok"}`. Used for basic alive checks.
- `/status` — Returns hostname, AP IP, server IP, uptime, sample count. Reveals infrastructure topology.

**The lock icon** controls `/status` authentication per AP:
- **Unlocked** — `/status` is open to anyone on the network
- **Locked** — `/status` requires a Bearer token. Returns `{"error":"unauthorized"}` without it.

When you lock an AP, the AP manager:
1. Generates a random 32-character token
2. Redeploys the listener with `API_TOKEN=<token>` set
3. Stores the token in the database
4. Uses the stored token for its own health checks

The token never appears in the dashboard UI. Each AP gets its own unique token.

**"Lock All / Unlock All"** toggles all APs in an office at once. Skips APs already in the desired state. Shows `X/Y` count of locked APs.

**Client data flow (Who's Here):**

```
AP Manager --SSH--> AP (mca-dump) --> DB --> Dashboard API --> Browser
```

Client data (MACs, hostnames, 802.1X identities) travels only over SSH from the APs, stored in the DB, and served by the dashboard. The AP manager and DB are Docker-internal only, so client data is never directly exposed to the network. The dashboard serves it on `:8080/api/clients/*` but only to browsers accessing the UI.

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
  - Repo: http://REDACTED_INTERNAL_IP/root/spectral
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
