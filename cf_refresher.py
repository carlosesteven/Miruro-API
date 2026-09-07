"""Refreshes the Cloudflare `cf_clearance` cookie used to reach the Miruro pipe.

Miruro's Cloudflare zone started serving an interactive JS challenge ("Just a moment...") to
every request from this server, including ones made with curl_cffi's browser TLS impersonation
— impersonating the TLS/HTTP fingerprint isn't enough, the challenge itself has to be solved by
something that looks like a real browser. A plain headless Chromium (via Playwright, even with
patchright's stealth patches) gets stuck on the challenge forever; a NON-headless Chromium run
under Xvfb solves it in a few seconds. See SESSION_LOG.md for the investigation.

This script drives that non-headless browser, waits for Cloudflare to hand out `cf_clearance`,
captures the exact request headers the browser used to get it (sec-ch-ua/user-agent/etc. have to
match byte-for-byte — mixing them with a different header set re-triggers the challenge), and
publishes {cookie, headers} as JSON to the Redis key api.py reads on every pipe request
(REDIS_KEY_CF_CLEARANCE = "miruro_api:cf_clearance").

Must run with a display: `xvfb-run -a python cf_refresher.py` (systemd unit does this).
Run on a timer well inside Cloudflare's clearance lifetime — see mi-api-cf-refresh.timer.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import redis.asyncio as aioredis
from patchright.async_api import async_playwright

logger = logging.getLogger(__name__)

MIRURO_BASE_URL = os.getenv("MIRURO_BASE_URL").rstrip("/")
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None

REDIS_KEY_CF_CLEARANCE = "miruro_api:cf_clearance"
REDIS_TTL_SECONDS = 25 * 60  # safety net: if this script stops running, api.py falls back to
                              # its static headers (no cookie) once this expires, rather than
                              # replaying a stale, already-invalid cookie forever.

# Skip launching the browser entirely when the cached cookie still has plenty of life left.
# The timer runs every 15 min; only actually refresh once we're within this margin of the
# 25 min TTL — cuts real Chromium/Cloudflare-challenge runs from ~96/day down to whenever a
# refresh is actually needed, at the cost of one extra timer tick of margin before expiry.
MIN_TTL_BEFORE_REFRESH_SECONDS = 10 * 60

# No debounce on this alert — this is a critical service with apps depending on it, and the
# user explicitly wants a message every time this fires until it's fixed. In practice that's
# capped at once/minute anyway: api.py only calls `cf_refresher.py --force` (which is what hits
# this failure path) once per CF_REFRESHER_TRIGGER_LOCK_TTL (60s) no matter how many requests
# are failing concurrently.
HERMES_BIN = "/home/carlos-esteven/.hermes/hermes-agent/venv/bin/hermes"

# Same 5-node deployment as api.py (this home machine + 4 cloud nodes) — only this one has
# Hermes installed locally. See NOTIFY_RELAY_URL in api.py for the full rationale; unset here,
# set to this machine's ZeroTier address on the cloud nodes' .env.
NOTIFY_RELAY_URL = os.getenv("NOTIFY_RELAY_URL", "").rstrip("/")
API_KEY = os.getenv("API_KEY")


def notify_telegram(message: str) -> None:
    """Best-effort — a failed notification must never crash the refresher."""
    if os.path.exists(HERMES_BIN):
        try:
            result = subprocess.run(
                [HERMES_BIN, "send", "--to", "telegram", "-q", message],
                timeout=20,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.warning("hermes send devolvió %s: %s", result.returncode, result.stderr.strip())
        except Exception:
            logger.exception("Fallo notificando por Telegram vía hermes send")
        return

    if not NOTIFY_RELAY_URL:
        return

    try:
        import httpx
        httpx.post(
            f"{NOTIFY_RELAY_URL}/internal/notify",
            json={"message": message},
            headers={"x-api-key": API_KEY},
            timeout=10,
        )
    except Exception:
        logger.exception("Fallo notificando por Telegram vía el relay")

CHALLENGE_TIMEOUT_SECONDS = 45
POLL_INTERVAL_SECONDS = 2

# Must match api.py's _encode_pipe_request exactly (plain base64 of the JSON, NOT gzipped —
# only pipe *responses* are gzip-compressed, not requests).
def _encode_pipe_request(payload: dict) -> str:
    import base64 as _b64
    return _b64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


_VERIFY_QUERY = _encode_pipe_request(
    {"path": "episodes", "method": "GET", "query": {"anilistId": 21}, "body": None, "version": "0.1.0"}
)

# Headers captured from an actual same-origin request made by the browser once cleared.
# These are the ones cf_clearance validation cares about; anything not in this set (like
# content-length) is request-specific and shouldn't be replayed.
CAPTURED_HEADER_NAMES = {
    "accept", "accept-language", "cache-control", "pragma", "priority", "referer",
    "sec-ch-ua", "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list", "sec-ch-ua-mobile", "sec-ch-ua-model",
    "sec-ch-ua-platform", "sec-ch-ua-platform-version",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "user-agent",
}


async def _solve_challenge_and_capture():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()

            await page.goto(f"{MIRURO_BASE_URL}/", timeout=30000, wait_until="domcontentloaded")

            cf_clearance = None
            deadline = time.monotonic() + CHALLENGE_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                cookies = await context.cookies(MIRURO_BASE_URL)
                match = next((c for c in cookies if c["name"] == "cf_clearance"), None)
                if match:
                    cf_clearance = match["value"]
                    break
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

            if not cf_clearance:
                raise RuntimeError(
                    f"cf_clearance never showed up after {CHALLENGE_TIMEOUT_SECONDS}s — "
                    "Cloudflare may have escalated to a harder challenge (interactive Turnstile)."
                )

            # Cloudflare grants the cookie and then navigates the challenge page to the real
            # page — let that settle before we drive a same-origin fetch, or the evaluate()
            # below races that navigation and blows up with "Execution context was destroyed".
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # A real top-level navigation's headers (sec-fetch-dest: document, no
            # sec-fetch-site) don't match what api.py replays (an XHR-style GET), so capture
            # headers from an actual same-origin fetch() instead, fired after things settle.
            captured_request_headers = {}

            def on_request(request):
                if request.url.startswith(MIRURO_BASE_URL):
                    captured_request_headers.update(request.headers)

            page.on("request", on_request)
            for attempt in range(3):
                try:
                    await page.evaluate(
                        "() => fetch(location.origin + '/', {cache: 'no-store'}).then(r => r.status)"
                    )
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1.5)

            if not captured_request_headers:
                raise RuntimeError("cleared the challenge but never captured a request's headers")

            all_cookies = await context.cookies(MIRURO_BASE_URL)
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in all_cookies)

            headers = {
                k.lower(): v
                for k, v in captured_request_headers.items()
                if k.lower() in CAPTURED_HEADER_NAMES
            }
            return cookie_str, headers
        finally:
            await browser.close()


async def _current_ttl() -> int:
    r = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
    )
    try:
        return await r.ttl(REDIS_KEY_CF_CLEARANCE)
    finally:
        await r.aclose()


async def main():
    force = "--force" in sys.argv
    ttl = await _current_ttl()
    if not force and ttl and ttl > MIN_TTL_BEFORE_REFRESH_SECONDS:
        print(f"[cf_refresher] SKIP — cookie still has {ttl}s left (> {MIN_TTL_BEFORE_REFRESH_SECONDS}s margin)")
        return

    try:
        cookie_str, headers = await _solve_challenge_and_capture()
    except Exception as e:
        print(f"[cf_refresher] FAILED: {e}", file=sys.stderr)

        ttl = await _current_ttl()
        vigencia = f"la cookie actual vence en ~{ttl // 60} min" if ttl and ttl > 0 else "no hay ninguna cookie vigente en este momento"

        notify_telegram(
            "⚠️ MI-API: cf_refresher no pudo resolver el challenge de Cloudflare de Miruro "
            f"({e}). {vigencia}. Generá un cf_clearance nuevo desde tu equipo (misma IP) y "
            "pasámelo para que lo aplique."
        )
        sys.exit(1)

    payload = {
        "cookie": cookie_str,
        "headers": headers,
        "updated_at": int(time.time()),
        "source": "cf_refresher.py",
    }

    r = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
    )
    try:
        await r.set(REDIS_KEY_CF_CLEARANCE, json.dumps(payload), ex=REDIS_TTL_SECONDS)
    finally:
        await r.aclose()

    print(f"[cf_refresher] OK — refreshed cf_clearance, {len(headers)} headers captured")


if __name__ == "__main__":
    asyncio.run(main())
