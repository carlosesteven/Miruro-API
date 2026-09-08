"""Refreshes the Cloudflare `cf_clearance` cookie used to reach the Miruro pipe.

Miruro's Cloudflare zone serves an interactive JS challenge ("Just a moment.../Un momento...")
that has to be solved by something that looks like a real, un-instrumented browser — headless
Chromium gets stuck on it forever, and even a real Chrome gets a cookie that silently fails
live pipe calls if Playwright/patchright control it from the first navigation (confirmed live,
see SESSION_LOG.md 2026-09-07): the fix is launching the browser as a bare subprocess with
nothing attached, letting the challenge clear on its own, and only THEN attaching via
connect_over_cdp to pull the cookie + the complete set of real request headers.

ONE script, any machine: this same file runs unchanged on this home server (Linux/Xvfb), a Mac,
a Windows box, or any other Linux/Ubuntu box you add later — it detects the OS at runtime and
finds the right Chrome binary (see _find_chrome_path). Only the .env differs per machine
(NODE_ID, NOTIFY_RELAY_URL, etc.) — see CLAUDE.md for the full setup per platform.

Modes (CLI args):
  (none)      One-shot: skip if the cached cookie still has plenty of TTL left, else refresh.
              This is what api.py's reactive trigger calls.
  --force     One-shot, skip the TTL check — always attempt.
  --listen    Run forever as an active fallback node: Redis Pub/Sub (instant reaction) + a
              periodic poll (durable fallback for whenever this machine was asleep/offline when
              the trigger was published). Deploy this on any extra machine you want acting as a
              second/third/etc. cf_clearance source.
  --dry-run   Solve + verify only — prints PASS/FAIL against the real pipe endpoints, does NOT
              write to Redis or notify anyone. Use this to test whether a given machine's IP is
              even viable before deciding to run it as a --listen node.

On Linux, run under a display: `xvfb-run -a python cf_refresher.py [mode]`.
"""
import asyncio
import base64
import gzip
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import httpx
import redis.asyncio as aioredis
from patchright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [cf_refresher] %(message)s")
logger = logging.getLogger(__name__)

MIRURO_BASE_URL = os.getenv("MIRURO_BASE_URL").rstrip("/")
MIRURO_PIPE_URL = f"{MIRURO_BASE_URL}/api/secure/pipe"
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None

# FALLBACK_TOPIC segments an entire group end to end — the cookie itself, not just who gets
# woken up. Set the SAME FALLBACK_TOPIC on api.py's own .env and on every --listen node meant to
# answer for it, and a DIFFERENT one for an isolated group — e.g. api.py (or whichever production
# node) set to "groupA" shares a cookie only with the --listen nodes also set to "groupA" (say,
# one Ubuntu + the Mac), completely independent from "groupB" (a second Ubuntu + the Windows
# box) — a break, a fix, or a cookie in one group has zero effect on the other. Defaults to
# "default" so a single-group deployment (the common case) needs nothing set.
FALLBACK_TOPIC = os.getenv("FALLBACK_TOPIC", "default")
REDIS_KEY_CF_CLEARANCE = f"miruro_api:cf_clearance:{FALLBACK_TOPIC}"
REDIS_TTL_SECONDS = 25 * 60  # safety net: if nothing refreshes it in time, api.py falls back to
                              # its static headers (no cookie) once this expires, rather than
                              # replaying a stale, already-invalid cookie forever.

# Same literal key as api.py's _trigger_reactive_cf_refresh — set there the moment a break is
# first detected, consumed here on successful recovery to log/report the REAL, measured
# break-to-recovery time instead of an estimate.
REDIS_KEY_BREAK_DETECTED_AT = "miruro_api:cf_refresher:break_detected_at"

# --listen mode trigger (see api.py's _trigger_reactive_cf_refresh, which publishes/sets both of
# these alongside the home server's own one-shot attempt).
REDIS_KEY_NEED_FALLBACK_REFRESH = f"miruro_api:need_mac_refresh:{FALLBACK_TOPIC}"
REDIS_CHANNEL_FALLBACK_REFRESH = f"miruro_api:mac_refresh_channel:{FALLBACK_TOPIC}"
FALLBACK_POLL_INTERVAL_SECONDS = int(os.getenv("FALLBACK_POLL_INTERVAL_SECONDS", str(30 * 60)))

# Skip launching the browser entirely when the cached cookie still has plenty of life left, in
# one-shot (non --force, non --listen) mode — cuts real Chromium/Cloudflare-challenge runs down
# to only when actually needed.
MIN_TTL_BEFORE_REFRESH_SECONDS = 10 * 60

# No debounce on the failure alert — this is a critical service with apps depending on it, and
# the user explicitly wants a message every time this fires until it's fixed. In practice that's
# capped at once/minute anyway: api.py's reactive trigger only fires once per its own 60s lock,
# no matter how many requests are failing concurrently.
# Only set on the home node's own .env — no hardcoded path with a real username in the repo.
# Unset (any other machine) means os.path.exists("") is False, falling through to the
# NOTIFY_RELAY_URL branch below, same as before.
HERMES_BIN = os.getenv("HERMES_BIN_PATH", "")

# Only the home node has Hermes installed locally; every other machine relays through it — see
# NOTIFY_RELAY_URL in api.py for the full rationale. Leave unset on the home node.
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
        logger.exception("Fallo notificando por Telegram vía el relay")


# Cloudflare's challenge page title, localized by the browser's Accept-Language — seen both as
# English ("Just a moment...") and Spanish ("Un momento...") live.
_CHALLENGE_TITLE_MARKERS = ("just a moment", "un momento")

# Confirmed live: a Chrome launched with ZERO automation attached — no Playwright/patchright
# control at all — clears Cloudflare's challenge on its own within this window, the same way an
# ordinary human visit would. Only AFTER this wait do we attach at all. Launching with
# patchright controlling the browser from the first navigation either never resolved, or
# resolved with an incomplete header capture that made the cookie fail live pipe calls anyway.
NAKED_LAUNCH_WAIT_SECONDS = int(os.getenv("NAKED_LAUNCH_WAIT_SECONDS", "20"))
DEBUG_PORT = 9222

_MAC_CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
]
_WINDOWS_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


async def _find_chrome_path() -> str:
    """Same technique everywhere (launch a bare Chrome subprocess), different binary per OS.
    Prefers a real installed Chrome over patchright's own bundled Chromium wherever one exists —
    a real install is one less thing distinguishing this from an ordinary human's browser."""
    if sys.platform == "darwin":
        for path in _MAC_CHROME_PATHS:
            if os.path.exists(path):
                return path
        # Hardcoded paths miss non-default install locations (e.g. installed via a Downloads
        # folder move, or synced from Time Machine to a different volume). mdfind (Spotlight)
        # finds Chrome.app wherever the OS actually indexed it, keyed by bundle id rather than
        # a guessed path.
        try:
            result = subprocess.run(
                ["mdfind", "kMDItemCFBundleIdentifier == 'com.google.Chrome'"],
                capture_output=True, text=True, timeout=5,
            )
            for app_path in result.stdout.strip().splitlines():
                candidate = f"{app_path}/Contents/MacOS/Google Chrome"
                if os.path.exists(candidate):
                    return candidate
        except Exception:
            pass
        raise RuntimeError(
            "Google Chrome no aparece instalado en este Mac (ni en las rutas estándar ni vía "
            "mdfind/Spotlight). Instalá Chrome normal (google.com/chrome) — este script necesita "
            "el Chrome real, no el Chromium embebido de patchright, para que Cloudflare confíe "
            "en la sesión."
        )
    elif sys.platform == "win32":
        for path in _WINDOWS_CHROME_PATHS:
            if path and os.path.exists(path):
                return path
        found = shutil.which("chrome") or shutil.which("chrome.exe")
        if found:
            return found
        raise RuntimeError(
            "Google Chrome no aparece instalado en este equipo Windows (ni en las rutas "
            "estándar ni en PATH). Instalá Chrome normal (google.com/chrome) — este script "
            "necesita el Chrome real para que Cloudflare confíe en la sesión."
        )

    # Linux: a real install if present, else patchright's own bundled Chromium (installed via
    # `patchright install chromium`) — works fine for this purpose, just not a "real" install.
    found = shutil.which("google-chrome") or shutil.which("chrome")
    if found:
        return found
    async with async_playwright() as p:
        return p.chromium.executable_path


# Must match api.py's _encode_pipe_request exactly (plain base64 of the JSON, NOT gzipped —
# only pipe *responses* are gzip-compressed, not requests).
def _encode_pipe_request(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


# Same two-path check as api.py's _cf_clearance_actually_broken — confirmed live: solving the
# homepage challenge can succeed while Cloudflare still 403s the real pipe paths this app needs
# (episodes, sources). Verify against both for real before ever accepting a cookie as a fix.
#
# Also cache-busted: Miruro's pipe responses are cached at Cloudflare's edge (confirmed live:
# `cf-cache-status: HIT`, age in the hours, on this exact query) — a cache HIT never reaches the
# origin, so it says nothing about whether the cookie actually works. A throwaway random field
# in the query changes the cache key (confirmed: flips HIT to MISS, still 200) without the
# backend rejecting it. Scoped to verification only — real traffic still caches normally.
#
# A single fixed anilistId got hammered by every node/verification all day (confirmed live
# 2026-09-07: the same anilistId=21/provider=ally/sub sources query got a 444 from two
# completely different IPs in the same session) — the resource itself, not any one node's IP,
# looked flagged. _pick_canary_anilist_id() rotates through a pool of real, current TV anime
# instead, and — via a Redis list shared across every node/group — never repeats one of the
# last 3 ids picked by ANYONE, anywhere in the fleet.
_VERIFY_PROVIDER = "ally"
_VERIFY_CATEGORY = "sub"
_CANARY_ANILIST_ID_POOL = [
    178789, 196187, 135865, 185874, 207141, 187538, 180136, 210031, 103303, 187260,
    159309, 177699, 185542, 198946, 202269, 194829, 201514, 190569, 199111, 188139,
    200637, 169583, 209983, 204466, 177637, 128757, 198409, 182616, 169582, 199066,
    199408, 194219,
]
REDIS_KEY_CANARY_RECENT_IDS = "miruro_api:canary:recent_ids"


async def _pick_canary_anilist_id() -> int:
    """Random pick from the pool, never repeating one of the last 3 ids picked by ANY node —
    shared globally across all FALLBACK_TOPIC groups, since the thing being avoided is hammering
    one shared upstream resource, not a per-group concern."""
    import random
    recent: list[int] = []
    r = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    try:
        recent = [int(x) for x in await r.lrange(REDIS_KEY_CANARY_RECENT_IDS, 0, -1)]
    except Exception:
        pass
    candidates = [i for i in _CANARY_ANILIST_ID_POOL if i not in recent] or _CANARY_ANILIST_ID_POOL
    picked = random.choice(candidates)
    try:
        await r.rpush(REDIS_KEY_CANARY_RECENT_IDS, picked)
        await r.ltrim(REDIS_KEY_CANARY_RECENT_IDS, -3, -1)
        await r.expire(REDIS_KEY_CANARY_RECENT_IDS, 3600)
    except Exception:
        pass
    finally:
        await r.aclose()
    return picked


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


async def _pipe_call(cookie_str: str, headers: dict, payload: dict):
    full_headers = dict(headers)
    full_headers["cookie"] = cookie_str
    full_headers.setdefault("referer", f"{MIRURO_BASE_URL}/")
    url = f"{MIRURO_PIPE_URL}?e={_encode_pipe_request(payload)}"
    async with httpx.AsyncClient(timeout=10, http2=True) as client:
        return await client.get(url, headers=full_headers)


async def _pipe_call_with_429_retry(cookie_str: str, headers: dict, payload: dict, verbose: bool = False):
    """A 429 means "rate limited right now", not "cookie is broken" — a hammered canary query
    (multiple nodes/manual tests verifying at once) can trip this on a perfectly good cookie.
    Only a 403 (or anything else) is treated as a real signal; 429 gets a couple of backed-off
    retries before we give up and let the caller decide."""
    delay = 3
    for attempt in range(3):
        res = await _pipe_call(cookie_str, headers, payload)
        if res.status_code != 429:
            return res
        if verbose:
            print(f"  -> HTTP 429 (rate limited), retry {attempt + 1}/3 in {delay}s...")
        await asyncio.sleep(delay)
        delay *= 2
    return res


async def _cookie_actually_works(cookie_str: str, headers: dict, verbose: bool = False) -> bool:
    """Only a cookie that passes both canaries gets accepted — anything less gets treated
    exactly like the challenge never resolved at all."""
    try:
        canary_anilist_id = await _pick_canary_anilist_id()
        episodes_payload = {
            "path": "episodes", "method": "GET",
            "query": {"anilistId": canary_anilist_id, "_cb": _cache_bust()},
            "body": None, "version": "0.1.0",
        }
        res = await _pipe_call_with_429_retry(cookie_str, headers, episodes_payload, verbose)
        if verbose:
            print(f"  -> episodes: HTTP {res.status_code} (cf-cache-status: {res.headers.get('cf-cache-status')})")
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
                "anilistId": canary_anilist_id,
                "_cb": _cache_bust(),
            },
            "body": None,
            "version": "0.1.0",
        }
        res2 = await _pipe_call_with_429_retry(cookie_str, headers, sources_payload, verbose)
        if verbose:
            print(f"  -> sources:  HTTP {res2.status_code} (cf-cache-status: {res2.headers.get('cf-cache-status')})")
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
    """Launches a bare Chrome subprocess — no Playwright/patchright attached at all — so
    Cloudflare's challenge gets solved exactly like an ordinary browser visit, waits untouched,
    and only THEN attaches via connect_over_cdp to pull the cookie and headers."""
    chrome_path = await _find_chrome_path()
    profile_dir = str(Path(__file__).resolve().parent / ".chrome-profile")

    proc = subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            # Harmless on Mac/Windows; required on Linux, where launching Chromium as a raw
            # subprocess (patchright's own launch() handles this internally, a raw subprocess
            # doesn't) hits its zygote sandbox init and crashes immediately under Xvfb/AppArmor
            # setups without it — confirmed live: "FATAL: No usable sandbox!".
            "--no-sandbox",
            f"{MIRURO_BASE_URL}/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        await asyncio.sleep(NAKED_LAUNCH_WAIT_SECONDS)

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
            try:
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()

                title = (await page.title()).lower()
                if any(marker in title for marker in _CHALLENGE_TITLE_MARKERS):
                    raise RuntimeError(
                        f"still on the challenge screen after {NAKED_LAUNCH_WAIT_SECONDS}s — "
                        "Cloudflare may have escalated to an interactive Turnstile."
                    )

                cookies = await context.cookies(MIRURO_BASE_URL)
                match = next((c for c in cookies if c["name"] == "cf_clearance"), None)
                if not match:
                    raise RuntimeError(
                        f"no cf_clearance cookie present after {NAKED_LAUNCH_WAIT_SECONDS}s"
                    )

                # request.headers() (from CDP's Network.requestWillBeSent) is missing headers
                # Chromium adds later — confirmed live: accept-language, cache-control, pragma,
                # priority, and all three sec-fetch-* were consistently absent, though Chromium
                # attaches those to every request with no exception. Those show up in a separate
                # CDP event (Network.requestWillBeSentExtraInfo). Merge both for the complete,
                # real, as-sent header set.
                target_url = f"{MIRURO_BASE_URL}/"
                base_headers = {}

                def on_request(request):
                    if request.url == target_url:
                        base_headers.update(request.headers)

                page.on("request", on_request)

                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.enable")
                request_urls = {}
                extra_headers_by_id = {}
                cdp.on(
                    "Network.requestWillBeSent",
                    lambda p_: request_urls.__setitem__(p_["requestId"], p_.get("request", {}).get("url")),
                )
                cdp.on(
                    "Network.requestWillBeSentExtraInfo",
                    lambda p_: extra_headers_by_id.__setitem__(p_["requestId"], p_.get("headers", {})),
                )

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
                await asyncio.sleep(1.5)  # let both CDP events land — order isn't guaranteed

                matched_id = next((rid for rid, url in request_urls.items() if url == target_url), None)
                extra_headers = extra_headers_by_id.get(matched_id, {}) if matched_id else {}
                captured_request_headers = {**base_headers, **extra_headers}

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
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

        # Chrome's own SingletonLock/SingletonCookie/SingletonSocket cleanup isn't guaranteed on
        # a forced terminate/kill (it normally happens as part of Chrome's OWN graceful shutdown
        # sequence) — confirmed live: these were still sitting in the profile dir after a cycle
        # whose Chrome process had already fully exited. Chrome validates a lock's PID/hostname
        # before trusting it, so a stale one is low-risk today, but explicit cleanup here removes
        # any chance of a future cycle's launch getting forwarded to (or confused by) leftover
        # state from a prior run instead of starting genuinely fresh every time.
        for stale in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (Path(profile_dir) / stale).unlink(missing_ok=True)
            except Exception:
                pass


async def _current_ttl() -> int:
    r = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
    )
    try:
        return await r.ttl(REDIS_KEY_CF_CLEARANCE)
    finally:
        await r.aclose()


_refresh_lock = asyncio.Lock()


async def run_refresh_once(force: bool = False, dry_run: bool = False) -> bool:
    """Core logic shared by every mode. Returns True on a verified-working refresh.

    dry_run=True: solves + verifies, prints PASS/FAIL, never touches Redis or Telegram — use to
    test whether a machine's IP is even viable before deciding to run it with --listen.
    """
    if _refresh_lock.locked():
        logger.info("A refresh is already in progress, skipping this trigger")
        return False

    async with _refresh_lock:
        if not dry_run and not force:
            ttl = await _current_ttl()
            if ttl and ttl > MIN_TTL_BEFORE_REFRESH_SECONDS:
                print(f"[cf_refresher] SKIP — cookie still has {ttl}s left (> {MIN_TTL_BEFORE_REFRESH_SECONDS}s margin)")
                return False

        failure_reason = None
        cookie_str = headers = None
        try:
            cookie_str, headers = await _solve_challenge_and_capture()
            if not await _cookie_actually_works(cookie_str, headers, verbose=dry_run):
                failure_reason = (
                    "resolvió el challenge de la home pero la cookie no funciona contra el "
                    "pipe real (episodes/sources) — Cloudflare está evaluando esas rutas aparte"
                )
        except Exception as e:
            failure_reason = str(e)

        if dry_run:
            if failure_reason:
                print(f"\nFAILED: {failure_reason}")
            else:
                print(f"\nOK — cookie works for episodes AND sources ({len(headers)} headers captured).")
                print("Nothing was written to Redis and no one was notified — this was a dry run.")
            return not failure_reason

        if failure_reason:
            print(f"[cf_refresher] FAILED: {failure_reason}", file=sys.stderr)
            ttl = await _current_ttl()
            vigencia = f"la cookie actual vence en ~{ttl // 60} min" if ttl and ttl > 0 else "no hay ninguna cookie vigente en este momento"
            notify_telegram(
                f"⚠️ MI-API [nodo: {NODE_ID}]: no logré una cookie que funcione de verdad "
                f"({failure_reason}). {vigencia}. Generá un cf_clearance nuevo desde tu "
                "equipo (misma IP) y pasámelo para que lo aplique."
            )
            return False

        payload = {
            "cookie": cookie_str,
            "headers": headers,
            "updated_at": int(time.time()),
            "source": NODE_ID,
        }

        r = aioredis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
        )
        try:
            await r.set(REDIS_KEY_CF_CLEARANCE, json.dumps(payload), ex=REDIS_TTL_SECONDS)
            await r.delete(REDIS_KEY_NEED_FALLBACK_REFRESH)
            break_detected_at = await r.get(REDIS_KEY_BREAK_DETECTED_AT)
            if break_detected_at:
                await r.delete(REDIS_KEY_BREAK_DETECTED_AT)
        finally:
            await r.aclose()

        print(f"[cf_refresher] OK — refreshed cf_clearance, {len(headers)} headers captured")

        # Only fire a "recovered" alert when this refresh actually closed out a detected outage
        # — a routine manual/proactive refresh with nothing broken shouldn't claim a "recovery"
        # that never happened.
        if break_detected_at:
            elapsed = time.time() - float(break_detected_at)
            print(f"[cf_refresher] RECOVERY TIME: {elapsed:.1f}s (medido, no estimado)")
            notify_telegram(
                f"✅ MI-API [nodo: {NODE_ID}]: recuperado. Tiempo real roto→arreglado: "
                f"{elapsed:.0f}s."
            )
        return True


async def _poll_loop():
    """--listen mode, durable path: catches the case where a pub/sub trigger was published
    while this machine was asleep/offline/disconnected and therefore never received."""
    r = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
    )
    try:
        while True:
            await asyncio.sleep(FALLBACK_POLL_INTERVAL_SECONDS)
            try:
                needed = await r.get(REDIS_KEY_NEED_FALLBACK_REFRESH)
            except Exception:
                logger.exception("Poll check failed (Redis unreachable?)")
                continue
            if needed:
                logger.info("Poll found the refresh flag set — running refresh")
                await run_refresh_once(force=True)
    finally:
        await r.aclose()


async def _pubsub_loop():
    """--listen mode, fast path: instant reaction whenever this listener is connected at
    publish time. Pub/Sub is fire-and-forget — Redis does NOT queue messages for offline
    subscribers — so _poll_loop above is the durable complement, not redundant."""
    r = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
    )
    pubsub = r.pubsub()
    await pubsub.subscribe(REDIS_CHANNEL_FALLBACK_REFRESH)
    logger.info("Listening on %s for instant refresh requests", REDIS_CHANNEL_FALLBACK_REFRESH)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            logger.info("Received instant refresh request via pub/sub")
            await run_refresh_once(force=True)
    finally:
        await pubsub.aclose()
        await r.aclose()


async def listen_forever():
    logger.info(
        "Listening as a fallback node [%s] — poll every %ss, instant pub/sub also active",
        NODE_ID, FALLBACK_POLL_INTERVAL_SECONDS,
    )
    await asyncio.gather(_pubsub_loop(), _poll_loop())


async def main():
    if "--listen" in sys.argv:
        await listen_forever()
        return

    ok = await run_refresh_once(force="--force" in sys.argv, dry_run="--dry-run" in sys.argv)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
