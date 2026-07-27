"""
============================================================
test_capture_window_recovery.py — Production incident reproduction
============================================================
INCIDENT (real production loglaridan):

    D2222 trigger queue'ga tushgan
    -> oldingi session 30 soniyalik HARD_DEADLINE gacha OCHIQ qolgan
    -> yangi session 20-22 soniya KECH boshlangan
    -> yangi kuzovning capture oynasi YO'QOLGAN

Sabab: `capture ownership` va `session finalization` bir-biriga qattiq
bog'langan. Plastinka kadrdan chiqib capture ALLAQACHON tugagan bo'lsa ham,
navbatdagi trigger faqat `_finalize_session()` OXIRIDA boshlanadi, u esa
`hold_until_deadline=true` + `require_rfid=true` bo'lganda deadline gacha
kutadi (`_maybe_complete_session` erta yopishni rad etadi).

Bu fayl AYNAN shu xatti-harakatni reproduksiya qiladi:

  1. Capture tugagach (plate exit) capture ownership BO'SHASHI kerak.
  2. Navbatdagi trigger oldingi sessionning DB finalization'ini KUTMASLIGI kerak.
  3. Haddan tashqari kech boshlangan trigger noto'g'ri kuzovni o'qimasligi —
     MISSED_CAPTURE_WINDOW bilan yakunlanishi kerak.
  4. Oldingi session natijasi (VIN/RFID) yangi sessionga O'TMASLIGI kerak.

Hech qanday real qurilma/DB ishlatilmaydi (tmp_db + fake kamera/OCR).
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from backend import config as cfg

from conftest import FakeRFIDService, make_ocr_result, wait_until


# Real production konfiguratsiyasi: sessiya deadline gacha ushlab turiladi va
# RFID majburiy. Aynan shu kombinatsiya incidentni keltirib chiqargan.
# `timeout_sec` testda QISQARTIRILADI (30s emas) — buggy xatti-harakat 5s
# kutish bilan namoyon bo'ladi, test esa tez ishlaydi.
PRODUCTION_LIKE_TIMEOUT_SEC = 5.0
CAPTURE_HANDOVER_BUDGET_SEC = 1.0     # spec: <= 500 ms; testda 2x zaxira


@pytest.fixture
def production_like(pipeline_instance, monkeypatch):
    """Incident paytidagi real config: hold_until_deadline + require_rfid + RFID yoqilgan."""
    p = pipeline_instance
    monkeypatch.setattr(cfg.SESSION, "hold_until_deadline", True)
    monkeypatch.setattr(cfg.SESSION, "require_rfid", True)
    monkeypatch.setattr(cfg.SESSION, "timeout_sec", PRODUCTION_LIKE_TIMEOUT_SEC)
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", False)
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    # Kamera oqimini pauza qilish real client'ga tegmasin.
    monkeypatch.setattr(p, "pause_camera_stream", lambda: None)
    monkeypatch.setattr(p, "resume_camera_stream", lambda: None)
    monkeypatch.setattr(p, "_ensure_camera_stream", lambda: True)
    p.rfid_service = FakeRFIDService(behavior="no_tag")
    return p


def simulate_plate_exit(p) -> None:
    """
    Plastinka kadrdan chiqishini REAL kod yo'li orqali simulyatsiya qiladi:
    `_handle_trigger(frame, [])` ni rearm chegarasidan ko'proq marta chaqiradi.
    """
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    p._plate_locked = True                      # OCR shu hodisa uchun yuborilgan
    for _ in range(int(cfg.DETECTION.ocr_rearm_absent_frames) + 1):
        p._handle_trigger(frame, [])


def test_capture_released_on_plate_exit(production_like):
    """1) Plate exit capture ownership'ni bo'shatadi (session hali natija kutayotgan bo'lsa ham)."""
    p = production_like
    p.plc_on()
    session_a = p._active_session_id
    assert session_a is not None

    simulate_plate_exit(p)

    assert p.capture_owner_id is None, (
        "plate exit'dan keyin capture ownership BO'SHASHI kerak — "
        "session natija kutayotgan bo'lsa ham kamera keyingi kuzov uchun ozod bo'lishi shart"
    )
    # Session hali terminal EMAS: OCR/RFID natijasi fonda kutilyapti.
    assert p._sessions[session_a].state.value not in ("SUCCESS", "TIMEOUT", "FAILED")


def test_queued_trigger_starts_without_waiting_for_finalization(production_like):
    """
    2) INCIDENT REPRODUKSIYASI — navbatdagi trigger oldingi sessionning
       HARD_DEADLINE finalization'ini KUTMASLIGI kerak.
    """
    p = production_like
    p.plc_on()
    session_a = p._active_session_id

    p.plc_on()                       # ikkinchi kuzov — capture hali band, navbatga
    assert len(p._pending_triggers) == 1

    simulate_plate_exit(p)           # birinchi kuzov kadrdan chiqdi -> capture bo'sh

    started = wait_until(
        lambda: p.capture_owner_id is not None and p.capture_owner_id != session_a,
        timeout=CAPTURE_HANDOVER_BUDGET_SEC,
    )
    assert started, (
        f"navbatdagi trigger capture bo'shagach <= {CAPTURE_HANDOVER_BUDGET_SEC}s ichida "
        f"boshlanishi kerak; hozir u oldingi sessiyaning "
        f"{PRODUCTION_LIKE_TIMEOUT_SEC}s HARD_DEADLINE'ini kutyapti (INCIDENT)"
    )
    assert len(p._pending_triggers) == 0
    # Oldingi session hamon natija kutmoqda (finalize bo'lmagan) — bu MAQSADLI.
    assert p._sessions[session_a].state.value not in ("SUCCESS", "TIMEOUT", "FAILED")


def test_previous_session_result_does_not_leak_into_new_capture(production_like):
    """4) Oldingi sessionning VIN natijasi yangi capture sessionga BOG'LANMASLIGI kerak."""
    p = production_like
    p.plc_on()
    session_a = p._active_session_id
    p.plc_on()
    simulate_plate_exit(p)
    assert wait_until(lambda: p.capture_owner_id not in (None, session_a),
                      timeout=CAPTURE_HANDOVER_BUDGET_SEC)
    session_b = p.capture_owner_id

    # Session A uchun kechikkan OCR natijasi keladi.
    p._on_ocr_result(make_ocr_result(session_a, vin="NSTFA814ATJ111111"))

    assert p._sessions[session_b].vin is None, (
        "oldingi sessiyaning VIN natijasi YANGI sessiyaga yozilmasligi kerak"
    )
    assert p._sessions[session_a].vin == "NSTFA814ATJ111111"


def test_excessively_delayed_trigger_is_missed_capture_window(production_like, monkeypatch):
    """3) Juda kech boshlangan trigger noto'g'ri kuzovni o'qimaydi -> MISSED_CAPTURE_WINDOW."""
    p = production_like
    monkeypatch.setattr(cfg.SESSION, "max_capture_delay_ms", 300, raising=False)

    p.plc_on()
    session_a = p._active_session_id
    p.plc_on()                                   # navbatga tushdi
    assert len(p._pending_triggers) == 1

    # Navbatda 300ms dan uzoq turdi -> kuzov allaqachon o'tib ketgan.
    time.sleep(0.45)
    simulate_plate_exit(p)

    assert wait_until(lambda: len(p._pending_triggers) == 0,
                      timeout=CAPTURE_HANDOVER_BUDGET_SEC), "navbat bo'shatilishi kerak"

    missed = [s for s in p._sessions.values()
              if s.session_id != session_a
              and s.failure_reason == "MISSED_CAPTURE_WINDOW"]
    assert missed, (
        "capture oynasi yo'qolgan trigger MISSED_CAPTURE_WINDOW bilan yakunlanishi kerak — "
        "kech boshlanib NOTO'G'RI kuzovni o'qimasligi shart"
    )
    assert p.capture_owner_id is None, "missed-window session capture'ni ushlab turmasligi kerak"


def test_one_terminal_db_row_per_accepted_trigger(production_like, tmp_db):
    """9) Har qabul qilingan trigger uchun AYNAN bitta terminal DB yozuvi."""
    from backend.database import db

    p = production_like
    p.plc_on()
    session_a = p._active_session_id
    p.plc_on()
    simulate_plate_exit(p)
    assert wait_until(lambda: p.capture_owner_id not in (None, session_a),
                      timeout=CAPTURE_HANDOVER_BUDGET_SEC)
    session_b = p.capture_owner_id

    # `TEST_FORCE_CLOSE` — hold_until_deadline guard'i tan oladigan yagona test sababi.
    p._finalize_session(session_a, reason="TEST_FORCE_CLOSE")
    p._finalize_session(session_b, reason="TEST_FORCE_CLOSE")
    p._finalize_session(session_a, reason="TEST_FORCE_CLOSE")    # idempotent bo'lishi kerak

    rows = db.get_records(limit=100)
    session_ids = [r["session_id"] for r in rows if r["session_id"] in (session_a, session_b)]
    assert sorted(session_ids) == sorted([session_a, session_b]), (
        f"har trigger uchun aynan bitta yozuv bo'lishi kerak, olindi: {session_ids}"
    )
