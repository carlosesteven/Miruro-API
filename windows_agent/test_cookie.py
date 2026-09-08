"""Standalone diagnostic: does a Windows machine's real Chrome reliably get a cf_clearance that
actually works against Miruro's pipe? Launches a real (non-headless) Chrome, solves Cloudflare's
challenge, then tests the resulting cookie against the SAME real endpoints the API serves:
/recent-episodes (pipe path "schedule") and /watch (pipe path "sources") — not a synthetic
canary, the actual production paths.

This is a TEST ONLY — it does NOT write to Redis and does NOT notify anyone. Nothing here talks
to the server. Run it, read the PASS/FAIL summary at the end, and report back.

Run (from this directory, in an activated venv — see instructions at the bottom of this file):
    python test_cookie.py
"""
import asyncio
import base64
import gzip
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

# Same repo, same one .env at the root — not a separate copy (see mac_agent for why).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import os

import httpx
from patchright.async_api import async_playwright

MIRURO_BASE_URL = os.getenv("MIRURO_BASE_URL", "https://www.miruro.to").rstrip("/")
MIRURO_PIPE_URL = f"{MIRURO_BASE_URL}/api/secure/pipe"

# Cloudflare's challenge page title, localized by the browser's Accept-Language — seen both as
# English ("Just a moment...") and Spanish ("Un momento...") live.
_CHALLENGE_TITLE_MARKERS = ("just a moment", "un momento")

# Confirmed live: a Chrome launched with ZERO automation attached — no Playwright/patchright
# control at all — clears Cloudflare's challenge on its own within this window, the same way an
# ordinary human visit would (see launch_raw_chrome.py). Only AFTER this wait do we attach at
# all. Launching with patchright controlling the browser from the first navigation either never
# resolved, or resolved with an incomplete header capture that made the cookie fail live pipe
# calls anyway — see the CDP ExtraInfo merge below.
NAKED_LAUNCH_WAIT_SECONDS = int(os.getenv("NAKED_LAUNCH_WAIT_SECONDS", "20"))
DEBUG_PORT = 9222

_WINDOWS_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def _find_chrome() -> str:
    import shutil
    for path in _WINDOWS_CHROME_PATHS:
        if path and os.path.exists(path):
            return path
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if found:
        return found
    raise RuntimeError("Could not find Google Chrome — edit _WINDOWS_CHROME_PATHS in this file")

CAPTURED_HEADER_NAMES = {
    "accept", "accept-language", "cache-control", "pragma", "priority", "referer",
    "sec-ch-ua", "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list", "sec-ch-ua-mobile", "sec-ch-ua-model",
    "sec-ch-ua-platform", "sec-ch-ua-platform-version",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "user-agent",
}

# Real anime IDs already confirmed to exist in Miruro's catalog today — used only to build a
# realistic "sources" (watch) query, not as anything special about these titles.
_PROBE_ANILIST_ID = 21          # One Piece
_PROBE_PROVIDER = "ally"
_PROBE_CATEGORY = "sub"


def _encode_pipe_request(payload: dict) -> str:
    """Must match api.py's _encode_pipe_request exactly: plain base64 of the JSON, NOT gzipped
    — only pipe *responses* are gzip-compressed, not requests."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode_pipe_response(encoded_str: str) -> dict:
    padded = encoded_str + "=" * (4 - len(encoded_str) % 4)
    return json.loads(gzip.decompress(base64.urlsafe_b64decode(padded)).decode())


def _cache_bust() -> str:
    """Miruro's pipe responses are cached at Cloudflare's edge — confirmed live: the exact same
    anilistId=21 episodes query came back `cf-cache-status: HIT`, age 3+ hours old. A cache HIT
    never reaches Miruro's origin, so a PASS from a cached response proves nothing about whether
    the cookie actually works. Adding this throwaway field to a query's "query" dict changes the
    resulting URL enough to force a MISS (confirmed live, still 200, Miruro ignores the unknown
    field) without touching how real production traffic gets cached."""
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


async def solve_challenge_and_capture():
    """Launches the ACTUAL installed Google Chrome as a raw subprocess — no Playwright/patchright
    attached at all — so Cloudflare's challenge gets solved exactly like an ordinary browser
    visit, waits untouched, and only THEN attaches via connect_over_cdp to pull the cookie and
    headers. Uses a dedicated, throwaway profile directory rather than the user's live
    daily-driver Chrome profile."""
    import subprocess

    chrome_path = _find_chrome()
    profile_dir = str(Path(__file__).resolve().parent / ".chrome-profile")

    print(f"[1/5] Launching Chrome — NOTHING attached: {chrome_path}")
    proc = subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            f"{MIRURO_BASE_URL}/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        print(f"[2/5] Waiting {NAKED_LAUNCH_WAIT_SECONDS}s untouched...")
        await asyncio.sleep(NAKED_LAUNCH_WAIT_SECONDS)

        print("[3/5] Attaching patchright now — only now, not before.")
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
                print("[4/5] Got cf_clearance.")

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
                print(f"[5/5] Captured {len(captured_request_headers)} headers (request.headers + CDP ExtraInfo merged).")

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


async def _pipe_call(cookie_str: str, headers: dict, payload: dict):
    full_headers = dict(headers)
    full_headers["cookie"] = cookie_str
    full_headers.setdefault("referer", f"{MIRURO_BASE_URL}/")
    url = f"{MIRURO_PIPE_URL}?e={_encode_pipe_request(payload)}"
    async with httpx.AsyncClient(timeout=15, http2=True) as client:
        res = await client.get(url, headers=full_headers)
    cache_status = res.headers.get("cf-cache-status")
    if cache_status:
        print(f"      (cf-cache-status: {cache_status}{', age=' + res.headers['age'] if 'age' in res.headers else ''})")
    return res


async def test_recent_episodes(cookie_str: str, headers: dict) -> bool:
    """Same pipe query as GET /recent-episodes (path "schedule"), cache-busted (see
    _cache_bust) so a Cloudflare edge cache HIT can't fake a pass."""
    payload = {"path": "schedule", "method": "GET", "query": {"sort": ["TIME_DESC"], "newest": True, "_cb": _cache_bust()}, "body": None}
    try:
        res = await _pipe_call(cookie_str, headers, payload)
    except Exception as e:
        print(f"      exception: {e}")
        return False
    if res.status_code != 200:
        print(f"      HTTP {res.status_code}")
        return False
    try:
        data = _decode_pipe_response(res.text.strip())
        print(f"      OK — {len(data)} items")
        return True
    except Exception as e:
        print(f"      got 200 but couldn't decode the response: {e}")
        return False


async def test_watch(cookie_str: str, headers: dict) -> bool:
    """Same two-step flow as GET /watch/{provider}/{anilist_id}/{category}/{slug}: fetch episodes
    for a real anime (path "episodes") to get a real raw episodeId, then fetch sources for it
    (path "sources") — the exact query GET /watch ends up making against the pipe."""
    episodes_payload = {
        "path": "episodes", "method": "GET",
        "query": {"anilistId": _PROBE_ANILIST_ID, "_cb": _cache_bust()}, "body": None, "version": "0.1.0",
    }
    try:
        res = await _pipe_call(cookie_str, headers, episodes_payload)
    except Exception as e:
        print(f"      exception fetching episodes: {e}")
        return False
    if res.status_code != 200:
        print(f"      HTTP {res.status_code} fetching episodes")
        return False

    try:
        episodes_data = _decode_pipe_response(res.text.strip())
        eps = episodes_data.get("providers", {}).get(_PROBE_PROVIDER, {}).get("episodes", {}).get(_PROBE_CATEGORY, [])
        raw_episode_id = _translate_id(eps[0]["id"]) if eps else None
    except Exception as e:
        print(f"      got episodes but couldn't parse them: {e}")
        return False

    if not raw_episode_id:
        print(f"      no episodes found for provider={_PROBE_PROVIDER}/{_PROBE_CATEGORY}")
        return False

    sources_payload = {
        "path": "sources", "method": "GET",
        "query": {
            "episodeId": base64.urlsafe_b64encode(raw_episode_id.encode()).decode().rstrip("="),
            "provider": _PROBE_PROVIDER,
            "category": _PROBE_CATEGORY,
            "anilistId": _PROBE_ANILIST_ID,
            "_cb": _cache_bust(),
        },
        "body": None, "version": "0.1.0",
    }
    try:
        res2 = await _pipe_call(cookie_str, headers, sources_payload)
    except Exception as e:
        print(f"      exception fetching sources: {e}")
        return False
    if res2.status_code != 200:
        print(f"      HTTP {res2.status_code} fetching sources")
        return False
    try:
        _decode_pipe_response(res2.text.strip())
        print("      OK — got streaming sources")
        return True
    except Exception as e:
        print(f"      got 200 but couldn't decode sources: {e}")
        return False


async def main():
    if not MIRURO_BASE_URL:
        print("MIRURO_BASE_URL not set — check the .env at the repo root", file=sys.stderr)
        sys.exit(1)

    try:
        cookie_str, headers = await solve_challenge_and_capture()
    except Exception as e:
        print(f"\nFAILED to solve the Cloudflare challenge: {e}")
        sys.exit(1)

    print("\nTesting the cookie against real pipe endpoints...")
    print("  -> /recent-episodes (path=schedule):")
    recent_ok = await test_recent_episodes(cookie_str, headers)

    print("  -> /watch (path=episodes then sources):")
    watch_ok = await test_watch(cookie_str, headers)

    print("\n===== RESULT =====")
    print(f"recent-episodes: {'PASS' if recent_ok else 'FAIL'}")
    print(f"watch:           {'PASS' if watch_ok else 'FAIL'}")
    if recent_ok and watch_ok:
        print("\nThis cookie works for everything the API needs.")
    else:
        print("\nThis cookie is PARTIALLY broken — same gap seen on the Mac/Linux automation.")
    print("===================\n")
    print("Nothing was sent anywhere — this was a local test only.")


if __name__ == "__main__":
    asyncio.run(main())


# ---------------------------------------------------------------------------------------------
# HOW TO RUN THIS ON WINDOWS
# ---------------------------------------------------------------------------------------------
# 1. Install Python 3.11+ from python.org if not already installed (check "Add to PATH" during
#    install), and make sure Google Chrome is installed normally.
#
# 2. Get this repo onto the Windows machine:
#      git clone https://github.com/carlosesteven/MI-API.git
#      cd MI-API
#
# 3. Copy the ONE .env this repo already uses at its root (same file api.py reads) and fill in
#    at least MIRURO_BASE_URL (the others aren't needed for this standalone test):
#      copy .env_example .env
#      notepad .env
#
# 4. Set up this folder's own venv and dependencies:
#      cd windows_agent
#      python -m venv venv
#      venv\Scripts\activate
#      pip install -r requirements.txt
#
# 5. Run it:
#      python test_cookie.py
#
#    A real Chrome window will pop up, solve Cloudflare's challenge, then close. The script
#    prints a PASS/FAIL summary for /recent-episodes and /watch. Report back what it prints —
#    this test does not touch Redis or notify anyone, it's local-only.
