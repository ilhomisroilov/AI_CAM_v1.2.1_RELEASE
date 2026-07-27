"""
Rapid MVP — D2222/D2223 grace observability (status() + /api/metrics) testlari.

Operator joriy sessiya holatini (jumladan GRACE_WAIT, D2222/D2223 timestamp,
grace qolgan vaqt, finalizing sessiya soni, "last completed") ko'ra olishini
tekshiradi — spec §18. Eski `last_vin` joriy sessiya bilan aralashmaydi.
"""
from __future__ import annotations

import time

import pytest

from backend import config as cfg
from backend.pipeline import SessionState
from conftest import make_ocr_result, wait_until


def _active(p):
    with p._session_lock:
        return p._sessions.get(p._active_session_id) if p._active_session_id else None


def test_status_exposes_grace_fields(pipeline_instance, monkeypatch):
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "post_exit_grace_ms", 2000)
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    p.on_exit_signal()          # -> GRACE_WAIT

    s = p.status()["session"]
    assert s["state"] == "GRACE_WAIT"
    assert s["d2222_at"] is not None
    assert s["d2223_at"] is not None
    assert isinstance(s["grace_remaining_sec"], (int, float))
    assert s["grace_remaining_sec"] > 0
    assert s["finalizing_session_count"] >= 1


def test_status_last_completed_is_separate(pipeline_instance, monkeypatch):
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "post_exit_grace_ms", 2000)
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ200001"))
    p.on_exit_signal()          # VIN bor + RFID o'chiq -> darhol SUCCESS
    assert wait_until(lambda: sess.state == SessionState.SUCCESS)

    s = p.status()["session"]
    # Faol sessiya yo'q, lekin "last_completed" ALOHIDA ko'rsatiladi (aralashmaydi).
    assert s["active"] is False
    assert s["last_completed"] is not None
    assert s["last_completed"]["session_id"] == sid
    assert s["last_completed"]["state"] == "SUCCESS"
    assert s["last_completed"]["vin"] == "NSTFA814ATJ200001"


def test_extract_metrics_surfaces_grace_keys():
    from backend.server import _extract_metrics
    synthetic = {
        "session": {
            "active": True, "id": 7, "state": "GRACE_WAIT",
            "d2222_at": 1000.0, "d2223_at": 1001.2, "grace_remaining_sec": 1.3,
            "finalizing_session_count": 2, "pending_triggers": 0,
            "dropped_trigger_count": 0, "last_completed": {"session_id": "x", "state": "SUCCESS"},
        },
        "metrics": {},
    }
    m = _extract_metrics(synthetic)
    assert m["current_session_state"] == "GRACE_WAIT"
    assert m["d2222_at"] == 1000.0
    assert m["d2223_at"] == 1001.2
    assert m["grace_remaining_sec"] == 1.3
    assert m["finalizing_session_count"] == 2
    assert m["last_completed_session"] == {"session_id": "x", "state": "SUCCESS"}
