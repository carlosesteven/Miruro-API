# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Lectura obligatoria al iniciar sesión

Lee [`SESSION_LOG.md`](./SESSION_LOG.md) antes de cualquier tarea. Contiene el historial de cambios realizados en sesiones anteriores: qué se implementó, qué archivos se modificaron y decisiones de diseño tomadas. Actualiza ese archivo al final de cada sesión con un resumen de lo que hiciste.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
uvicorn api:app --host 0.0.0.0 --port 8000 --reload

# Docker
docker build -t miruro-api .
docker run -p 8000:8000 miruro-api
```

There is no test suite and no linting configuration.

## Deployment: 5 nodes, one is special

This app runs on 5 nodes behind a load balancer: 4 cloud nodes + **this physical home machine**
(`home.csc-lab.co`, reachable from the cloud nodes over ZeroTier at `10.147.19.131`). Only this
home machine has Hermes (a personal agent framework) installed, which is how Telegram alerts get
sent — see `NOTIFY_RELAY_URL` below. All 5 nodes share the same Redis instance and the same
`API_KEY`.

## Architecture

The entire API lives in a single file: `api.py` (~1100 lines). It is a FastAPI app that acts as a thin, authenticated proxy over two upstream sources:

1. **AniList GraphQL** (`https://graphql.anilist.co`) — all anime metadata: search, filter, collections, info, characters, relations, recommendations.
2. **Miruro Pipe** (`{MIRURO_BASE_URL}/api/secure/pipe`) — episode lists and M3U8 streaming URLs. Miruro's pipe protocol base64-encodes (no gzip) every request and gzip+base64-compresses every response; `_encode_pipe_request()` and `_decode_pipe_response()` handle this transparently.

### Cloudflare `cf_clearance` — required to reach the pipe at all

Miruro's Cloudflare zone serves an interactive JS challenge ("Just a moment...") to this app's
traffic. A valid `cf_clearance` cookie (plus the exact browser headers it was solved with) is
required on every pipe request, or Cloudflare 403s it. Two things had to be true at once for
this to work reliably, both found the hard way (see `SESSION_LOG.md`, sessions 2026-09-07):

1. **Getting the cookie**: solving the challenge requires a real, non-headless browser. Headless
   Chromium (plain Playwright, and `patchright`'s stealth fork) gets stuck on the challenge
   forever. `cf_refresher.py` solves it with `patchright` in **non-headless** mode under **Xvfb**
   (`xvfb-run -a`), which works in ~10-15s. It publishes `{cookie, headers}` as JSON to the Redis
   key `miruro_api:cf_clearance` (`_get_pipe_headers()` reads it, cached in-process for
   `CF_CLEARANCE_LOCAL_CACHE_SECONDS`). The cookie is **not** tied to the requesting IP (verified:
   a cookie solved on one device works fine replayed from this server) — it's tied to the header
   set (`sec-ch-ua`/`user-agent`/etc.) matching exactly what Cloudflare saw when it was issued.
2. **Replaying the cookie**: even with a byte-for-byte matching cookie+headers, the pipe still
   403s if the request goes out over plain **HTTP/1.1** — `curl_cffi` (any `impersonate=` profile)
   and httpx's default both failed live; only HTTP/2 (`httpx.AsyncClient(http2=True)`, matching
   what a real browser and system `curl` both negotiate by default) gets a 200. `_pipe_get()` uses
   `httpx` with `http2=True` whenever a `cf_clearance` blob is present in Redis, falling back to
   the old `curl_cffi` (`impersonate=PIPE_IMPERSONATE`) `pipe_session` only when Redis has nothing
   cached (rare/degraded path, effectively dead weight now but kept as a fallback).

**Keeping the cookie fresh — two triggers, not one:**
- *Proactive*: `cf_refresher.py` is meant to run on a timer (`mi-api-cf-refresh.timer`/`.service`,
  **not yet installed** as of 2026-09-07 — see SESSION_LOG). It checks the cookie's Redis TTL
  first and skips (no browser launch) unless TTL < `MIN_TTL_BEFORE_REFRESH_SECONDS` (10 min) —
  cuts real Chromium/challenge-solve runs down from one per timer tick to only when actually
  needed.
- *Reactive* (the one that matters for uptime): when a live pipe request gets a 403 with a
  cookie set, `api.py`'s `_trigger_reactive_cf_refresh()` fires immediately — a Redis lock
  (`miruro_api:cf_refresher:reactive_trigger_lock`, 60s TTL) de-dupes concurrent failures across
  ALL 5 nodes into one browser launch, and `cf_refresher.py --force` (bypasses the TTL-skip
  check) runs in the background. Recovery for subsequent requests: ~15-30s. A Redis TTL that
  says "still valid" is **not proof the cookie actually works** (learned the hard way) — the
  reactive path is what actually catches real breakage, the proactive timer is just cheap
  insurance between failures.
- If the forced refresh itself fails (e.g. Cloudflare escalates to an interactive Turnstile a
  non-headless-but-still-automated browser can't solve), the service **stays down** — there's no
  further automatic fallback. A human has to solve the challenge in a real browser and hand the
  `cf_clearance` + full header set over to be pushed into Redis manually.
- **Alerting is intentionally NOT debounced** — every failed forced-refresh attempt sends a
  Telegram message (capped at ~once/minute by the 60s trigger lock, not by any cooldown on the
  alert itself). This is deliberate: it's a critical service with apps depending on uptime: the
  user wants to be spammed, not softly notified once.

### `NOTIFY_RELAY_URL` — Telegram alerts from the 4 cloud nodes

Only the home node has Hermes installed, so `notify_telegram()`/`_notify_telegram()` check for
the local Hermes binary first (`/home/carlos-esteven/.hermes/hermes-agent/venv/bin/hermes`); if
it's missing (any cloud node), they POST `{"message": ...}` to `f"{NOTIFY_RELAY_URL}/internal/notify"`
instead, authenticated with this deployment's own `API_KEY`. `POST /internal/notify` (in
`api.py`) is what actually calls Hermes on the receiving end — it's a normal endpoint (not in the
auth-bypass list), so it's protected by the same `x-api-key` check as everything else. Leave
`NOTIFY_RELAY_URL` unset on the home node; set it to `http://10.147.19.131:8848` (the home
node's ZeroTier address) on the 4 cloud nodes.

### `cf_refresher.py` and `mi_api_mcp.py` — companion files, not deployed to Vercel

- `cf_refresher.py`: standalone script described above. Needs `patchright` (its browser installed
  via `python -m patchright install chromium`) and `xvfb` (`sudo apt install xvfb`) on any node
  that should be able to solve the Cloudflare challenge — in practice, cloud nodes may not have
  Xvfb set up, in which case they rely on the reactive-refresh Redis lock being grabbed by the
  home node instead (or whichever node does have Xvfb) since the cookie itself is shared via
  Redis across the whole fleet.
- `mi_api_mcp.py`: MCP server (stdio, `mcp.server.fastmcp.FastMCP`) exposing `estado_cf_clearance()`
  and `refrescar_cf_clearance()` for manual diagnosis/triggering from Hermes chat. Registered in
  `~/.hermes/config.yaml` under `mcp_servers.mi_api` (home node only — that's where Hermes runs).
  **Pin `mcp[cli]==1.28.1` in requirements.txt** — `mcp` v2.x renamed `FastMCP` to `MCPServer` and
  breaks this import; all the other MCP servers on this machine (`camaras_ip`, `jkanime_relator`,
  etc.) are on 1.28.1 too, for the same reason.

### `mac_agent/` — second-tier cf_clearance fallback, runs on a Mac

Even the home node's own Xvfb+patchright automation eventually gets distrusted by Cloudflare —
it's the same IP auto-solving hundreds of challenges a day, which is exactly the pattern a
bot-management WAF learns to flag (confirmed live: it started failing to solve the challenge at
all, escalating to what looked like an interactive Turnstile). A residential Mac running its
actual installed Chrome, with a normal mixed human traffic history, is trusted far more —
manually-pasted cookies from a real Mac worked every single time this happened. `mac_agent/`
automates that same act instead of asking a human to open DevTools and copy ~20 headers by hand.

**Trigger model** (server side lives in `api.py`'s `_trigger_reactive_cf_refresh`, fired
alongside — not after — the home node's own Linux attempt):
- `SET miruro_api:need_mac_refresh EX 600` — a durable flag, polled by `mac_agent/refresher.py`
  every `MAC_AGENT_POLL_INTERVAL_SECONDS` (default 30 min) as the fallback for whenever the Mac
  was asleep/offline at the moment of the real event.
- `PUBLISH miruro_api:mac_refresh_channel` — instant reaction whenever the Mac's listener happens
  to already be connected. Redis Pub/Sub does **not** queue messages for offline subscribers, so
  this is the fast path, never the only path.
- `asyncio.create_task(_escalate_if_still_broken())` — scheduled the moment a break is detected,
  wakes once after `MAC_ESCALATION_TIMEOUT_SECONDS` (120s) and checks whether
  `miruro_api:cf_refresher:break_detected_at` is *still* set. If so — neither the Linux attempt
  nor the Mac actually fixed it in time — sends an escalation Telegram alert. Deliberately checks
  the real outcome (is the cookie still broken) rather than "did the Mac acknowledge the
  message" — an ack only proves the message arrived, not that Chrome actually solved the
  challenge (the Linux side failed that exact way once already).

**Setup on the Mac** (this Mac needs to be joined to the same ZeroTier network as the home node
— it needs to reach both Redis and `NOTIFY_RELAY_URL`):
```bash
git clone <this repo> ~/MI-API   # or wherever
cd MI-API
cp .env_example .env   # ONE .env at the repo root, shared with api.py — mac_agent/refresher.py
                        # reads this same file (../. env relative to mac_agent/), not a second one
# edit .env: REDIS_HOST/PORT/PASSWORD (same as the server's), API_KEY (same shared key),
# MIRURO_BASE_URL, NOTIFY_RELAY_URL (home node's ZeroTier address), NODE_ID (e.g. "mac")

cd mac_agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
patchright install chrome   # or rely on an already-installed Google Chrome — channel="chrome"
                             # drives the real installed browser, not patchright's bundled one

# Run it in the foreground once first to confirm it starts cleanly:
python refresher.py

# Then install as a launchd agent (survives reboots/crashes):
# 1. Edit com.mi-api.mac-refresher.plist — replace /REPLACE/WITH/PATH/TO/MI-API with the real
#    absolute path (both occurrences).
cp com.mi-api.mac-refresher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mi-api.mac-refresher.plist
# Logs: /tmp/mi-api-mac-refresher.log and .../  -error.log
# Stop it: launchctl unload ~/Library/LaunchAgents/com.mi-api.mac-refresher.plist
```

Uses a dedicated, throwaway Chrome profile (`mac_agent/.chrome-profile/`, gitignored) rather
than the user's live daily-driver profile — this never conflicts with the user actually using
Chrome at the same time as a refresh runs.

### `windows_agent/` — one-off diagnostic, not wired into the fleet

`windows_agent/test_cookie.py` answers one question: does a given machine's real Chrome reliably
get a `cf_clearance` that works against the actual pipe paths the API needs, not just the
homepage? It solves the Cloudflare challenge (same real-Chrome approach as `mac_agent/`, no
Xvfb needed) and then tests the resulting cookie against the SAME production endpoints —
`/recent-episodes` (pipe path `schedule`) and `/watch` (pipe path `episodes` then `sources`) —
instead of an arbitrary canary. Prints a PASS/FAIL summary. **Does not write to Redis or notify
anyone** — it's a standalone test, used to evaluate whether a residential/cloud-desktop Windows
box is a viable second source of `cf_clearance` before wiring it into the reactive/pub-sub flow
the way `mac_agent/` is. See the run instructions at the bottom of the file itself.

### Security middleware (`secure_api`)

Every non-doc request must pass one of two checks (checked in order):
- Valid `x-api-key` header matching `API_KEY` env var
- `Origin` or `Referer` header that starts with one of the `ALLOWED_ORIGINS`

Doc paths (`/`, `/docs`, `/redoc`, `/openapi.json`) bypass this check entirely.

### ID encoding

Episode IDs returned by the Miruro pipe are base64-encoded. `_translate_id()` decodes a single ID; `_deep_translate()` recursively walks any JSON structure and decodes all IDs. Endpoints that return episode data must call `_deep_translate()` before returning.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ALLOWED_ORIGINS` | localhost variants | Comma-separated CORS + auth whitelist |
| `API_KEY` | `123456` | Auth header value (`x-api-key`) |
| `API_DEBUG` | `False` | `True` renders a styled HTML homepage; `False` renders a minimal page |
| `REDIS_HOST` | `localhost` | Redis host for the `/recent-episodes` cache |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | — | Redis password |
| `CACHE_RECENT_EPISODES_HOURS` | `2` | TTL (hours) for the `/recent-episodes` cache |
| `BLOCKED_EPISODE_PREFIXES` | `` (empty) | Comma-separated episode ID prefixes (part before `:`, e.g. `animepahe`) to strip out of `/episodes` responses — use to hide a provider whose source is broken/hanging upstream, no code change needed |
| `MIRURO_BASE_URL` | — (required) | Base domain for the Miruro pipe, e.g. `https://www.miruro.to`. No hardcoded fallback — update this if Miruro changes domains again |
| `PIPE_USER_AGENT` | `Mozilla/5.0 (Windows NT 10.0; Win64; x64)` | User-Agent sent to the pipe |
| `PIPE_EXTRA_HEADERS` | `{}` | JSON object merged into pipe request headers (e.g. `sec-ch-ua`, `accept`, `cf_clearance`-adjacent headers) — used to adapt to Cloudflare without touching code |
| `CACHE_EPISODES_HOURS` | `1` | TTL (hours) for the `/episodes/{id}` cache |
| `NOTIFY_RELAY_URL` | `` (empty) | Base URL of the home node (`http://10.147.19.131:8848` over ZeroTier), used by cloud nodes to relay Telegram alerts through `POST /internal/notify` when no local Hermes install exists. Leave unset on the home node itself. |
| `NODE_ID` | OS hostname | Human-readable label for this node (e.g. `cloud-1`), appended to Telegram alerts as `[nodo: ...]` so you know which of the 5 nodes actually detected the failure. |

### Deployment targets

- **Vercel**: `vercel.json` maps all routes to `api.py` via the Python runtime (`mangum` adapter is imported for ASGI compatibility).
- **Koyeb/Docker**: `Dockerfile` uses Python 3.11 slim, installs requirements, and starts uvicorn directly.
