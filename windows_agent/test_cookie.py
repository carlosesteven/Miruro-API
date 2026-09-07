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
import time
from pathlib import Path

from dotenv import load_dotenv

# Same repo, same one .env at the root — not a separate copy (see mac_agent for why).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import os

import httpx
from playwright.async_api import async_playwright

MIRURO_BASE_URL = os.getenv("MIRURO_BASE_URL", "https://www.miruro.to").rstrip("/")
MIRURO_PIPE_URL = f"{MIRURO_BASE_URL}/api/secure/pipe"

CHALLENGE_TIMEOUT_SECONDS = 45
POLL_INTERVAL_SECONDS = 2

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
    """Drives the ACTUAL installed Google Chrome (channel="chrome", not Playwright's bundled
    Chromium) in a normal foreground window — real display, no virtual framebuffer needed. Uses
    a dedicated, throwaway profile directory rather than the user's live daily-driver Chrome
    profile."""
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

            print(f"[1/4] Navigating to {MIRURO_BASE_URL}/ ...")
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
                    "Cloudflare may have escalated to an interactive Turnstile."
                )
            print("[2/4] Got cf_clearance from the challenge.")

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
            print("[3/4] Captured real browser request headers.")

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


async def _pipe_call(cookie_str: str, headers: dict, payload: dict):
    full_headers = dict(headers)
    full_headers["cookie"] = cookie_str
    full_headers.setdefault("referer", f"{MIRURO_BASE_URL}/")
    url = f"{MIRURO_PIPE_URL}?e={_encode_pipe_request(payload)}"
    async with httpx.AsyncClient(timeout=15, http2=True) as client:
        return await client.get(url, headers=full_headers)


async def test_recent_episodes(cookie_str: str, headers: dict) -> bool:
    """Same pipe query as GET /recent-episodes (path "schedule")."""
    payload = {"path": "schedule", "method": "GET", "query": {"sort": ["TIME_DESC"], "newest": True}, "body": None}
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
        "query": {"anilistId": _PROBE_ANILIST_ID}, "body": None, "version": "0.1.0",
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

    print("\n[4/4] Testing the cookie against real pipe endpoints...")
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
