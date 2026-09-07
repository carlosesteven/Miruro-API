"""Mac-side second-tier fallback for refreshing MI-API's Cloudflare `cf_clearance` cookie.

Why this exists: the home server's own automation (cf_refresher.py, patchright+Xvfb) is
increasingly distrusted by Cloudflare — it's the same IP auto-solving challenges hundreds of
times a day, which is exactly the pattern a bot-management WAF learns to flag. A residential
Mac, running its actual installed Chrome, with a normal human's mixed traffic history, gets
trusted far more. Manual cookies pasted from this same Mac worked every single time today; this
automates that same act instead of asking a human to open DevTools and copy 20 headers by hand.

Trigger model (see api.py's _trigger_reactive_cf_refresh — this is its second-tier fallback,
fired ALONGSIDE the server's own Linux attempt, not after it fails):
  - Redis Pub/Sub (`miruro_api:mac_refresh_channel`) for an instant reaction whenever this
    listener happens to be connected. Pub/Sub is fire-and-forget — Redis does NOT queue
    messages for offline subscribers — so this is the fast path, not the only path.
  - A persistent flag (`miruro_api:need_mac_refresh`, TTL 10min) polled every
    POLL_INTERVAL_SECONDS as the durable fallback for whenever this Mac was asleep, off, or
    disconnected the moment the server actually published.

On success: writes {cookie, headers} to the same Redis key api.py reads
(miruro_api:cf_clearance), and — if this recovery closed out a break the server detected
(miruro_api:cf_refresher:break_detected_at) — reports the real measured recovery time, exactly
like cf_refresher.py does for the Linux path.

Run via launchd (see com.mi-api.mac-refresher.plist) so it survives reboots and restarts on
crash — see README section in CLAUDE.md (root of MI-API repo) for setup steps.
"""
import asyncio
import base64
import gzip
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Same .env as the main repo (repo root, one directory up) — not a separate mac_agent/.env.
# This is just a git checkout of the same repo, so it's the same file whether api.py or this
# script reads it.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import httpx
import redis.asyncio as aioredis
from patchright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [mac_agent] %(message)s")
logger = logging.getLogger(__name__)

MIRURO_BASE_URL = os.getenv("MIRURO_BASE_URL", "").rstrip("/")
MIRURO_PIPE_URL = f"{MIRURO_BASE_URL}/api/secure/pipe"
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None

# Same literal keys as api.py — this is the other end of that contract.
REDIS_KEY_CF_CLEARANCE = "miruro_api:cf_clearance"
REDIS_KEY_BREAK_DETECTED_AT = "miruro_api:cf_refresher:break_detected_at"
REDIS_KEY_NEED_MAC_REFRESH = "miruro_api:need_mac_refresh"
REDIS_CHANNEL_MAC_REFRESH = "miruro_api:mac_refresh_channel"
REDIS_TTL_SECONDS = 25 * 60

POLL_INTERVAL_SECONDS = int(os.getenv("MAC_AGENT_POLL_INTERVAL_SECONDS", str(30 * 60)))
CHALLENGE_TIMEOUT_SECONDS = 45
# Cloudflare's challenge page title, localized by the browser's Accept-Language — seen both as
# English ("Just a moment...") and Spanish ("Un momento...") live.
_CHALLENGE_TITLE_MARKERS = ("just a moment", "un momento")

NOTIFY_RELAY_URL = os.getenv("NOTIFY_RELAY_URL", "").rstrip("/")
API_KEY = os.getenv("API_KEY")
NODE_ID = os.getenv("NODE_ID", "mac")

CAPTURED_HEADER_NAMES = {
    "accept", "accept-language", "cache-control", "pragma", "priority", "referer",
    "sec-ch-ua", "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list", "sec-ch-ua-mobile", "sec-ch-ua-model",
    "sec-ch-ua-platform", "sec-ch-ua-platform-version",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "user-agent",
}


def notify_telegram(message: str) -> None:
    """This Mac has no local Hermes install — always relays through the home node's
    /internal/notify, same mechanism the 4 cloud nodes use. Best-effort."""
    if not NOTIFY_RELAY_URL:
        logger.warning("NOTIFY_RELAY_URL not set — can't send Telegram alert: %s", message)
        return
    try:
        httpx.post(
            f"{NOTIFY_RELAY_URL}/internal/notify",
            json={"message": message},
            headers={"x-api-key": API_KEY},
            timeout=10,
        )
    except Exception:
        logger.exception("Failed to relay Telegram notification")


# Same two-path check as api.py's _cf_clearance_actually_broken / cf_refresher.py's
# _cookie_actually_works — confirmed live: solving the homepage challenge can succeed while
# Cloudflare still 403s the real pipe paths this app needs (episodes, sources). This Mac's first
# real run did exactly that (got a cookie good for episodes only) before this check existed.
#
# Also cache-busted: Miruro's pipe responses are cached at Cloudflare's edge (confirmed live,
# cf-cache-status: HIT, age in the hours, for this exact episodes query) — a cache HIT never
# reaches the origin, so an un-busted check could "pass" a dead cookie. A throwaway random field
# changes the cache key (confirmed: flips HIT->MISS, still 200) without the backend rejecting it.
_VERIFY_PROVIDER = "ally"
_VERIFY_CATEGORY = "sub"
_VERIFY_ANILIST_ID = 21


def _cache_bust() -> str:
    import random, string
    return "".join(random.choices(string.ascii_lowercase, k=8))


def _translate_id(encoded_id: str) -> str:
    """Pipe episode IDs come back base64-encoded (Miruro's own encoding) — decode to plain text,
    same as api.py's _translate_id."""
    try:
        padded = encoded_id + "=" * (4 - len(encoded_id) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        return decoded if ":" in decoded else encoded_id
    except Exception:
        return encoded_id


async def _cookie_actually_works(cookie_str: str, headers: dict) -> bool:
    """Replays the same two canary queries api.py/cf_refresher.py check (episodes, then sources)
    against the freshly-solved cookie, over HTTP/2. Only a cookie that passes both gets accepted
    — anything less gets treated exactly like the challenge never resolved at all."""
    full_headers = dict(headers)
    full_headers["cookie"] = cookie_str
    full_headers.setdefault("referer", f"{MIRURO_BASE_URL}/")

    async def _raw_call(payload: dict):
        url = f"{MIRURO_PIPE_URL}?e={base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')}"
        async with httpx.AsyncClient(timeout=10, http2=True) as client:
            return await client.get(url, headers=full_headers)

    try:
        episodes_payload = {
            "path": "episodes", "method": "GET",
            "query": {"anilistId": _VERIFY_ANILIST_ID, "_cb": _cache_bust()},
            "body": None, "version": "0.1.0",
        }
        res = await _raw_call(episodes_payload)
        if res.status_code != 200:
            return False

        raw = res.text.strip()
        raw += "=" * (4 - len(raw) % 4)
        episodes_data = json.loads(gzip.decompress(base64.urlsafe_b64decode(raw)).decode())

        eps = (
            episodes_data.get("providers", {})
            .get(_VERIFY_PROVIDER, {})
            .get("episodes", {})
            .get(_VERIFY_CATEGORY, [])
        )
        raw_episode_id = _translate_id(eps[0]["id"]) if eps else None
        if not raw_episode_id:
            return True  # episodes canary passed and there's nothing else we can check safely

        sources_payload = {
            "path": "sources",
            "method": "GET",
            "query": {
                "episodeId": base64.urlsafe_b64encode(raw_episode_id.encode()).decode().rstrip("="),
                "provider": _VERIFY_PROVIDER,
                "category": _VERIFY_CATEGORY,
                "anilistId": _VERIFY_ANILIST_ID,
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


async def _solve_challenge_and_capture():
    """Same approach as cf_refresher.py's homepage-solve, adapted for a real Mac: drives the
    ACTUAL installed Google Chrome (channel="chrome", not the Playwright-bundled Chromium) in a
    normal foreground window — no Xvfb needed, this machine already has a real display. Uses a
    dedicated, throwaway profile directory rather than the user's live daily-driver Chrome
    profile, so this never conflicts with them actually using Chrome at the same time."""
    profile_dir = str(Path(__file__).resolve().parent / ".chrome-profile")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            profile_dir,
            channel="chrome",
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()

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
                    if not any(marker in title for marker in _CHALLENGE_TITLE_MARKERS):
                        cf_clearance = match["value"]
                        break
                await asyncio.sleep(2)

            if not cf_clearance:
                raise RuntimeError(
                    f"cf_clearance never showed up after {CHALLENGE_TIMEOUT_SECONDS}s"
                )

            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

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
            await context.close()


async def _current_break_detected_at(r) -> Optional[str]:
    return await r.get(REDIS_KEY_BREAK_DETECTED_AT)


_refresh_lock = asyncio.Lock()


async def run_refresh_once():
    """Solves the challenge and pushes the result. Guarded by an in-process lock so a pub/sub
    message arriving mid-refresh (or overlapping with the poll loop) doesn't launch a second
    Chrome at the same time."""
    if _refresh_lock.locked():
        logger.info("Refresh already in progress, skipping this trigger")
        return

    async with _refresh_lock:
        logger.info("Starting Chrome-based refresh...")
        r = aioredis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
        )
        try:
            failure_reason = None
            cookie_str = headers = None
            try:
                cookie_str, headers = await _solve_challenge_and_capture()
                if not await _cookie_actually_works(cookie_str, headers):
                    failure_reason = (
                        "resolvió el challenge pero la cookie no funciona contra el pipe real "
                        "(episodes/sources) — mismo hueco que ya se vio con este Mac una vez"
                    )
            except Exception as e:
                failure_reason = str(e)

            if failure_reason:
                logger.error("FAILED: %s", failure_reason)
                notify_telegram(
                    f"⚠️ MI-API [nodo: {NODE_ID}]: tampoco logré una cookie que funcione de "
                    f"verdad desde el Mac ({failure_reason}). Necesito una cf_clearance manual."
                )
                return

            payload = {
                "cookie": cookie_str,
                "headers": headers,
                "updated_at": int(time.time()),
                "source": "mac_agent",
            }
            await r.set(REDIS_KEY_CF_CLEARANCE, json.dumps(payload), ex=REDIS_TTL_SECONDS)
            await r.delete(REDIS_KEY_NEED_MAC_REFRESH)

            break_detected_at = await _current_break_detected_at(r)
            if break_detected_at:
                await r.delete(REDIS_KEY_BREAK_DETECTED_AT)
                elapsed = time.time() - float(break_detected_at)
                logger.info("RECOVERY TIME (via Mac): %.1fs", elapsed)
                notify_telegram(
                    f"✅ MI-API [nodo: {NODE_ID}]: recuperado desde el Mac. Tiempo real "
                    f"roto→arreglado: {elapsed:.0f}s."
                )
            else:
                logger.info("Refreshed cf_clearance (no outage was pending)")
        finally:
            await r.aclose()


async def _poll_loop():
    """Durable fallback: catches the case where a pub/sub 'refresh' message was published while
    this Mac was asleep/offline/disconnected and therefore never received."""
    r = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
    )
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                needed = await r.get(REDIS_KEY_NEED_MAC_REFRESH)
            except Exception:
                logger.exception("Poll check failed (Redis unreachable?)")
                continue
            if needed:
                logger.info("Poll found need_mac_refresh flag set — running refresh")
                await run_refresh_once()
    finally:
        await r.aclose()


async def _pubsub_loop():
    """Fast path: instant reaction whenever this listener is actually connected at publish time."""
    r = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
    )
    pubsub = r.pubsub()
    await pubsub.subscribe(REDIS_CHANNEL_MAC_REFRESH)
    logger.info("Listening on %s for instant refresh requests", REDIS_CHANNEL_MAC_REFRESH)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            logger.info("Received instant refresh request via pub/sub")
            await run_refresh_once()
    finally:
        await pubsub.aclose()
        await r.aclose()


async def main():
    if not MIRURO_BASE_URL:
        print("MIRURO_BASE_URL not set — check the .env at the repo root", file=sys.stderr)
        sys.exit(1)

    logger.info(
        "mac_agent starting — poll every %ss, instant pub/sub also active", POLL_INTERVAL_SECONDS
    )
    await asyncio.gather(_pubsub_loop(), _poll_loop())


if __name__ == "__main__":
    asyncio.run(main())
