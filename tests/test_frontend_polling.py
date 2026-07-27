from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_poll_owner_behavior_is_deterministic():
    result = subprocess.run(
        [
            "cscript.exe",
            "//nologo",
            str(ROOT / "tests" / "frontend_polling_harness.js"),
            str(ROOT / "frontend" / "static" / "js" / "polling.js"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_history_polling_is_bounded_and_single_owned():
    source = (ROOT / "frontend" / "static" / "js" / "history.js").read_text(
        encoding="utf-8"
    )
    assert 'p.set("limit", "150")' in source
    assert "limit\", \"5000" not in source
    assert 'AICAMPolling.create("history-records"' in source
    assert "setInterval(" not in source
    assert "fetch(`/api/records?${p.toString()}`, { signal })" in source


def test_dashboard_uses_one_page_polling_owner():
    source = (ROOT / "frontend" / "static" / "js" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    assert source.count("AICAMPolling.create(") == 1
    assert "setInterval(" not in source
    assert "Promise.allSettled" in source
    assert '{ intervalMs: 5000 }' in source
