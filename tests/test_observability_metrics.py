"""
tests/test_observability_metrics.py
============================================================
Observability — /api/metrics displays the minimum required metric set,
defensively (never crashes if a field is missing — Agent 1/2 are adding
most of these directly to pipeline.status() in parallel).

Bu agentning mas'uliyati: KO'RSATISH/endpoint qatlami (_extract_metrics,
/api/metrics). pipeline.status() ning o'zi backend/pipeline.py da (boshqa
agent) — shu sababli bu yerda pipeline.status() ni monkeypatch/stub qilib
_extract_metrics + endpoint mantig'ini sinaymiz.
"""
from __future__ import annotations

import backend.config as config_mod
import backend.server as server_mod
from backend.server import _extract_metrics

from conftest import login


def _authed(client, monkeypatch, username="metricsuser", password="MetricsPass1"):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", username)
    monkeypatch.setattr(config_mod.AUTH, "password", password)
    r = login(client, username, password)
    assert r.status_code == 303
    return client


_REQUIRED_KEYS = (
    "active_session", "active_session_id", "pending_trigger_count",
    "dropped_trigger_count", "session_mismatch_count", "late_ocr_result_count",
    "late_rfid_result_count", "duplicate_finalize_attempt_count",
    "camera_last_frame_age", "camera_reconnect_count", "plc_ack_timeout_count",
    "database_write_failure_count",
)


def test_extract_metrics_never_crashes_on_missing_fields():
    out = _extract_metrics({})
    for k in _REQUIRED_KEYS:
        assert k in out


def test_extract_metrics_passes_through_when_present():
    fake_status = {
        "metrics": {
            "dropped_trigger_count": 4,
            "session_mismatch_count": 1,
            "late_ocr_result_count": 2,
            "late_rfid_result_count": 3,
            "duplicate_finalize_attempt_count": 0,
            "camera_reconnect_count": 5,
            "plc_ack_timeout_count": 1,
            "database_write_failure_count": 0,
            "pending_trigger_count": 7,
        },
        "session": {"active": True, "id": 42},
    }
    out = _extract_metrics(fake_status)
    assert out["dropped_trigger_count"] == 4
    assert out["session_mismatch_count"] == 1
    assert out["late_ocr_result_count"] == 2
    assert out["late_rfid_result_count"] == 3
    assert out["camera_reconnect_count"] == 5
    assert out["plc_ack_timeout_count"] == 1
    assert out["active_session"] is True
    assert out["active_session_id"] == 42
    assert out["pending_trigger_count"] == 7


def test_extract_metrics_derives_active_session_from_existing_session_fields():
    """session.active/session.id ALLAQACHON pipeline.py:649-673 da mavjud —
    Agent1 metrics= qo'shmagan holatda ham shulardan foydalanamiz."""
    fake_status = {"session": {"active": True, "id": 7, "vin_done": False, "rfid_done": False}}
    out = _extract_metrics(fake_status)
    assert out["active_session"] is True
    assert out["active_session_id"] == 7


def test_extract_metrics_derives_camera_last_frame_age():
    import time
    now = time.time()
    fake_status = {"last_frame_ts": now - 12.0}
    out = _extract_metrics(fake_status)
    assert out["camera_last_frame_age"] is not None
    assert 10.0 <= out["camera_last_frame_age"] <= 20.0


def test_metrics_endpoint_returns_required_keys(client, monkeypatch):
    _authed(client, monkeypatch)

    class _FakePipeline:
        def status(self):
            return {
                "session": {"active": False, "id": 3},
                "metrics": {"dropped_trigger_count": 2, "pending_trigger_count": 0},
            }

    monkeypatch.setattr(server_mod, "pipeline", _FakePipeline())
    r = client.get("/api/metrics")
    assert r.status_code == 200
    body = r.json()
    for k in _REQUIRED_KEYS:
        assert k in body
    assert body["dropped_trigger_count"] == 2
    assert body["active_session_id"] == 3


def test_metrics_endpoint_requires_auth(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    r = client.get("/api/metrics")
    assert r.status_code == 401
