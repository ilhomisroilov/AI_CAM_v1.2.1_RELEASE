"""
tests/test_health.py
============================================================
P3 — /health yo'q, barcha status endpoint auth ortida (audit finding:
server.py:111,123-124). Ommaviy liveness endpoint qo'shildi — secret/IP/
parol QAYTARMAYDI, auth talab qilinmaydi.
"""
from __future__ import annotations

import backend.config as config_mod
import backend.server as server_mod


def test_health_accessible_without_auth(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "ok"
    runtime = body["components"]["runtime"]
    assert runtime["effective_profile"] in {"cpu", "cpu-fallback", "mixed", "gpu"}
    assert "cuda_available" in runtime["torch"]
    assert "cuda_available" in runtime["paddle"]
    assert "device_visible" in runtime["paddle"]
    assert "compute_ready" in runtime["paddle"]
    assert "ocr_preload" in runtime


def test_health_returns_no_secrets_or_ip(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.CAMERA, "password", "SuperSecretCam1")
    monkeypatch.setattr(config_mod.RFID, "password", "SuperSecretRfid1")
    monkeypatch.setattr(config_mod.PLC, "ip", "10.123.40.99")
    monkeypatch.setattr(config_mod.RFID, "ip_address", "10.123.90.172")

    r = client.get("/health")
    text = r.text
    assert "SuperSecretCam1" not in text
    assert "SuperSecretRfid1" not in text
    assert "10.123.40.99" not in text
    assert "10.123.90.172" not in text


def test_other_status_endpoints_still_require_auth(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    r = client.get("/api/status")
    assert r.status_code == 401


def test_health_is_503_when_required_gpu_runtime_is_not_ready(client, monkeypatch):
    monkeypatch.setattr(config_mod.AUTH, "enabled", False)
    monkeypatch.setattr(
        server_mod,
        "runtime_health_summary",
        lambda _report: {
            "status": "critical",
            "ready": False,
            "effective_profile": "mixed",
            "gpu_pipeline_ready": False,
            "failure_codes": ["CUDNN_LOAD_FAILED"],
            "policy": {"ocr_gpu_effective": False, "gpu_required": True},
            "ocr_preload": {"attempted": False, "ready": None},
        },
    )

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "critical"
    assert response.json()["components"]["runtime"]["failure_codes"] == [
        "CUDNN_LOAD_FAILED"
    ]
