"""Render proposal.html to a print ready PDF.

Uses whichever Chromium based browser is already installed and drives it in
headless mode with --print-to-pdf. That choice is deliberate: the document is
laid out with real print CSS (@page, page-break-before, mm units), and Chrome's
print engine is the one that honours all of it. A Python PDF library would need
the whole stylesheet rewritten to whatever subset it supports.

No pip install required. If no browser is found the script says which paths it
looked at rather than failing with an import error.

    python build_pdf.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "proposal.html"
OUTPUT = HERE / "SokoScout_Proposal.pdf"

# Ordered by preference. Chrome first, then Edge, which ships on every Windows
# machine and uses the same print engine.
CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser() -> Path:
    for path in CANDIDATES:
        candidate = Path(path)
        if candidate.exists():
            return candidate
    looked = "\n  ".join(CANDIDATES)
    raise SystemExit(
        "No Chromium based browser found. Looked in:\n  " + looked
    )


def build() -> Path:
    if not SOURCE.exists():
        raise SystemExit(f"Missing source document: {SOURCE}")

    browser = find_browser()

    # A fresh profile directory each run. Without it, an already running Chrome
    # instance swallows the command line and the process exits immediately
    # having produced nothing, which is the single most common way this fails.
    profile = HERE / ".chrome-profile"

    cmd = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        f"--user-data-dir={profile}",
        "--virtual-time-budget=10000",  # let the inline SVG and fonts settle
        "--print-to-pdf-no-header",     # no browser chrome, URL or page numbers
        f"--print-to-pdf={OUTPUT}",
        SOURCE.as_uri(),
    ]

    print(f"Browser : {browser.name}")
    print(f"Source  : {SOURCE.name}")

    started = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    elapsed = time.time() - started

    if not OUTPUT.exists():
        print(result.stdout[-2000:], file=sys.stderr)
        print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"Browser exited {result.returncode} without writing a PDF.")

    size_kb = OUTPUT.stat().st_size / 1024
    print(f"Output  : {OUTPUT}")
    print(f"Size    : {size_kb:,.0f} KB in {elapsed:.1f}s")
    return OUTPUT


if __name__ == "__main__":
    build()
