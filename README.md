# MRTG-CMP

Enterprise-grade network traffic monitoring, historical analysis, and reporting system for MikroTik RouterOS branch gateways.

The system polls the MikroTik RouterOS API over a dedicated TCP tunnel, records granular time-series traffic samples in an embedded SQLite database (WAL mode), and serves an authenticated web dashboard featuring custom date-range selection, RRDtool-identical graphs, and multi-format reporting exports (PNG, Excel, CSV).

---

## Key Capabilities

- **Direct RouterOS API Polling**: Queries cumulative octet counters (`rx-byte`, `tx-byte`) over TCP port `8728` (or custom tunnel port), bypassing SNMP UDP limitations.
- **Robust Delta Rate Engine**: Computes exact bits per second with counter rollover, router reboot, and rate sanity guards.
- **RRDtool Visual Fidelity**: Generates pixel-perfect telco-style graphs (stepped solid green inbound area `#00CC00`, stepped dark blue outbound line `#0000CC`, high-contrast pink dotted grid `#FFAAAA` at `zorder=3`, 3D chiseled outer bezel, 100% monospace typography, and directional arrows).
- **True RRDtool Dynamic Autoscale**: Implements standard logarithmic `nice_ceiling()` math with 5% headroom and 5 clean horizontal divisions, adapting effortlessly from idle/low traffic to 150 Mbps+ without clipping or flattening.
- **Multi-Timespan Adaptive Locators**: Dynamically adapts time ticks across all ranges: 1-minute ticks for sub-15m, 10-minute ticks for sub-2h, 2-hour ticks for 24h (MRTG Daily standard), and daily ticks for 7d (MRTG Weekly standard).
- **Point-and-Click Time Selector & Sub-Day Presets**: 100% point-and-click date-time matrix selector, hourly presets (*1 Hour*, *3 Hours*, *6 Hours*, *12 Hours*, *24 Hours*, *Today*, *Yesterday*, *7 Days*, *This Month*), single-click 24h day picker, and instant "Now" shortcut.
- **RouterOS Web Console Bridge**: Integrated web terminal emulator with live MikroTik passthrough authentication (zero router credentials stored on disk), contextual TAB autocomplete matching RouterOS v6, Up/Down arrow command history, anti-linger session security (auto-lock & tab-close kill), and SQLite audit trail.
- **Reporting & Exports**: Instant downloads of rendered PNG graphs, styled Excel (`.xlsx`) workbooks with metadata banners, and raw CSV files.
- **Clean NOC Aesthetics**: Authentic RRDtool visual styling with seamless Light and Dark Mode NOC themes (slate palette `#0F172A`, `#1E293B`, `#38BDF8`), anti-FOUC script, live collapsible recent samples table, and real-time 60-second countdown auto-refresh.
- **Session Authentication**: Protected web dashboard with PBKDF2-HMAC-SHA256 password hashing, auto-syncing admin password from `.env`, and "Remember Me" session persistence.
- **Zero External Database Overhead**: Powered by embedded SQLite with Write-Ahead Logging (WAL) for 100% portability across Windows 11 and Debian 13.

---

## Architecture Overview

```
┌──────────────────────────────────────┐
│       Enterprise Branch Router       │
│         (MikroTik RouterOS)          │
│    Interface: WAN (Main Uplink 150M) │
└──────────────────┬───────────────────┘
                   │ RouterOS API (TCP 8728)
                   ▼
┌──────────────────────────────────────┐
│       Traffic Monitor Host           │
│   ┌──────────────────────────────┐   │
│   │ Collector Daemon (300s)      │   │
│   └──────────────┬───────────────┘   │
│                  ▼                   │
│   ┌──────────────────────────────┐   │
│   │ SQLite Database (traffic.db) │   │
│   └──────────────┬───────────────┘   │
│                  ▼                   │
│   ┌──────────────────────────────┐   │
│   │ FastAPI Web Dashboard        │   │
│   │ Matplotlib RRDtool Engine    │   │
│   │ Excel & CSV Exporters        │   │
│   └──────────────┬───────────────┘   │
└──────────────────┼───────────────────┘
                   │ HTTP / Web Dashboard
                   ▼
┌──────────────────────────────────────┐
│       Engineer Web Browser           │
│   - Date-Picker & Quick Presets      │
│   - Live In/Out Telemetry Card       │
│   - Download PNG, Excel, CSV         │
└──────────────────────────────────────┘
```

---

## Configuration Reference (`.env`)

Copy `.env.example` to `.env` and adjust the variables:

```ini
# Application branding and site identification
APP_NAME="MRTG Traffic Monitor"
SITE_NAME="Enterprise Gateway"
LOCATION_NAME="Branch Office"
UPLINK_NAME="Main Uplink (150 Mbps)"
DATABASE_PATH="data/traffic.db"

# MikroTik RouterOS API Settings
ROUTEROS_HOST="192.168.88.1"
ROUTEROS_PORT=8728
ROUTEROS_USERNAME="mrtg"
ROUTEROS_PASSWORD="YourRouterPassword"
ROUTEROS_INTERFACE="WAN"

# Collector Timing (seconds)
POLLING_INTERVAL=60

# Web Dashboard Server
WEB_HOST="0.0.0.0"
WEB_PORT=8000
SECRET_KEY="generate-a-secure-random-key"
SESSION_COOKIE_SECURE=false
SESSION_TTL_SECONDS=28800
REMEMBER_ME_TTL_SECONDS=2592000

# Default Initial Administrator
ADMIN_USERNAME="admin"
ADMIN_PASSWORD="ChangeMeImmediately123!"
```

---

## Local Development & Testing (Windows 11)

### 1. Prerequisites
- Python 3.11+ (Python 3.14 recommended)
- `uv` package manager (`winget install astral-sh.uv`)
- Git

### 2. Setup Environment
```powershell
# Clone or navigate to the repository
cd MRTG-CMP

# Install all project and development dependencies
uv sync --all-extras

# Copy environment settings
cp .env.example .env
```

### 3. Initialize Database & Admin User
```powershell
uv run mrtg-cmp init-db
```
*(Default user: `admin` / `admin123`)*

To create or update a user password:
```powershell
uv run mrtg-cmp create-user --username engineer --password "YourPassword!"
```

### 4. Run the Application Locally
To run both the background collector and the web server together:
```powershell
uv run mrtg-cmp all --port 8000
```
Open your browser and navigate to: `http://localhost:8000`

### 5. Run Automated Tests & Quality Checks
```powershell
# Run full unit and integration test suite
uv run pytest -v

# Run linting
uv run ruff check

# Run type checks
uv run mypy src
```

---

## Production Deployment (Debian 13)

### 1. System Preparation
```bash
sudo apt update && sudo apt install -y git python3 python3-pip python3-venv curl ufw
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

### 2. Deploy Project Directory
```bash
cd /home/mriazh
git clone https://github.com/mriazh/MRTG-CMP.git
cd MRTG-CMP

# Install production dependencies
uv sync --no-dev
cp .env.example .env
nano .env  # configure actual router password and secret key

# Initialize database
uv run mrtg-cmp init-db

# Make automated deployment script executable
chmod +x deploy.sh
```

### 3. Install Systemd Services (Auto-Run on Reboot)
The standardized `mrtg-cmp-*` units ship with `@APP_DIR@` / `@APP_USER@` placeholders that
`deploy.sh` substitutes at install time, so the same units work on any checkout path:
```bash
sudo sed -e "s|@APP_DIR@|$PWD|g" -e "s|@APP_USER@|$(id -un)|g" \
    systemd/mrtg-cmp-collector.service | sudo tee /etc/systemd/system/mrtg-cmp-collector.service
sudo sed -e "s|@APP_DIR@|$PWD|g" -e "s|@APP_USER@|$(id -un)|g" \
    systemd/mrtg-cmp-web.service | sudo tee /etc/systemd/system/mrtg-cmp-web.service
sudo sed -e "s|@APP_DIR@|$PWD|g" -e "s|@APP_USER@|$(id -un)|g" \
    systemd/mrtg-cmp-netcare.service | sudo tee /etc/systemd/system/mrtg-cmp-netcare.service
sudo systemctl daemon-reload
sudo systemctl enable --now mrtg-cmp-collector.service
sudo systemctl enable --now mrtg-cmp-web.service
sudo systemctl enable --now mrtg-cmp-netcare.service
```

| Unit | Runs | Purpose |
| :--- | :--- | :--- |
| `mrtg-cmp-web.service` | `python -m mrtg_cmp web` | Unified dashboard + API |
| `mrtg-cmp-collector.service` | `python -m mrtg_cmp collect` | MikroTik WAN polling every 30s |
| `mrtg-cmp-netcare.service` | `python -m mrtg_cmp netcare` | TelkomCare branch graph scraper every 5 min |

The Netcare unit needs Google Chrome (or Chromium) installed for headless graph capture:
```bash
sudo apt install -y chromium
```

### 4. Firewall & Network Access
Allow web access through the Debian firewall:
```bash
sudo ufw allow 8000/tcp comment "MRTG Web Dashboard & Console"
```

- **Office LAN Access**: Connect your workstation to the branch network (cable or Wi-Fi) and open:
  `http://<server-ip>:8000`
- **Remote / WFH Access**: Connect your workstation to your corporate VPN client. Once connected to the tunnel, navigate to:
  `http://<server-ip>:8000`

### 5. Automated 1-Click Fast Updates (`deploy.sh`)
Whenever updates are pushed from development, update the production server with zero hassle:
```bash
./deploy.sh
```
This script pulls the latest git commits, syncs Python packages, stops and disables the legacy
`mrtg-poncab-*` units if they are installed, renders the `mrtg-cmp-*` units for the current
checkout path, then enables and restarts all three services. Traffic data and web sessions
are preserved.

### 6. Service Health & Logs
```bash
# Check service statuses
sudo systemctl status mrtg-cmp-web.service
sudo systemctl status mrtg-cmp-collector.service
sudo systemctl status mrtg-cmp-netcare.service

# Stream live collector logs
sudo journalctl -u mrtg-cmp-collector.service -f

# Stream Netcare scraper logs (login, CAPTCHA rotation, capture status)
sudo journalctl -u mrtg-cmp-netcare.service -f
```

---

## TelkomCare Netcare Scraper

The scraper automates the TelkomCare MRTG portal so branch graphs appear on the same
dashboard as the live MikroTik telemetry.

### How It Works
1. **Login** — a persistent Chrome profile plus an exported `cookies.json` keep the portal
   session alive. Gemini Vision is only called when the cookies have expired.
2. **CAPTCHA failover** — the 3-character login CAPTCHA is solved through the Gemini Vision
   API. `GEMINI_API_KEYS` rotates to the next key on HTTP 429, and `GEMINI_MODELS` falls back
   through the configured model list. Failover is bounded: each request times out after at
   most 6s, and a model answering HTTP 404 or 503 is unavailable rather than busy, so it is
   blacklisted for the rest of the session and skipped instantly on later CAPTCHAs.
3. **MFA** — a 6-digit code is generated from `TOTP_SECRET` with `pyotp`, so login is fully
   unattended.
4. **Capture** — each target is filtered to the requested range (today's window by
   default, `00:00` to `23:55`), the `graph.php` image is isolated via injected
   JavaScript, and only that element is screenshotted. Targets are captured in
   parallel by the worker pool, each worker owning its own browser session.
5. **Validation & storage** — every capture is checked with Pillow (rejecting blank, solid,
   and "no graph" placeholders), then atomically replaces `data/netcare_cache/{target}.png`
   (or the day partition for a past range). Manifest read-modify-writes and temp-file swaps
   run under a lock, so concurrent workers cannot lose each other's status updates. The live
   cache holds exactly one image per target, so it stays under ~2 MB and no images are stored
   in SQLite.
6. **Resilience** — a failed target never deletes its previous image; the status manifest
   records it as `stale` so the dashboard keeps showing the last good graph.

### Dashboard Integration
- **Regional filters**: All (18), CGK Area (7), Surabaya (2), Makassar (4),
  Denpasar (2), Balikpapan (1), Sentul VPN (2).
- **Branch cards**: branch name, target ID, physical address, relative update badge
  (`Updated 3m ago`), and status indicator.
- **Click to zoom**: full-resolution modal with metadata and a PNG download button.
- **Auto-refresh**: the page reloads Netcare data every 5 minutes with a visible countdown
  and a manual "Refresh All" button. MikroTik live polling stays at 30 seconds.
- **Reserved nav**: `/orbit` is a placeholder for the upcoming Telkomsel Orbit modem page.

### Time Range Selection
The Netcare section has its own toolbar, mirroring the MikroTik one so both mean
the same thing:

- **Presets**: 1 Hour, 3 Hours, 6 Hours, 12 Hours, 24 Hours, Today, Yesterday,
  7 Days, This Month, and Select Day (24h).
- **Custom range**: `From` / `To` inputs with a `Now` shortcut and a `Filter` action.

Picking one opens a non-blocking loading dialog showing an animated spinner, a live
progress bar (`x / 18 cabang selesai`), and a countdown estimate. The scrape runs in
the background so the rest of the page stays usable; the dialog switches to a green
checkmark and closes itself a second after the last branch lands.

### Parallel Worker Pool
Each scrape round fans out over `NETCARE_WORKERS` threads (default `3`), with targets
split into balanced buckets — 18 branches become 6/6/6. Each worker drives its own
browser session, so a full round drops from roughly 150 seconds to about 45. Raise it
toward 8 on a host with spare memory, since each worker adds a Chrome process.

### Running It
```bash
# One-off round (repeatable for testing)
uv run mrtg-cmp netcare -n 1

# Continuous daemon
uv run mrtg-cmp netcare
```

### Configuration
All settings live in `.env` (documented in `.env.example`). At minimum set
`GEMINI_API_KEYS`, `TELKOM_USER`, `TELKOM_PASSWORD`, and `TOTP_SECRET` to enable auto-login.

Deployments holding the full master list can point `NETCARE_CATALOG_FILE` at a CSV export
with the columns `type,target,name,address,region,ocr_enabled,service_type` to replace the
built-in branch names and addresses at runtime. The committed
`config/netcare_targets.csv.example` documents that format with 18 anonymous circuits
(`target-001` through `target-018`); copy it to `config/netcare_targets.csv` and fill in your
own circuits. That file is gitignored, because it names your portal circuit ids, branch names,
and facility addresses.

### Storage Layout
```
data/netcare_cache/
├── target-001.png       # one latest graph per target
├── target-018.png
├── status.json              # per-target status, timestamp, error, file size
├── 2026-09-20/              # historical day partition, written on demand
│   ├── target-001.png
│   └── ...
└── status_2026-09-20.json   # that day's status manifest
```

A time range that reaches into the past is captured into its own `YYYY-MM-DD`
partition instead of overwriting the live graph. Picking that day again serves it
straight from disk, so a repeat lookup is instant. The current day always stays
flat and always re-captures, because it is the "latest" view.

---

## CLI Command Reference

| Command | Description | Example |
| :--- | :--- | :--- |
| `init-db` | Create SQLite schema and seed administrator | `mrtg-cmp init-db` |
| `create-user` | Create or update a dashboard user | `mrtg-cmp create-user -u engineer -p pass` |
| `collect` | Run background traffic collection loop | `mrtg-cmp collect --interval 300` |
| `web` | Start FastAPI web server | `mrtg-cmp web --host 0.0.0.0 --port 8000` |
| `netcare` | Run the TelkomCare branch graph scraper daemon | `mrtg-cmp netcare` |
| `netcare` (one round) | Scrape all branch graphs once | `mrtg-cmp netcare -n 1` |
| `all` | Run collector and web server concurrently | `mrtg-cmp all --port 8000` |
| `all` (with scraper) | Also run the Netcare daemon in a thread | `mrtg-cmp all --with-netcare` |

---

## License & Attribution
Network monitoring tool designed to match RRDtool visual standards for enterprise branch gateways.
