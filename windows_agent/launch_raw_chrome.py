"""Launches real Chrome pointed at Miruro with ZERO automation attached — no Playwright, no
patchright, no CDP client connected at all, not even a WebSocket handshake. This isolates one
specific question: does Cloudflare's challenge detect the DevTools Protocol connection itself
(which Playwright/patchright establish the instant they launch or attach a browser, before any
navigation even happens), separate from headless-vs-headful or IP reputation?

This script does NOT close the browser. It launches it and exits — Chrome keeps running on its
own as an independent process. Watch the window: does the challenge clear on its own, the way it
would for a completely ordinary human visit?

A --remote-debugging-port is opened (so a LATER script could attach *after* the challenge clears
and pull the cookie) but nothing connects to it here — that's the whole point of this test.

Run (Windows, from this directory):
    python launch_raw_chrome.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEBUG_PORT = 9333
PROFILE_DIR = str(Path(__file__).resolve().parent / ".chrome-profile-raw")
URL = "https://www.miruro.to/"

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
    raise RuntimeError(
        "Could not find Google Chrome. Edit _WINDOWS_CHROME_PATHS in this file with the real "
        "install path."
    )


def main():
    chrome = find_chrome()
    print(f"Chrome: {chrome}")
    print(f"Profile (dedicated, throwaway): {PROFILE_DIR}")
    print(f"Debug port open but UNUSED: {DEBUG_PORT} — nothing connects to it in this script.")
    print("Nothing is attached to this browser. No Playwright. No CDP client.")
    print()
    print("Launching now. The window will STAY OPEN — this script does not close it.")
    print("Watch it: does the Cloudflare challenge clear on its own?")

    kwargs = {}
    if sys.platform == "win32":
        # Detach fully so this script exiting doesn't take the browser down with it.
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    subprocess.Popen(
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
    print("\nLaunched. This script has exited — Chrome keeps running independently.")
    print("Report back what you see: does the title change away from 'Just a moment...' /")
    print("'Un momento...' on its own, and if so, about how long did it take?")


if __name__ == "__main__":
    main()
