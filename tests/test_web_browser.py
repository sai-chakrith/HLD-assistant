"""Optional browser smoke test: a real Chromium drives the UI against a throwaway server.

Skipped cleanly when the playwright package or a Chromium build is not installed. The server runs in a subprocess
with its own DB / index / processed dir, the hashing embedder and no LLM (evidence-only answers), so nothing touches
the real data or Ollama.
"""
from __future__ import annotations

import glob
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

ROOT = Path(__file__).resolve().parent.parent


def _launch(p):
    """Default Playwright Chromium, else any Chromium already in the Playwright cache, else skip."""
    try:
        return p.chromium.launch()
    except Exception:  # noqa: BLE001 - browser for this playwright version not downloaded
        pass
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / "AppData/Local/ms-playwright")
    if not cache.is_dir():
        cache = Path.home() / ".cache/ms-playwright"
    exes = sorted(glob.glob(str(cache / "chromium_headless_shell-*/*/chrome-headless-shell*")) +
                  glob.glob(str(cache / "chromium-*/*/chrome.exe")) + glob.glob(str(cache / "chromium-*/*/chrome")),
                  reverse=True)
    for exe in exes:
        try:
            return p.chromium.launch(executable_path=exe)
        except Exception:  # noqa: BLE001
            continue
    pytest.skip("no Chromium available for playwright")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path, make_pdf):
    import httpx

    from app import controls
    from scripts.seed import seed

    data = tmp_path / "data"
    env = {**os.environ, "DATA_DIR": str(data), "DB_PATH": str(data / "hld.db"), "CHROMA_DIR": str(tmp_path / "chroma"),
           "PROCESSED_DIR": str(data / "processed"), "LLM_PROVIDER": "none", "EMBEDDER": "hash", "ALLOW_HASH_QA": "1",
           "WARMUP": "0", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "OLLAMA_BASE_URL": "http://127.0.0.1:9"}  # closed port: /health never waits on a real Ollama
    env.pop("PYTEST_CURRENT_TEST", None)
    c = controls.get_conn(data / "hld.db")
    seed(c)
    c.close()
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api:app", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 90
        while True:
            try:
                if httpx.get(base + "/static/css/app.css", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline or proc.poll() is not None:
                pytest.fail("server did not start")
            time.sleep(0.5)
        pdf = make_pdf(tmp_path / "wiper_hld.pdf")
        with open(pdf, "rb") as f:
            r = httpx.post(f"{base}/projects/synthetic-ecu/upload", headers={"X-User": "admin"}, timeout=300,
                           files={"file": (pdf.name, f, "application/pdf")},
                           data={"doc_type": "hld", "version": "1.0", "source_status": "synthetic"})
        assert r.status_code == 200, r.text
        yield base, make_pdf(tmp_path / "draft.pdf", marker="draft")
    finally:
        proc.terminate()
        proc.wait(timeout=20)


def test_browser_smoke_rejected_upload_ask_review(server):
    base, draft = server
    external, errors = [], []
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: external.append(r.url) if not r.url.startswith((base, "data:", "blob:", "about:")) else None)
        try:
            # 1. Documents: a draft upload is rejected and the reason is shown inline
            page.goto(f"{base}/documents?project=synthetic-ecu")
            page.wait_for_selector("#docs-table table")
            assert "wiper_hld" in page.inner_text("#docs-table")
            page.get_by_text("Add document", exact=True).click()
            page.set_input_files("input[type=file]", str(draft))
            page.select_option("select[name=source_status]", "draft")
            page.click("#upload-btn")
            page.wait_for_selector("#upload-result .note.error", timeout=60000)
            assert "not allowed" in page.inner_text("#upload-result")

            # 2. Ask (home page): sample questions are checked first and only answered ones are listed (here no
            #    component owns a port and there is no LLM, so only the deliberate off-topic sample remains);
            #    Settings popover, Enter submits; the answer carries confidence and the unreviewed label
            page.goto(f"{base}/")
            page.wait_for_selector("#ask-form", timeout=60000)
            samples = page.inner_text("#samples")
            assert "weather" not in samples and "Which ports does the" not in samples
            page.click("#settings-btn")
            assert page.locator("#settings").is_visible() and page.locator("#settings #project-select").count() == 1
            page.keyboard.press("Escape")
            assert not page.locator("#settings").is_visible()
            page.set_viewport_size({"width": 800, "height": 900})  # tablet width: no horizontal page scroll
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.set_viewport_size({"width": 1280, "height": 900})
            page.fill("#question", "What does the WiperManager do?")
            page.press("#question", "Enter")
            page.wait_for_selector("#answer .meta-row", timeout=120000)
            text = page.inner_text("#answer")
            assert "Retrieved passages only" in text and "Unreviewed AI output" in text
            page.get_by_text("Recent questions", exact=True).click()
            assert "What does the WiperManager do?" in page.inner_text("#history")

            # Completed answers, citations, the selected question, and drafts survive full navigation.
            page.fill("#question", "Unsubmitted follow-up")
            page.locator('.nav a[href*="architecture"]').click()
            page.wait_for_selector("#inventory table")
            page.locator('.nav a').filter(has_text="Ask").click()
            page.wait_for_selector("#answer .meta-row")
            assert page.inner_text("#answer") == text
            assert page.locator("#question").input_value() == "Unsubmitted follow-up"
            assert "What does the WiperManager do?" in page.inner_text("#history")
            page.reload()
            page.wait_for_selector("#answer .meta-row")
            assert page.inner_text("#answer") == text
            page.goto(f"{base}/?project=chassis")
            page.wait_for_selector("#ask-form")
            assert page.locator("#question").input_value() == ""
            assert page.inner_text("#history") == ""
            page.goto(f"{base}/?project=synthetic-ecu")
            page.wait_for_selector("#answer .meta-row")
            assert page.inner_text("#answer") == text
            assert page.locator("#question").input_value() == "Unsubmitted follow-up"

            # 3. Review: the page loads its document and findings state, with approve controls for admin
            page.goto(f"{base}/review")
            page.wait_for_function("document.querySelector('#findings .state, #findings .rule-group') "
                                   "&& !document.querySelector('#findings').innerText.startsWith('Loading')", timeout=60000)
            assert page.locator("#doc-select").input_value() == "wiper_hld"
            assert page.locator("#review-actions").count() == 1

            # Review a real extracted fact through the browser and verify persistence after a full reload.
            page.goto(f"{base}/architecture")
            page.wait_for_selector("#inventory table")
            row = page.locator("#inventory tr").filter(has_text="WiperManager").first
            row.get_by_role("button", name="Review / edit").click()
            page.locator("#side-panel").get_by_label("description", exact=True).fill("Source checked by the browser smoke test")
            page.get_by_label("Reason for correction", exact=True).fill("Checked the source page")
            page.get_by_role("button", name="Save correction").click()
            page.wait_for_function("document.querySelector('#side-panel').innerText.includes('Correction saved')")
            page.reload()
            page.wait_for_selector("#inventory table")
            assert "Source checked by the browser smoke test" in page.inner_text("#inventory")
            assert "completeness has not been established" in page.inner_text("#coverage")

            # Demo path keeps the workspace selected. Every screen fits a narrow phone viewport.
            for path, ready in [("/", "#ask-form"), ("/architecture", "#inventory table"),
                                ("/review", "#findings"), ("/documents", "#docs-table table")]:
                page.set_viewport_size({"width": 375, "height": 812})
                page.goto(f"{base}{path}?project=synthetic-ecu")
                page.wait_for_selector(ready)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), path
                assert "project=synthetic-ecu" in page.locator('.nav a[href*="architecture"]').get_attribute("href")
            page.goto(f"{base}/?project=synthetic-ecu")
            page.click("#workspace-switch")
            assert page.locator("#settings").is_visible()
            page.keyboard.press("Escape")
            page.locator('.nav a[href*="architecture"]').click()
            page.wait_for_selector("#inventory table")
            assert "project=synthetic-ecu" in page.url
            page.set_viewport_size({"width": 1280, "height": 900})

            # Task views reveal one focused workspace at a time and survive a reload.
            page.locator('[data-view="dependencies"]').click()
            page.wait_for_selector("#graph iframe")
            assert not page.locator("#inventory").is_visible()
            page.reload()
            page.wait_for_selector("#graph iframe")
            assert page.locator('[data-view="dependencies"]').get_attribute("aria-pressed") == "true"
            page.goto(f"{base}/review?project=synthetic-ecu")
            page.locator('[data-view="versions"]').click()
            assert page.locator("#compare-form").is_visible()
            assert not page.locator("#findings").is_visible()
            page.locator('[data-view="export"]').click()
            assert page.locator("#export-fmt").is_visible()
            page.locator('[data-view="issues"]').click()
            assert page.locator("#findings").is_visible()

            # 4. Default document: the project's own newest release, not the first row (another spec in the project)
            pick = page.evaluate("""async () => {
                const m = await import('/static/js/api.js');
                m.ctx.project = 'body-comfort';
                const docs = [{doc_id: 'AUTOSAR_CP_SWS_DiagnosticCommunicationManager', version: '1.0'},
                              {doc_id: 'bodycomfort_r3.2.3', version: 'R3.2.3'},
                              {doc_id: 'bodycomfort_r3.1.5', version: 'R3.1.5'}];
                return m.fillDocPicker(document.createElement('select'), docs, undefined);
            }""")
            assert pick == "bodycomfort_r3.2.3"
        finally:
            browser.close()
    assert not errors, errors
    assert not external, external
