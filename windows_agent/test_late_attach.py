"""Two-phase test: launch Chrome with ZERO automation attached, let the challenge clear
completely on its own (confirmed live: an untouched Chrome resolves it and lands on the
homepage — see launch_raw_chrome.py), then — ONLY after that 30s window — attach patchright via
connect_over_cdp to pull the cookie and headers, and close the browser.

Nothing talks to Chrome at all during the wait — no HTTP polling of the debug port, no CDP
handshake, nothing. This isolates whether Cloudflare's challenge specifically detects the
DevTools Protocol connection itself (which Playwright/patchright normally establish the instant
they launch a browser, before navigation even starts), separate from headless/IP-reputation.

Does NOT write to Redis or notify the server — local test only, same as test_cookie.py. Reuses
its validation checks (recent-episodes, watch) so the result means the same thing.

Run (Windows, from this directory):
    python test_late_attach.py
"""
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

from patchright.async_api import async_playwright

from test_cookie import (
    MIRURO_BASE_URL,
    CAPTURED_HEADER_NAMES,
    test_recent_episodes,
    test_watch,
)

DEBUG_PORT = 9334
PROFILE_DIR = str(Path(__file__).resolve().parent / ".chrome-profile-late")
URL = f"{MIRURO_BASE_URL}/"
WAIT_SECONDS = 30

_WINDOWS_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome() -> str:
    for path in _WINDOWS_CHROME_PATHS:
        if path and os.path.exists(path):
            return path
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if found:
        return found
    raise RuntimeError("Could not find Google Chrome — edit _WINDOWS_CHROME_PATHS in this file")


async def main():
    chrome = find_chrome()
    print(f"[1/6] Launching Chrome — NOTHING attached: {chrome}")

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            URL,
        ],
        **kwargs,
    )

    print(f"[2/6] Waiting {WAIT_SECONDS}s untouched — nothing connects to Chrome during this window.")
    for remaining in range(WAIT_SECONDS, 0, -5):
        print(f"      {remaining}s left...")
        await asyncio.sleep(5)

    print("[3/6] Attaching patchright now — only now, not before.")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        title = await page.title()
        print(f"      Page title right now: {title!r}")

        cookies = await context.cookies(MIRURO_BASE_URL)
        match = next((c for c in cookies if c["name"] == "cf_clearance"), None)
        if not match:
            print("\nFAILED: no cf_clearance cookie present even after waiting.")
            await browser.close()
            proc.terminate()
            return
        print("[4/6] Got cf_clearance.")

        # Playwright's page.on("request") only exposes headers from the CDP event
        # Network.requestWillBeSent — confirmed live: it was consistently missing
        # accept-language, cache-control, pragma, priority, and all three sec-fetch-* headers,
        # which Chromium ALWAYS attaches to every request with no exception. Those get added
        # later, in a separate CDP event (Network.requestWillBeSentExtraInfo) that the simple
        # request.headers() API doesn't surface. Capture BOTH — request.headers() has
        # sec-ch-ua-*/accept/referer/user-agent correctly, ExtraInfo has the rest — and merge
        # them for the complete, real, as-sent header set.
        target_url = f"{MIRURO_BASE_URL}/"
        base_headers = {}

        def on_request(request):
            if request.url == target_url:
                base_headers.update(request.headers)

        page.on("request", on_request)

        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.enable")

        request_urls = {}        # requestId -> url
        extra_headers_by_id = {}  # requestId -> headers

        cdp.on("Network.requestWillBeSent", lambda p: request_urls.__setitem__(p["requestId"], p.get("request", {}).get("url")))
        cdp.on("Network.requestWillBeSentExtraInfo", lambda p: extra_headers_by_id.__setitem__(p["requestId"], p.get("headers", {})))

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
        await asyncio.sleep(1.5)  # let both CDP events land — order between them isn't guaranteed

        matched_id = next((rid for rid, url in request_urls.items() if url == target_url), None)
        extra_headers = extra_headers_by_id.get(matched_id, {}) if matched_id else {}
        if not extra_headers:
            print("      WARNING: couldn't match the ExtraInfo event — using request.headers only (incomplete)")
        captured_request_headers = {**base_headers, **extra_headers}

        all_cookies = await context.cookies(MIRURO_BASE_URL)
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in all_cookies)
        headers = {
            k.lower(): v
            for k, v in captured_request_headers.items()
            if k.lower() in CAPTURED_HEADER_NAMES
        }
        current_url = page.url
        print(f"[5/6] Captured {len(headers)} headers (request.headers + CDP ExtraInfo merged).")

        await browser.close()

    print("\n----- RAW DATA (inspect this yourself before trusting the PASS/FAIL) -----")
    print(f"page.url at capture time: {current_url}")
    print(f"user-agent used:          {headers.get('user-agent')}")
    print(f"sec-ch-ua used:           {headers.get('sec-ch-ua')}")
    print(f"full cookie string:       {cookie_str}")
    print("all captured headers:")
    for k, v in sorted(headers.items()):
        print(f"    {k}: {v}")
    print("----------------------------------------------------------------------------\n")

    try:
        proc.terminate()
    except Exception:
        pass
    print("[6/6] Browser closed.")

    print("\nTesting the cookie against real pipe endpoints...")
    print("  -> /recent-episodes (path=schedule):")
    recent_ok = await test_recent_episodes(cookie_str, headers)
    print("  -> /watch (path=episodes then sources):")
    watch_ok = await test_watch(cookie_str, headers)

    print("\n===== RESULT =====")
    print(f"recent-episodes: {'PASS' if recent_ok else 'FAIL'}")
    print(f"watch:           {'PASS' if watch_ok else 'FAIL'}")
    if recent_ok and watch_ok:
        print("\nLaunching naked + attaching late WORKS — this cookie is fully good.")
    print("===================\n")
    print("Nothing was sent to the server — this was a local test only.")


if __name__ == "__main__":
    asyncio.run(main())
