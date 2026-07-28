"""Real-browser (Playwright) frontend acceptance test.

The Windows Script Host polling harness (`test_frontend_polling.py`) injects fake
timer functions, so it cannot catch native-binding defects — e.g. calling
`window.setInterval` with the wrong `this` raises "Illegal invocation" in a real
browser and silently stops ALL polling, while the mock harness stays green. This
test drives a real headless Chromium (a *visible* page) against a live server and
asserts the dashboard and history actually issue their API requests with zero
uncaught JS errors. It skips cleanly when Playwright / its browser is absent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="playwright not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"
EM_DASH = "—"
ENDPOINTS = ("/api/status", "/api/plc/status", "/api/rfid/status", "/video_feed", "/api/records")


def _chromium_ready() -> bool:
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
            return bool(path) and os.path.exists(path)
    except Exception:
        return False


def _wait_health(timeout: float = 150.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(2.0)
    return False


@pytest.fixture(scope="module")
def server():
    if not _chromium_ready():
        pytest.skip("Chromium for Playwright is not installed")
    env = dict(os.environ)
    env["AI_CAM_SERVER_PORT"] = str(PORT)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "run.py", "--no-hardware"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_health():
            proc.terminate()
            pytest.skip("server did not become healthy in time")
        yield BASE
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()


def _drive_browser(base: str):
    api_req: set[str] = set()
    api_resp: dict[str, int] = {}
    console_errors: list[str] = []
    page_errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("request", lambda r: [api_req.add(e) for e in ENDPOINTS if e in r.url])
        page.on("response", lambda r: api_resp.update({e: r.status for e in ENDPOINTS if e in r.url}))

        # login admin/admin (real form POST carries the CSRF field)
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.wait_for_selector("input[type=password]", timeout=10000)
        page.fill("input[type=text]", "admin")
        page.fill("input[type=password]", "admin")
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

        # dashboard
        page.goto(f"{base}/dashboard", wait_until="domcontentloaded")
        visibility = page.evaluate("document.visibilityState")
        page.wait_for_timeout(3000)

        def txt(sel: str):
            el = page.query_selector(sel)
            return el.inner_text() if el else None

        dash = {
            "visibility": visibility,
            "api_req": set(api_req),
            "api_resp": dict(api_resp),
            "tCam": txt("#tCam"), "tAi": txt("#tAi"),
        }

        # history
        api_req.clear(); api_resp.clear()
        page.goto(f"{base}/history", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        hist = {
            "records": "/api/records" in api_req,
            "records_status": api_resp.get("/api/records"),
            "rows": page.evaluate("document.querySelectorAll('table tbody tr').length"),
        }
        browser.close()
    return dash, hist, console_errors, page_errors


def test_dashboard_and_history_issue_api_requests(server):
    dash, hist, console_errors, page_errors = _drive_browser(server)

    # zero uncaught JS — this is what catches the setInterval "Illegal invocation".
    assert page_errors == [], f"uncaught page errors: {page_errors}"
    assert console_errors == [], f"console errors: {console_errors}"

    # a visible page must actually poll
    assert dash["visibility"] == "visible"
    for ep in ("/api/status", "/api/plc/status", "/api/rfid/status"):
        assert ep in dash["api_req"], f"{ep} was never requested by the dashboard"
        assert dash["api_resp"].get(ep) == 200, f"{ep} status={dash['api_resp'].get(ep)}"

    # status cards populated from /api/status (not stuck on the em-dash placeholder)
    assert dash["tCam"] not in (None, "", EM_DASH), f"camera card not updated: {dash['tCam']!r}"
    assert dash["tAi"] not in (None, "", EM_DASH), f"AI card not updated: {dash['tAi']!r}"

    # history issues its one records request and renders rows
    assert hist["records"], "/api/records was never requested by history"
    assert hist["records_status"] == 200
    assert hist["rows"] >= 1
