"""Screenshots of the four UI screens for the report, taken with Playwright against a RUNNING server.

    python scripts/screenshots.py [--base http://127.0.0.1:8000] [--out docs/screenshots]

Uses the real data: Documents and Review on synthetic-ecu, Ask (home page) on body-comfort using its sample
questions (one cited answer and the off-topic refusal), Architecture with the WinCtrlManager side panel open.
--width 800 takes the same screens at tablet width. Prints any page error or
request that left the server, so it doubles as an offline check.
"""
from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def launch(p):
    try:
        return p.chromium.launch()
    except Exception:  # noqa: BLE001 - this playwright version's browser is not downloaded; use any cached one
        cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / "AppData/Local/ms-playwright")
        for exe in sorted(glob.glob(str(cache / "chromium_headless_shell-*/*/chrome-headless-shell*")), reverse=True):
            return p.chromium.launch(executable_path=exe)
        raise


WIDTH = 1280


def shoot(page, path: Path, full: bool = True) -> None:
    """Grow the viewport to the page height instead of full_page, so the sticky top bar stays at the top."""
    page.wait_for_function("document.getElementById('status-text').textContent !== 'Checking'", timeout=60000)
    if full:
        h = page.evaluate("document.documentElement.scrollHeight")
        page.set_viewport_size({"width": WIDTH, "height": max(800, min(h, 4000))})
    page.wait_for_timeout(300)
    page.screenshot(path=str(path))
    page.set_viewport_size({"width": WIDTH, "height": 800})
    if page.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
        print("WARNING horizontal scroll on", path.name)
    print("saved", path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    ap.add_argument("--width", type=int, default=1280)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    base = a.base.rstrip("/")
    problems: list[str] = []
    with sync_playwright() as p:
        browser = launch(p)
        global WIDTH
        WIDTH = a.width
        ctx = browser.new_context(viewport={"width": WIDTH, "height": 800}, color_scheme="light")
        ctx.add_cookies([{"name": "hld_user", "value": "admin", "url": base}])
        page = ctx.new_page()
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
        page.on("request", lambda r: problems.append(f"external request: {r.url}")
                if not r.url.startswith((base, "data:", "blob:", "about:")) else None)
        page.on("response", lambda r: problems.append(f"HTTP {r.status} {r.url}") if r.status >= 500 else None)

        page.goto(f"{base}/documents?project=synthetic-ecu")
        page.wait_for_selector("#docs-table table")
        page.wait_for_function("document.getElementById('status-text').textContent !== 'Checking'")
        shoot(page, out / "documents.png")

        page.goto(f"{base}/?project=body-comfort")
        page.wait_for_selector("#samples li button")
        for i in (-1, 0):  # the off-topic sample (refusal), then "Which ports does the ... have?"
            btn = page.locator("#samples li button").nth(i if i >= 0 else page.locator("#samples li button").count() - 1)
            q = btn.inner_text()
            btn.click()
            page.wait_for_function(f"document.querySelector('#history')?.innerText.includes({q!r})", timeout=180000)
            if i < 0:
                shoot(page, out / "ask_refusal.png", full=False)
        shoot(page, out / "ask.png")

        page.goto(f"{base}/architecture?project=synthetic-ecu&doc=syn_hld_v1")
        page.wait_for_selector("#inventory table")
        page.wait_for_selector(".graph-frame", timeout=60000)
        page.wait_for_timeout(2500)  # let vis-network settle the layout
        page.get_by_role("button", name="WinCtrlManager").first.click()
        page.wait_for_selector("#panel-body .facts", timeout=180000)
        shoot(page, out / "architecture.png", full=False)

        page.goto(f"{base}/review?project=synthetic-ecu&doc=syn_hld_v1")
        page.wait_for_selector(".rule-group")
        page.click("#compare-form button[type=submit]")
        page.wait_for_selector(".diff-summary", timeout=60000)
        page.locator(".section-change").first.locator("summary").click()
        shoot(page, out / "review.png")
        browser.close()
    print("\n".join(problems) if problems else "no page errors, no requests outside the server")


if __name__ == "__main__":
    main()
