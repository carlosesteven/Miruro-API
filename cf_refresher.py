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
import socket
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import redis.asyncio as aioredis
from patchright.async_api import async_playwright

logger = logging.getLogger(__name__)

import httpx

MIRURO_BASE_URL = os.getenv("MIRURO_BASE_URL").rstrip("/")
MIRURO_PIPE_URL = f"{MIRURO_BASE_URL}/api/secure/pipe"
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None

REDIS_KEY_CF_CLEARANCE = "miruro_api:cf_clearance"
REDIS_TTL_SECONDS = 25 * 60  # safety net: if this script stops running, api.py falls back to
                              # its static headers (no cookie) once this expires, rather than
                              # replaying a stale, already-invalid cookie forever.

# Same literal key as api.py's _trigger_reactive_cf_refresh — set there the moment a break is
# first detected, consumed here on successful recovery to log/report the REAL, measured
# break-to-recovery time instead of an estimate.
REDIS_KEY_BREAK_DETECTED_AT = "miruro_api:cf_refresher:break_detected_at"

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

# Same NODE_ID convention as api.py — set per-node in .env, falls back to the OS hostname.
NODE_ID = os.getenv("NODE_ID") or socket.gethostname()


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


# Same two-path check as api.py's _cf_clearance_actually_broken — confirmed live TWICE now that
# "got a cf_clearance from the homepage" is not the same as "the pipe actually accepts it".
# Solving the homepage challenge can succeed while Cloudflare still 403s the real pipe paths this
# app needs (episodes, sources) — verify against both for real before ever calling this a fix.
#
# Also confirmed live: Miruro's pipe responses are cached at Cloudflare's edge (this exact
# anilistId=21 episodes query came back `cf-cache-status: HIT`, `age: 11068` — 3+ hours old). A
# cache HIT never reaches Miruro's origin, so it says nothing about whether the cookie actually
# works. A throwaway random field in the query changes the cache key (confirmed: flips HIT to
# MISS, still 200) without the backend rejecting it — every verification call below is
# cache-busted so it actually reaches the origin. This is scoped to verification only; the real
# cookie this produces is still cached normally by Cloudflare/Redis for real traffic.
_VERIFY_SOURCES_PROVIDER = "ally"
_VERIFY_SOURCES_CATEGORY = "sub"
_VERIFY_SOURCES_ANILIST_ID = 21


def _cache_bust() -> str:
    import random, string
    return "".join(random.choices(string.ascii_lowercase, k=8))


async def _cookie_actually_works(cookie_str: str, headers: dict) -> bool:
    """Replays the SAME two canary queries api.py checks (episodes, then sources) against the
    freshly-solved cookie, over HTTP/2 (confirmed: HTTP/1.1 gets rejected even with an otherwise
    valid cookie — see api.py's _pipe_get). Only a cookie that passes both gets accepted."""
    full_headers = dict(headers)
    full_headers["cookie"] = cookie_str
    full_headers.setdefault("referer", f"{MIRURO_BASE_URL}/")

    async def _raw_call(payload: dict):
        url = f"{MIRURO_PIPE_URL}?e={_encode_pipe_request(payload)}"
        async with httpx.AsyncClient(timeout=10, http2=True) as client:
            res = await client.get(url, headers=full_headers)
        return res

    try:
        episodes_payload = {
            "path": "episodes", "method": "GET",
            "query": {"anilistId": _VERIFY_SOURCES_ANILIST_ID, "_cb": _cache_bust()},
            "body": None, "version": "0.1.0",
        }
        res = await _raw_call(episodes_payload)
        if res.status_code != 200:
            return False
        import base64 as _b64
        import gzip as _gzip
        raw = res.text.strip()
        raw += "=" * (4 - len(raw) % 4)
        episodes_data = json.loads(_gzip.decompress(_b64.urlsafe_b64decode(raw)).decode())

        eps = (
            episodes_data.get("providers", {})
            .get(_VERIFY_SOURCES_PROVIDER, {})
            .get("episodes", {})
            .get(_VERIFY_SOURCES_CATEGORY, [])
        )
        raw_episode_id = eps[0]["id"] if eps else None
        if not raw_episode_id:
            return True  # episodes canary passed and there's nothing else we can check safely

        # Pipe episode IDs come back base64-encoded (Miruro's own encoding, not ours) — decode to
        # plain text first, same as api.py's _translate_id, or we'd be re-encoding an already-
        # encoded string when building the sources query below.
        try:
            padded = raw_episode_id + "=" * (4 - len(raw_episode_id) % 4)
            decoded = _b64.urlsafe_b64decode(padded).decode()
            if ":" in decoded:
                raw_episode_id = decoded
        except Exception:
            pass

        sources_payload = {
            "path": "sources",
            "method": "GET",
            "query": {
                "episodeId": _b64.urlsafe_b64encode(raw_episode_id.encode()).decode().rstrip("="),
                "provider": _VERIFY_SOURCES_PROVIDER,
                "category": _VERIFY_SOURCES_CATEGORY,
                "anilistId": _VERIFY_SOURCES_ANILIST_ID,
                "_cb": _cache_bust(),
            },
            "body": None,
            "version": "0.1.0",
        }
        res2 = await _raw_call(sources_payload)
        return res2.status_code == 200
    except Exception:
        logger.exception("Verification call itself failed")
        return False

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
                    # A cf_clearance cookie can appear mid-challenge and get superseded by a
                    # later, real one (confirmed live on windows_agent: the challenge visibly
                    # ran twice before actually clearing) — don't trust it until the page has
                    # actually navigated off the challenge screen.
                    title = (await page.title()).lower()
                    if "just a moment" not in title:
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

    failure_reason = None
    cookie_str = headers = None
    try:
        cookie_str, headers = await _solve_challenge_and_capture()
        if not await _cookie_actually_works(cookie_str, headers):
            failure_reason = (
                "resolvió el challenge de la home pero la cookie no funciona contra el pipe "
                "real (episodes/sources) — Cloudflare está evaluando esas rutas aparte"
            )
    except Exception as e:
        failure_reason = str(e)

    if failure_reason:
        print(f"[cf_refresher] FAILED: {failure_reason}", file=sys.stderr)

        ttl = await _current_ttl()
        vigencia = f"la cookie actual vence en ~{ttl // 60} min" if ttl and ttl > 0 else "no hay ninguna cookie vigente en este momento"

        notify_telegram(
            f"⚠️ MI-API [nodo: {NODE_ID}]: cf_refresher no logró una cookie que funcione de "
            f"verdad ({failure_reason}). {vigencia}. Generá un cf_clearance nuevo desde tu "
            "equipo (misma IP) y pasámelo para que lo aplique."
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
        break_detected_at = await r.get(REDIS_KEY_BREAK_DETECTED_AT)
        if break_detected_at:
            await r.delete(REDIS_KEY_BREAK_DETECTED_AT)
    finally:
        await r.aclose()

    print(f"[cf_refresher] OK — refreshed cf_clearance, {len(headers)} headers captured")

    # Only fire a "recovered" alert when this refresh actually closed out a detected outage
    # (break_detected_at was set) — a routine manual/proactive refresh with nothing broken
    # shouldn't claim a "recovery" that never happened.
    if break_detected_at:
        elapsed = time.time() - float(break_detected_at)
        print(f"[cf_refresher] RECOVERY TIME: {elapsed:.1f}s (medido, no estimado)")
        notify_telegram(
            f"✅ MI-API [nodo: {NODE_ID}]: recuperado. Tiempo real roto→arreglado: "
            f"{elapsed:.0f}s."
        )


if __name__ == "__main__":
    asyncio.run(main())
