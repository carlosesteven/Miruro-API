# MI API

FastAPI proxy over AniList GraphQL and the MI pipe. Returns anime metadata, episode lists, and streaming sources.

---

## Setup

```bash
git clone https://github.com/carlosesteven/MI-API.git
cd MI-API
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Environment variables

Copy `.env_example` to `.env` and fill in your values:

```bash
cp .env_example .env
```

| Variable                      | Default                                     | Purpose                                                                                                                       |
| ----------------------------- | ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `ALLOWED_ORIGINS`             | —                                           | Comma-separated CORS + auth whitelist                                                                                         |
| `API_KEY`                     | —                                           | Auth header value (`x-api-key`)                                                                                               |
| `API_DEBUG`                   | `False`                                     | `True` renders full HTML docs at `/`                                                                                          |
| `REDIS_HOST`                  | `localhost`                                 | Redis host for caching                                                                                                        |
| `REDIS_PORT`                  | `XXXX`                                      | Redis port                                                                                                                    |
| `REDIS_PASSWORD`              | —                                           | Redis password                                                                                                                |
| `CACHE_RECENT_EPISODES_HOURS` | `2`                                         | TTL for `/recent-episodes` cache                                                                                              |
| `MIRURO_BASE_URL`             | — (required)                                | Base domain for the Miruro pipe, e.g. `https://www.miruro.XX`. Update this if Miruro changes domains                          |
| `PIPE_IMPERSONATE`            | — (required)                                | `curl_cffi` browser TLS fingerprint to impersonate for Cloudflare bypass, e.g. `chrome1XX`                                    |
| `PIPE_USER_AGENT`             | `Mozilla/5.0 (Windows NT 10.0; Win64; x64)` | User-Agent sent to the pipe                                                                                                   |
| `PIPE_EXTRA_HEADERS`          | `{}`                                        | JSON object merged into pipe request headers (e.g. `sec-ch-ua`, `accept`) — used to adapt to Cloudflare without touching code |

> `MIRURO_BASE_URL` and `PIPE_IMPERSONATE` have no code fallback — the server will not start without them set.

### Run locally

```bash
python -m uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000/` for interactive API docs (requires `API_DEBUG=True`).

---

## Deploy (production — uvicorn direct)

### First deploy

```bash
git clone https://github.com/carlosesteven/MI-API.git
cd MI-API
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
nohup python -m uvicorn api:app --host 0.0.0.0 --port 8848 > /dev/null 2>&1 &
nohup python -m uvicorn api:app --host 0.0.0.0 --port 8848 > "uvicorn-$(date +%F-%H%M%S).log" 2>&1 &
```

### Update to latest version

```bash
# 1. Pull changes
git pull

# 2. Install any new dependencies
pip install -r requirements.txt

# 3. Find the exact PID of this service (do NOT kill others)
ps aux | grep uvicorn

# 4. Kill only this process by PID
kill <PID>

# 5. Start again
nohup python -m uvicorn api:app --host 0.0.0.0 --port 8848 > /dev/null 2>&1 &
nohup python -m uvicorn api:app --host 0.0.0.0 --port 8848 > "uvicorn-$(date +%F-%H%M%S).log" 2>&1 &
```

> **Important:** always kill by PID (`kill <PID>`), not by name (`pkill`). The server may be running multiple uvicorn processes on different ports.

## Deploy (production — systemd service, auto-start on boot)

Instead of manually launching `nohup` after every reboot, register a systemd service so the API starts automatically on boot and restarts on crash.

### Install

Create `/etc/systemd/system/mi-api.service`:

```ini
[Unit]
Description=MI-API (uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=REPLACE_WITH_YOUR_USER
Group=REPLACE_WITH_YOUR_USER
WorkingDirectory=/REPLACE/WITH/PATH/TO/MI-API
ExecStart=/bin/bash -c 'source /REPLACE/WITH/PATH/TO/MI-API/venv/bin/activate && exec python -m uvicorn api:app --host 0.0.0.0 --port 8848 >> "/REPLACE/WITH/PATH/TO/MI-API/uvicorn-$(date +%%F-%%H%%M%%S).log" 2>&1'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload

sudo systemctl enable mi-api.service

sudo systemctl start mi-api.service

sudo systemctl stop mi-api.service

sudo systemctl daemon-reload

sudo systemctl restart mi-api.service

sudo systemctl status mi-api.service
```

`ExecStart` activates the venv and runs uvicorn the same way as the manual command, writing a timestamped log file on every start (matches the `uvicorn-$(date +%F-%H%M%S).log` convention above). `nohup`/`&` are not needed here — systemd already detaches the process from any terminal; `Type=simple` requires the process to stay in the foreground, which `exec` guarantees.

### Verify

```bash
sudo systemctl status mi-api.service     # process state, main PID
journalctl -u mi-api -f                  # systemd-level logs (start/stop/restarts)
tail -f uvicorn-*.log                    # uvicorn's own logs
```

> If a manually-started (`nohup`) instance is already bound to port 8848, the service will fail to bind and keep restarting (`Errno 98: address already in use`) until that process is killed.

### Update to latest version

```bash
git pull
source venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart mi-api.service
```

No more hunting for PIDs to kill manually — systemd manages the process lifecycle.

## Disclaimer

This project is for educational purposes and API integrity research only. The author takes absolutely zero responsibility for network usage. Code contains zero skiddable artifacts.
