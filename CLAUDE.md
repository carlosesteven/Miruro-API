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
(reachable from the cloud nodes over ZeroTier at a private address — see `NOTIFY_RELAY_URL`
below). Only this home machine has Hermes (a personal agent framework) installed, which is how Telegram alerts get
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
   forever. Beyond that, **the browser must have ZERO automation attached while the challenge
   resolves** — confirmed live: launching Chrome with Playwright/patchright controlling it from
   the first navigation (CDP active before the page even loads) either never cleared the
   challenge, or cleared it with an incomplete header capture that made the resulting cookie
   fail live pipe calls anyway. `cf_refresher.py`/`mac_agent/refresher.py` now launch the browser
   as a **raw subprocess** (`--remote-debugging-port` open but nothing connected), wait
   `NAKED_LAUNCH_WAIT_SECONDS` (default 20, under **Xvfb** on Linux via `xvfb-run -a`) completely
   untouched, and only THEN attach via `connect_over_cdp` to pull the cookie. Header capture also
   has to merge two CDP events — `Network.requestWillBeSent` (has `sec-ch-ua-*`/`user-agent`) and
   `Network.requestWillBeSentExtraInfo` (has `sec-fetch-*`/`cache-control`/`pragma`/`priority`,
   which Chromium always attaches but Playwright's simple `request.headers()` doesn't expose) —
   using only the first one was silently producing incomplete, non-working cookies. Publishes
   `{cookie, headers}` as JSON to the Redis key `miruro_api:cf_clearance:{FALLBACK_TOPIC}`
   (`_get_pipe_headers()` reads it, cached in-process for `CF_CLEARANCE_LOCAL_CACHE_SECONDS`).
   The cookie is **not** tied to the requesting IP (verified: a cookie solved on one device
   works fine replayed from this server) — it's tied to the header set (`sec-ch-ua`/`user-agent`/
   etc.) matching exactly what Cloudflare saw when it was issued. That's *why* sharing one cookie
   across every node used to work at all technically — but don't read that as "one shared cookie
   is fine": see the `FALLBACK_TOPIC` isolation note below for why every group now has its own key
   regardless, and why this is not up for debate again.
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
a local Hermes binary first (path from the `HERMES_BIN_PATH` env var, set only on the home
node's own `.env` — never hardcoded in code); if it's unset/missing (any cloud node), they POST
`{"message": ...}` to `f"{NOTIFY_RELAY_URL}/internal/notify"` instead, authenticated with this
deployment's own `API_KEY`. `POST /internal/notify` (in `api.py`) is what actually calls Hermes
on the receiving end — it's a normal endpoint (not in the auth-bypass list), so it's protected
by the same `x-api-key` check as everything else. Leave `NOTIFY_RELAY_URL` unset on the home
node; set it to `http://<home-node-zerotier-ip>:8848` on the 4 cloud nodes.

### `cf_refresher.py` — ONE script, any OS, four modes

Solves the Cloudflare challenge and refreshes `miruro_api:cf_clearance:{FALLBACK_TOPIC}`. Runs **unchanged** on
this home server, a Mac, a Windows box, or any extra Linux/Ubuntu machine you add later — it
detects the OS at runtime (`_find_chrome_path`) and finds the right Chrome binary; only the
`.env` differs per machine (`NODE_ID`, `NOTIFY_RELAY_URL`, etc — same one `.env` as `api.py`,
never a separate copy per machine). There used to be separate `mac_agent/`/`windows_agent/`
directories with near-duplicate code — collapsed into this one file once it became clear the
only real per-OS difference is "how do I find/launch a bare Chrome", not the surrounding logic.

**Modes** (CLI args):
| Mode | What it does |
|---|---|
| *(none)* | One-shot: skip if the cached cookie still has plenty of TTL left, else refresh. This is what `api.py`'s reactive trigger calls. |
| `--force` | One-shot, skip the TTL check — always attempt. |
| `--listen` | Run forever as an active fallback node: Redis Pub/Sub (instant reaction) + a periodic poll (durable fallback for whenever this machine was asleep/offline when the trigger was published). Deploy this on any extra machine you want acting as a second/third/etc. `cf_clearance` source. |
| `--dry-run` | Solve + verify only — prints PASS/FAIL against the real pipe endpoints (`episodes` then `sources`, cache-busted). Does **not** write to Redis or notify anyone. Use this to test whether a machine's IP is even viable before deciding to run it with `--listen`. |

**Why it needs to exist at all:** headless Chromium (plain Playwright, and `patchright`'s
stealth fork) gets stuck on the challenge forever. Beyond that, **the browser must have ZERO
automation attached while the challenge resolves** — confirmed live: launching Chrome with
Playwright/patchright controlling it from the first navigation (CDP active before the page even
loads) either never cleared the challenge, or cleared it with an incomplete header capture that
made the resulting cookie fail live pipe calls anyway. `_solve_challenge_and_capture` launches
the browser as a **raw subprocess** (`--remote-debugging-port` open but nothing connected), waits
`NAKED_LAUNCH_WAIT_SECONDS` (default 20) completely untouched, and only THEN attaches via
`connect_over_cdp` to pull the cookie. Header capture also has to merge two CDP events —
`Network.requestWillBeSent` (has `sec-ch-ua-*`/`user-agent`) and
`Network.requestWillBeSentExtraInfo` (has `sec-fetch-*`/`cache-control`/`pragma`/`priority`,
which Chromium always attaches but Playwright's simple `request.headers()` doesn't expose) —
using only the first one was silently producing incomplete, non-working cookies.

**Why an extra machine at all:** even this home server's own Xvfb+patchright automation
eventually gets distrusted by Cloudflare — it's the same IP auto-solving hundreds of challenges
a day, which is exactly the pattern a bot-management WAF learns to flag. A residential Mac (or
any machine on a different, less-flagged IP) running a real Chrome, with a normal mixed traffic
history, gets trusted far more.

**Second-tier trigger model** (server side lives in `api.py`'s `_trigger_reactive_cf_refresh`,
fired alongside — not after — the home node's own one-shot attempt):
- `SET miruro_api:need_mac_refresh:{FALLBACK_TOPIC} EX 600` — a durable flag, polled by every
  `--listen` node in the same topic every `FALLBACK_POLL_INTERVAL_SECONDS` (default 30 min) as
  the fallback for whenever that machine was asleep/offline at the moment of the real event.
  (Key name is historical — "mac" isn't literal, it means "whichever fallback node answers for
  this topic".)
- `PUBLISH miruro_api:mac_refresh_channel:{FALLBACK_TOPIC}` — instant reaction whenever a
  `--listen` node in the same topic's listener happens to already be connected. Redis Pub/Sub
  does **not** queue messages for offline subscribers, so this is the fast path, never the only
  path. Multiple `--listen` nodes in the same topic can be subscribed at once — a single trigger
  fans out to all of them in that topic, and whichever actually produces a working cookie first
  wins (harmless if more than one succeeds within the same topic; they just overwrite the same
  Redis key with an equally-valid cookie).
- `asyncio.create_task(_escalate_if_still_broken())` — scheduled the moment a break is detected,
  wakes once after `MAC_ESCALATION_TIMEOUT_SECONDS` (120s) and checks whether
  `miruro_api:cf_refresher:break_detected_at` is *still* set. If so — nothing anywhere fixed it
  in time — sends an escalation Telegram alert. Deliberately checks the real outcome (is the
  cookie still broken) rather than "did some node acknowledge the message" — an ack only proves
  the message arrived, not that Chrome actually solved the challenge.

**`FALLBACK_TOPIC` isolates the cookie itself, not just who gets woken up — this is not
optional, and it must never be walked back.** Every group (`{production node(s)} + {--listen
fallback node(s)}` sharing one `FALLBACK_TOPIC` value) has its own, completely independent
`cf_clearance` cookie at `miruro_api:cf_clearance:{FALLBACK_TOPIC}` — as well as its own
need/refresh flag and pub/sub channel, both shown above. **Do not go back to one shared
`cf_clearance` key across groups, even though a cookie is technically valid replayed from any
IP** (see the header-set-not-IP note above) — that was the original design here, and it was
wrong: sharing one cookie coupled every group's fate together. A break in one group's cookie
made every node reading that same key 403 at once (regardless of group), and — because each
node also holds its own few-second in-process copy (`CF_CLEARANCE_LOCAL_CACHE_SECONDS`) — two
completely unrelated fallback groups could appear to "trigger each other" seconds apart. That
was never one group waking the other (the topic-scoped trigger channels were already isolated
even before this fix); it was two independent nodes racing against the *same* shared cookie,
each hitting its own stale local copy at a slightly different moment. Confirmed live 2026-09-07
(see `SESSION_LOG.md`) and fixed by giving every group its own cookie key, full stop — a group
must live or die entirely on its own cookie, with zero coupling to any other group's.

**Setup on a new machine** (Mac, Windows, or another Linux box — needs to be joined to the same
ZeroTier network as the home node, to reach both Redis and `NOTIFY_RELAY_URL`):
```bash
git clone <this repo> ~/MI-API   # or wherever
cd MI-API
cp .env_example .env
# edit .env: REDIS_HOST/PORT/PASSWORD (same as the server's), API_KEY (same shared key),
# MIRURO_BASE_URL, NOTIFY_RELAY_URL (home node's ZeroTier address), NODE_ID (e.g. "mac", "cloud-ubuntu-2")

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
patchright install chromium       # Linux only — Mac/Windows use the real installed Chrome instead

# Test it works at all before wiring it into the fleet:
python cf_refresher.py --dry-run   # Linux: xvfb-run -a python cf_refresher.py --dry-run

# Run it as the active fallback node:
python cf_refresher.py --listen    # Linux: xvfb-run -a python cf_refresher.py --listen
```

Keep it running persistently:
- **Linux**: `mi-api-fallback-agent.service` (systemd) — edit the placeholder paths/user, then
  `sudo cp` it to `/etc/systemd/system/`, `daemon-reload`, `enable --now`.
- **Mac**: `com.mi-api.fallback-agent.plist` (launchd) — edit the placeholder paths, then
  `cp` to `~/Library/LaunchAgents/`, `launchctl load`.
- **Windows**: Task Scheduler, "run at log on", pointed at `venv\Scripts\python.exe
  cf_refresher.py --listen` with the repo as the working directory.

Uses a dedicated, throwaway Chrome profile (`.chrome-profile/`, gitignored) rather than the
user's live daily-driver profile — this never conflicts with them actually using Chrome at the
same time as a refresh runs.

### `mi_api_mcp.py` — MCP server for manual diagnosis (home node only)

MCP server (stdio, `mcp.server.fastmcp.FastMCP`) exposing `estado_cf_clearance()` and
`refrescar_cf_clearance()` for manual diagnosis/triggering from Hermes chat. Registered in
`~/.hermes/config.yaml` under `mcp_servers.mi_api` (home node only — that's where Hermes runs).
**Pin `mcp[cli]==1.28.1` in requirements.txt** — `mcp` v2.x renamed `FastMCP` to `MCPServer` and
breaks this import; all the other MCP servers on this machine (`camaras_ip`, `jkanime_relator`,
etc.) are on 1.28.1 too, for the same reason.

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
| `NOTIFY_RELAY_URL` | `` (empty) | Base URL of the home node (its ZeroTier address, e.g. `http://10.x.x.x:8848`), used by cloud nodes to relay Telegram alerts through `POST /internal/notify` when no local Hermes install exists. Leave unset on the home node itself. |
| `HERMES_BIN_PATH` | `` (empty) | Absolute path to the local Hermes CLI binary. Only set on the home node's own `.env`; unset/missing anywhere else falls through to `NOTIFY_RELAY_URL`. |
| `NODE_ID` | OS hostname | Human-readable label for this node (e.g. `cloud-1`), appended to Telegram alerts as `[nodo: ...]` so you know which of the 5 nodes actually detected the failure. |

### Deployment targets

- **Vercel**: `vercel.json` maps all routes to `api.py` via the Python runtime (`mangum` adapter is imported for ASGI compatibility).
- **Koyeb/Docker**: `Dockerfile` uses Python 3.11 slim, installs requirements, and starts uvicorn directly.
