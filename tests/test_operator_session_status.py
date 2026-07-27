"""
tests/test_operator_session_status.py
============================================================
P1 — Operator UI stale VIN (audit finding: dashboard.js:57,68-69 global
ocr.last_vin ni "joriy" muvaffaqiyat sifatida ko'rsatadi; session.* maydonlarini
UMUMAN o'qimaydi. pipeline.py:317-319: TIMEOUT sessiyada last_vin yangilanmaydi
-> operator ESKI VIN'ni muvaffaqiyat deb o'qiydi).

Bu agent backend/pipeline.py ga TEGMAYDI (Agent1/2 mas'uliyati) — session.*
maydonlar pipeline.py:649-673 da ALLAQACHON mavjud (active/id/remaining_sec/
vin_done/rfid_done). Bu yerda API/status darajasida tekshiramiz:
  1) /api/status har doim session.* maydonlarni qaytaradi (mavjud kontrakt)
  2) TIMEOUT stsenariysi (ocr.last_vin ESKI, session.vin_done=False) da
     _extract_metrics/active_session hisob-kitobi hech qachon eski VIN'ni
     "joriy" deb ko'rsatmaydi (faqat session.* dan foydalanadi, ocr.last_vin
     dan EMAS)
  3) Frontend'ning stale-VIN muammosi qanday tuzatilgani (dashboard.js) ni
     hujjatli tarzda tasdiqlaydi: session obyekti mavjud bo'lganda dashboard
     ocr.last_vin ni "Last completed" panelida alohida ko'rsatadi.
"""
from __future__ import annotations

import backend.config as config_mod
import backend.server as server_mod
from backend.server import _extract_metrics

from conftest import login


def _authed(client, monkeypatch, username="sessionuser", password="SessionPass1"):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", username)
    monkeypatch.setattr(config_mod.AUTH, "password", password)
    r = login(client, username, password)
    assert r.status_code == 303
    return client


def test_status_contract_includes_session_fields(client, monkeypatch):
    """pipeline.status() ning session.* mavjud kontraktini (active/id/
    remaining_sec/vin_done/rfid_done) /api/status orqali tasdiqlaydi."""
    _authed(client, monkeypatch)

    class _FakePipeline:
        def status(self):
            return {
                "camera_connected": True, "processing": True, "yolo_ready": True,
                "stats": {}, "last_frame_ts": 0,
                "ocr": {"last_vin": "OLDVIN1234567890X", "last_conf": 0.97},
                "session": {"active": True, "id": 9, "remaining_sec": 42.0,
                           "vin_done": False, "rfid_done": False},
            }

    monkeypatch.setattr(server_mod, "pipeline", _FakePipeline())
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "session" in body
    assert body["session"]["active"] is True
    assert body["session"]["id"] == 9
    assert body["session"]["vin_done"] is False


def test_stale_last_vin_is_not_reported_as_active_session_success():
    """
    TIMEOUT stsenariysi: global ocr.last_vin ESKI muvaffaqiyatli o'qishdan
    qolgan (masalan oldingi avtomobil), lekin JORIY sessiya hali vin_done=False
    holatida (yangi kuzov hali TIMEOUT bo'lmagan yoki TIMEOUT bo'lgan).
    _extract_metrics/active_session FAQAT session.* dan hisoblanadi — ocr.last_vin
    HECH QACHON "joriy" holat sifatida ishlatilmaydi.
    """
    fake_status = {
        "ocr": {"last_vin": "OLDVIN1234567890X", "last_conf": 0.97},
        "session": {"active": True, "id": 11, "vin_done": False, "rfid_done": False,
                    "remaining_sec": 5.0},
    }
    out = _extract_metrics(fake_status)
    # active_session/active_session_id session.* dan olinadi — ocr.last_vin
    # bilan chalkashtirilmaydi.
    assert out["active_session"] is True
    assert out["active_session_id"] == 11
    # metrics chiqishida ocr.last_vin degan maydon UMUMAN yo'q — global
    # "oxirgi VIN" bilan "joriy sessiya holati" ANIQ ajratilgan.
    assert "ocr" not in out
    assert "last_vin" not in out


def test_timeout_session_reports_not_done_even_with_stale_global_last_vin():
    fake_status = {
        "ocr": {"last_vin": "OLDVIN1234567890X", "last_conf": 0.97},
        "session": {"active": False, "id": 12, "vin_done": False, "rfid_done": False,
                    "remaining_sec": 0.0},
    }
    out = _extract_metrics(fake_status)
    assert out["active_session"] is False
    assert out["active_session_id"] == 12
