# MRTG-CMP

Enterprise network monitoring platform for a MikroTik WAN uplink plus two live portal scrapers (TelkomCare MRTG branch graphs and Telkomsel Orbit modem quotas). Traffic samples land in an embedded SQLite database (WAL mode) and are served through an authenticated FastAPI dashboard with RRDtool-style graphs, PNG/Excel/CSV exports, an interactive Leaflet map, and a NOC status strip.

Production runs as four systemd daemons: `mrtg-cmp-web` (FastAPI modular router), `mrtg-cmp-collector` (MikroTik WAN telemetry), `mrtg-cmp-netcare` (TelkomCare 18-branch scraper), and `mrtg-cmp-orbit` (Telkomsel Orbit 12-modem quota scraper).

## Key Capabilities

- **Direct RouterOS API Polling**: reads cumulative octet counters over TCP 8728 (or a tunnel port) with a delta-rate engine that survives counter rollover and router reboots.
- **RRDtool-Fidelity Graphs**: Matplotlib rendering with dynamic autoscaling, adaptive time ticks per range, and stepped inbound/outbound styling; PNG, Excel (`.xlsx`), and CSV exports for any time window.
- **Point-and-Click Time Ranges**: presets (1/3/6/12/24 Hours, Today, Yesterday, 7 Days, This Month), a day picker, a custom From/To selector, and a Now shortcut.
- **RouterOS Web Console**: browser terminal with live RouterOS passthrough auth, command history, and a SQLite audit trail; auto-lock kills the session on tab close.
- **TelkomCare Scraper**: headless-Chrome capture of 18 branch MRTG graphs with Gemini CAPTCHA failover, TOTP auto-login, and a parallel worker pool.
- **Telkomsel Orbit Scraper**: 12-modem quota monitoring with burn-rate forecasting, days-to-exhaustion, and predictive WhatsApp alerts.
- **Interactive Leaflet Map**: top-of-dashboard CartoDB Dark Matter map plotting Netcare branches and Orbit modems with valid coordinates (30 target coordinates across both catalogs), with a Show/Hide toggle.
- **NOC Summary Strip**: Fresh/Stale/Down counters for the 18 Netcare links, updated on each scrape round.
- **Hardened Auth**: SHA-256 session-token hashing, session revocation on logout and password change, admin/viewer RBAC, and open-redirect validation.
- **Zero External Database**: embedded SQLite (WAL) — portable across Windows 11 and Debian 13.

## Architecture

```
  MikroTik RouterOS (WAN uplink)          TelkomCare Portal            MyOrbit Portal
         │ RouterOS API (TCP 8728)          │ HTTPS + headless Chrome    │ HTTPS + headless Chrome
         ▼                                  ▼                            ▼
 mrtg-cmp-collector                mrtg-cmp-netcare               mrtg-cmp-orbit
 (MikroTik telemetry, 30s)         (18-branch graph scraper,      (12-modem quota scraper,
   10s fast-probe on DOWN)          300s)                          300s)
         │                              │                            │
         ▼                              ▼                            ▼
  SQLite (data/traffic.db)     data/netcare_cache/            data/orbit_cache/
                                   \                              /
                                    └────────────┬───────────────┘
                                                 ▼
                       mrtg-cmp-web (FastAPI modular router, port 8000)
                       dashboard, map, NOC strip, exports, console
                                                 │
                                                 ▼
                     Engineer browser (via office LAN / VPN, or
                     Cloudflare Zero Trust tunnel for remote access)
```

### Daemons & Polling Intervals

| Daemon | Command | Interval | Notes |
| :--- | :--- | :--- | :--- |
| `mrtg-cmp-collector` | `python -m mrtg_cmp collect` | 30s | `POLLING_INTERVAL=30`; on link/route DOWN the interval drops to 10s fast-probe (`POLLING_INTERVAL_DOWN=10`) until recovery |
| `mrtg-cmp-web` | `python -m mrtg_cmp web` | — | FastAPI/Uvicorn on port 8000; routes live in `web/routes/` modules (`auth`, `dashboard`, `netcare`, `orbit`, `console`, `logs`) |
| `mrtg-cmp-netcare` | `python -m mrtg_cmp netcare` | 300s | `NETCARE_POLL_INTERVAL_SECONDS=300` |
| `mrtg-cmp-orbit` | `python -m mrtg_cmp orbit` | 300s | `ORBIT_SYNC_INTERVAL_SECONDS=300` |

Frontend cadence: the dashboard's live telemetry card polls every 30s; the Netcare and Orbit card grids auto-refresh from server cache every 300s with a visible countdown and manual "Refresh All" (Orbit Refresh All triggers an immediate live scrape).

### Security Architecture

- **Session tokens**: stored only as SHA-256 hashes in SQLite (`sessions.token_hash`); the raw token lives in the HttpOnly `SameSite=Lax` cookie.
- **Revocation**: logout revokes the current session; password change revokes all of that user's sessions.
- **RBAC**: `admin` role can manage users, scrape triggers, catalog CRUD, and the console; `viewer` role is read-only (no console nav, write APIs return 403).
- **Open-redirect validation**: login `next` targets pass `validate_redirect_url()` (CWE-601) — absolute/external URLs are rejected.
- **Passwords**: PBKDF2-HMAC-SHA256, 260,000 iterations, 16-byte random salt.
- **Remote access**: the dashboard is reached through a Cloudflare Zero Trust tunnel (Argo) rather than a public IP; office LAN and VPN access remain available.

---

## Configuration

All settings live in `config/.env` (documented with comments in `config/.env.example`; a root `.env` is optional and read after it). Key variables:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `ROUTEROS_HOST` / `ROUTEROS_PORT` | `192.168.88.1` / `8728` | RouterOS API endpoint (tunnel domain for remote) |
| `POLLING_INTERVAL` | `60` | Collector cycle, seconds (production sets `30`) |
| `POLLING_INTERVAL_DOWN` | `10` | Fast-probe cycle while the link is DOWN |
| `NETCARE_POLL_INTERVAL_SECONDS` | `300` | Netcare scrape round, seconds |
| `ORBIT_SYNC_INTERVAL_SECONDS` | `300` | Orbit scrape round, seconds |
| `WEB_PORT` | `8000` | Dashboard bind port |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `admin123` | Initial admin seeded by `init-db` (change immediately) |
| `TUNNEL_WEB_*` | empty | Optional tunnel.web.id watchdog auto-restart |
| `WA_ALERT_ENABLED` / `WA_*` | `false` | Optional GOWA WhatsApp gateway alerts |
| `GEMINI_API_KEYS` / `GEMINI_MODELS` | empty | Netcare CAPTCHA solving keys & model failover list |
| `TOTP_SECRET` | — | Unattended Netcare portal MFA (omit to log in manually) |

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

# Copy environment settings (root .env.example -> .env; config/.env also supported)
Copy-Item .env.example .env
```

### 3. Initialize Database & Admin User
```powershell
uv run mrtg-cmp init-db
```
Seeds the initial admin (`admin` / `admin123` by default — set `ADMIN_PASSWORD` in `.env` or rotate it immediately).

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
cd /home/<user>
git clone https://github.com/<org>/MRTG-CMP.git
cd MRTG-CMP

# Install production dependencies
uv sync --no-dev
cp .env.example .env
nano .env  # router credentials, secret key, POLLING_INTERVAL=30

# Initialize database
uv run mrtg-cmp init-db

# Make the deployment script executable
chmod +x deploy.sh
```

### 3. Install the Four Systemd Services
`deploy.sh` renders all four units (substituting the `@APP_DIR@` / `@APP_USER@` placeholders
from `systemd/`) and manages them end to end, so the manual steps below are only needed for
first-time reference:

| Unit | Runs | Purpose |
| :--- | :--- | :--- |
| `mrtg-cmp-web.service` | `python -m mrtg_cmp web` | FastAPI modular-router dashboard on port 8000 |
| `mrtg-cmp-collector.service` | `python -m mrtg_cmp collect` | MikroTik WAN telemetry every 30s (10s fast-probe on DOWN) |
| `mrtg-cmp-netcare.service` | `python -m mrtg_cmp netcare` | TelkomCare 18-branch graph scraper every 300s |
| `mrtg-cmp-orbit.service` | `python -m mrtg_cmp orbit` | Telkomsel Orbit 12-modem quota scraper every 300s |

```bash
sudo sed -e "s|@APP_DIR@|$PWD|g" -e "s|@APP_USER@|$(id -un)|g" \
    systemd/mrtg-cmp-collector.service | sudo tee /etc/systemd/system/mrtg-cmp-collector.service
sudo sed -e "s|@APP_DIR@|$PWD|g" -e "s|@APP_USER@|$(id -un)|g" \
    systemd/mrtg-cmp-web.service | sudo tee /etc/systemd/system/mrtg-cmp-web.service
sudo sed -e "s|@APP_DIR@|$PWD|g" -e "s|@APP_USER@|$(id -un)|g" \
    systemd/mrtg-cmp-netcare.service | sudo tee /etc/systemd/system/mrtg-cmp-netcare.service
sudo sed -e "s|@APP_DIR@|$PWD|g" -e "s|@APP_USER@|$(id -un)|g" \
    systemd/mrtg-cmp-orbit.service | sudo tee /etc/systemd/system/mrtg-cmp-orbit.service
sudo systemctl daemon-reload
sudo systemctl enable --now mrtg-cmp-collector.service mrtg-cmp-web.service mrtg-cmp-netcare.service mrtg-cmp-orbit.service
```

The Netcare and Orbit units both need Chrome (or Chromium) for headless scraping:
```bash
sudo apt install -y chromium
```

### 4. Network Access
The dashboard is not exposed to the open internet. Remote access goes through the
Cloudflare Zero Trust tunnel (Argo) registered for the server's port 8000; office LAN and
VPN clients can also reach `http://<server-ip>:8000` directly.

### 5. 1-Click Updates (`deploy.sh`)
```bash
./deploy.sh
```
Pulls the latest commits, syncs Python packages, disables any legacy `mrtg-poncab-*`
units, re-renders the four `mrtg-cmp-*` units for the current checkout path, then restarts
all four services. Traffic data and web sessions survive the restart.

### 6. Service Health & Logs
```bash
sudo systemctl status mrtg-cmp-web.service mrtg-cmp-collector.service mrtg-cmp-netcare.service mrtg-cmp-orbit.service
sudo journalctl -u mrtg-cmp-collector.service -f   # telemetry collection
sudo journalctl -u mrtg-cmp-netcare.service -f     # CAPTCHA, login, capture status
sudo journalctl -u mrtg-cmp-orbit.service -f       # quota scrape rounds
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
- **Auto-refresh**: the Netcare grid reloads from server cache every 300s with a visible
  countdown and a manual "Refresh All" button. MikroTik live polling stays at 30 seconds.
- **NOC summary strip**: Fresh / Stale / Down counters across all 18 branches above the
  branch grid.

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
All settings live in `config/.env` (documented in `config/.env.example`). Set
`NETCARE_CATALOG_FILE` to a CSV export with the columns
`type,target,name,address,region,ocr_enabled,service_type` to override the built-in branch
list at runtime. `config/netcare_targets.csv.example` documents that format with 18 example
circuits (`target-001` through `target-018`); copy it to `config/netcare_targets.csv` and
fill in your own circuits — that file is gitignored because it names your portal circuit
ids, branch names, and facility addresses.

For unattended login, set `GEMINI_API_KEYS` (CAPTCHA solving) and `TOTP_SECRET`
(MFA code generation).

### Storage Layout

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

## Telkomsel Orbit Modem Quota Scraper

Scrapes the live MyOrbit portal (`myorbit.id`) for the 12 modems in
`config/orbit_targets.csv` (or a private Excel catalog), and persists per-modem
quota, expiry, and status to `data/orbit_cache/modems.json`. The dashboard `/orbit` page renders each modem
with a quota bar, "days to empty" forecast, and a predictive WhatsApp alert
(`burn_rate.py`) when the remaining balance will be exhausted before the next
renewal window.

### How It Works
1. **Catalog** — `config/orbit_targets.csv` (or private Excel catalog) lists
   12 modems with `no,imei,phone,location,ssid,status,latitude,longitude`.
   Modems with `status=IMEI_PENDING` are skipped (no valid IMEI yet); the
   remaining 10 are scraped every round.
2. **Live scrape** — a headless Chrome session logs into the portal and reads
   the "Paket Aktif" panel for each modem. Quota, expiry, and status are
   parsed with strict guards against stray SSID / IMEI numbers and
   `DRIVER_UNAVAILABLE` zero-quota overwrites.
3. **Burn-rate forecast** — `orbit/burn_rate.py` computes `GB/day` burn rate
   from the active package, projects days-to-empty, and classifies the alert
   level (`OK` / `WARNING` / `CRITICAL`). When `WA_ALERT_ENABLED` is set, a
   WhatsApp notification fires on crossing `CRITICAL`.
4. **Cache** — results land in `data/orbit_cache/modems.json` under a lock so
   concurrent reads (dashboard, Orbit page) never see a half-written cache.

### Dashboard Integration
- **Orbit page** (`/orbit`): 12 modem cards sorted by lowest-quota-first by
  default; manual "Refresh All" triggers an immediate live scrape in the
  background, with a non-blocking progress dialog.
- **Interactive Leaflet map**: modems with valid coordinates (all 12, plus
  regional fallbacks from `map_data.py` for any blank row) are plotted
  alongside the 18 Netcare branch pins on the top-of-dashboard map.
- **Auto-refresh**: the `/orbit` page reloads from the server cache every
  300s with a visible countdown.

### Running It
```bash
# One-off sync round (repeatable for testing)
uv run mrtg-cmp orbit -n 1

# Continuous daemon
uv run mrtg-cmp orbit
```

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
| `orbit` | Run the Telkomsel Orbit modem quota scraper daemon (300s cadence) | `mrtg-cmp orbit` |
| `orbit` (one round) | Scrape all Orbit modem quotas once | `mrtg-cmp orbit -n 1` |
| `all` | Run collector and web server concurrently | `mrtg-cmp all --port 8000` |
| `all` (with Netcare scraper) | Also run the Netcare daemon in a thread | `mrtg-cmp all --with-netcare` |

---

## License & Attribution
Network monitoring tool designed to match RRDtool visual standards for enterprise branch gateways.
