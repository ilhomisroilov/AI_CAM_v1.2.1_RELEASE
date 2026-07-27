"""
============================================================
test_rfid_stale_events.py — EPC ownership across session boundaries
============================================================
INCIDENT: navbatdan boshlangan sessiyalar OLDINGI sessiyaga tegishli EPC ni
qayta olgan. Bu traceability xavfi: kuzov A ning tegi kuzov B ning yozuviga
biriktirilib qolishi mumkin.

R700 oqimida teg kamera maydonida turgan ekan qayta-qayta ko'rinadi. Agar
tegning `first_seen_at` vaqti YANGI sessiyaning RFID o'qish oynasi
boshlanishidan OLDIN bo'lsa — bu OLDINGI kuzovdan qolgan dalil, yangi kuzov
uchun dalil EMAS.

Qoida: yangi sessiya uchun FRESH RFID dalili talab qilinadi.
Eskirgan event rad etiladi va sabab saqlanadi (jim yutilmaydi).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from backend import config as cfg

from conftest import wait_until


EPC = "300833B2DDD9014000000001"
NUMBER = "1001"


class ReplayRFIDService:
    """`first_seen_at` ni test to'liq boshqaradigan soxta RFID xizmati."""

    def __init__(self):
        self.first_seen_offset_sec = 0.0     # sessiya boshlanishiga NISBATAN
        self.calls: list = []

    def trigger_read(self, on_done=None, stop_event=None, deadline=None,
                     session_id=None, started_at=None) -> bool:
        from backend.rfid.rfid_service import RFIDResult
        self.calls.append(session_id)
        base = datetime.fromtimestamp(started_at or time.time())
        first_seen = base + timedelta(seconds=self.first_seen_offset_sec)
        if on_done is not None:
            on_done(RFIDResult(
                session_id=session_id, epc=EPC, number=NUMBER, antenna=1, rssi=-40.0,
                first_seen_at=first_seen, received_at=datetime.now(),
            ))
        return True

    def status(self) -> dict:
        return {"enabled": True, "mode": "replay", "connected": True, "state": "IDLE"}


@pytest.fixture
def rfid_pipeline(pipeline_instance, monkeypatch):
    p = pipeline_instance
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    monkeypatch.setattr(cfg.SESSION, "require_rfid", True)
    monkeypatch.setattr(cfg.SESSION, "hold_until_deadline", False)
    monkeypatch.setattr(cfg.SESSION, "timeout_sec", 5.0)
    monkeypatch.setattr(p, "pause_camera_stream", lambda: None)
    monkeypatch.setattr(p, "_ensure_camera_stream", lambda: True)
    p.rfid_service = ReplayRFIDService()
    return p


def test_fresh_rfid_evidence_is_accepted(rfid_pipeline):
    """Sessiya oynasi ICHIDA birinchi marta ko'rilgan teg — qabul qilinadi."""
    p = rfid_pipeline
    p.rfid_service.first_seen_offset_sec = +0.2      # o'qish boshlangandan KEYIN
    p.plc_on()
    sid = p._active_session_id

    assert wait_until(lambda: p._sessions[sid].rfid_epc == NUMBER, timeout=2.0), \
        "yangi (fresh) RFID dalili qabul qilinishi kerak"
    assert p._sessions[sid].rfid_rejection_reason is None


def test_stale_rfid_event_from_previous_vehicle_is_rejected(rfid_pipeline):
    """
    Teg sessiya o'qish oynasi BOSHLANISHIDAN OLDIN ko'rilgan — bu oldingi
    kuzovdan qolgan dalil. Yangi sessiyaga BIRIKTIRILMASLIGI kerak.
    """
    p = rfid_pipeline
    p.rfid_service.first_seen_offset_sec = -8.0      # 8s oldin ko'rilgan (oldingi kuzov)
    p.plc_on()
    sid = p._active_session_id

    # Qisqa kutish — natija kelib ulgursin.
    time.sleep(0.2)
    session = p._sessions[sid]
    assert session.rfid_epc != NUMBER, (
        "oldingi kuzovdan qolgan EPC yangi sessiyaga biriktirilmasligi kerak "
        "(traceability xavfi)"
    )
    assert session.rfid_rejection_reason == "STALE_RFID_EVENT", (
        "rad etish sababi saqlanishi kerak (jim yutilmasin)"
    )
    assert session.rfid_reader_event_at is not None, "reader_event_at saqlanishi kerak"
    assert session.rfid_read_started_at is not None, "session_read_started_at saqlanishi kerak"


def test_same_epc_consecutive_sessions_requires_fresh_evidence(rfid_pipeline):
    """
    Bir xil EPC ketma-ket ikki sessiyada: ikkinchisi FRESH dalil bo'lmasa
    avtomatik qabul qilinmaydi.
    """
    p = rfid_pipeline
    # 1-sessiya: fresh dalil -> qabul.
    p.rfid_service.first_seen_offset_sec = +0.1
    p.plc_on()
    sid_a = p._active_session_id
    assert wait_until(lambda: p._sessions[sid_a].rfid_epc == NUMBER, timeout=2.0)
    p._finalize_session(sid_a, reason="TEST_FORCE_CLOSE")

    # 2-sessiya: AYNI teg hamon maydonda, lekin yangi dalil YO'Q (eski first_seen).
    p.rfid_service.first_seen_offset_sec = -12.0
    p.plc_on()
    sid_b = p._active_session_id
    assert sid_b != sid_a
    time.sleep(0.2)

    assert p._sessions[sid_b].rfid_epc != NUMBER, (
        "ayni EPC ikkinchi sessiyaga FRESH dalilsiz biriktirilmasligi kerak"
    )
    assert p._sessions[sid_b].rfid_rejection_reason == "STALE_RFID_EVENT"
    # 1-sessiyaning natijasi buzilmagan.
    assert p._sessions[sid_a].rfid_epc == NUMBER


def test_stale_tolerance_is_configurable(rfid_pipeline, monkeypatch):
    """Chegara config orqali boshqariladi (hardcoded emas)."""
    p = rfid_pipeline
    # Katta tolerance -> 8s oldin ko'rilgan teg ham qabul qilinadi.
    monkeypatch.setattr(cfg.RFID, "stale_event_tolerance_ms", 30000, raising=False)
    p.rfid_service.first_seen_offset_sec = -8.0
    p.plc_on()
    sid = p._active_session_id

    assert wait_until(lambda: p._sessions[sid].rfid_epc == NUMBER, timeout=2.0), \
        "tolerance oshirilganda ayni event qabul qilinishi kerak (config-driven)"
