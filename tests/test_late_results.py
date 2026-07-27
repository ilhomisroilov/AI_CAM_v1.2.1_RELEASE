"""
============================================================
test_late_results.py — kech kelgan / noma'lum sessiya natijalarini rad etish
============================================================
Talab (arxitektura spetsifikatsiyasi): rad etilishi kerak bo'lgan holatlar —
finalized sessiya natijasi, unknown session, boshqa active session natijasi,
deadline'dan keyingi natija. Har biri ALOHIDA log kategoriyasi bilan:
  LATE_RESULT_REJECTED, SESSION_MISMATCH, FINALIZED_SESSION_RESULT_REJECTED,
  UNKNOWN_SESSION_RESULT — va mos metrikalar: late_ocr_results_total,
  late_rfid_results_total, session_mismatch_total.

Shuningdek (test 6): timeout'dan keyin kelgan OCR natijasi IKKINCHI DB
yozuvi yaratmasligi kerak (P0-2 asosiy topilmasi).
"""
from __future__ import annotations

import logging
import time

from backend import config as cfg
from backend.database import db
from backend.pipeline import Session, SessionState

from conftest import make_ocr_result, wait_until


def test_ocr_after_timeout_does_not_create_second_record(pipeline_instance, monkeypatch):
    """6) Timeout'dan keyin kelgan OCR ikkinchi record yaratmaydi (session TERMINAL -> rad etiladi)."""
    p = pipeline_instance
    monkeypatch.setattr(cfg.SESSION, "timeout_sec", 0.15)
    p.plc_on()
    sid = p._active_session_id

    assert wait_until(lambda: p._sessions[sid].state.value == "TIMEOUT", timeout=3.0), \
        "watchdog TIMEOUT bilan finalize qilishi kerak edi"

    # Kech kelgan OCR natijasi — rad etilishi va DB'da IKKINCHI yozuv yaratmasligi kerak.
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ123456"))

    records = [r for r in db.get_all_records() if r["session_id"] == sid]
    assert len(records) == 1, "session uchun DB'da faqat BITTA yozuv bo'lishi kerak"
    assert records[0]["status"] == "TIMEOUT"
    assert records[0]["detected_vin"] == "NO_READ"      # kech kelgan VIN yozilmagan


def test_unknown_session_result_rejected(pipeline_instance, caplog):
    """14) Noma'lum session_id bilan kelgan natija rad etiladi (UNKNOWN_SESSION_RESULT)."""
    caplog.set_level(logging.ERROR)
    p = pipeline_instance

    p._on_ocr_result(make_ocr_result("hech-qachon-mavjud-bolmagan-session", vin="NSTFA814ATJ123456"))

    assert p._metrics["late_ocr_results_total"] == 1
    assert any("UNKNOWN_SESSION_RESULT" in r.message for r in caplog.records)
    # Hech qanday DB yozuvi qilinmagan.
    assert db.count_records() == 0


def test_session_mismatch_rejects_known_but_non_active_session(pipeline_instance, caplog):
    """
    SESSION_MISMATCH: session_id MA'LUM (tarixda bor) va TERMINAL EMAS, lekin
    joriy FAOL sessiya emas. (Oddiy oqimda bunday holat sodir bo'lmaydi — bir
    vaqtda faqat bitta faol, terminal-bo'lmagan sessiya bo'ladi — shu sababli
    ownership tekshiruvini ALOHIDA, to'g'ridan-to'g'ri sinaymiz.)
    """
    p = pipeline_instance
    p.plc_on()

    ghost = Session(session_id="ghost-session-xyz", trigger_sequence=999,
                    triggered_at=time.time(), started_at=time.time(),
                    deadline=time.monotonic() + 90.0, state=SessionState.ACTIVE)
    with p._session_lock:
        p._sessions["ghost-session-xyz"] = ghost

    caplog.set_level(logging.WARNING)
    p._on_ocr_result(make_ocr_result("ghost-session-xyz", vin="NSTFA814ATJ123456"))

    assert ghost.vin is None
    assert p._metrics["session_mismatch_total"] == 1
    assert any("SESSION_MISMATCH" in r.message for r in caplog.records)


def test_finalized_session_result_rejected(pipeline_instance, caplog):
    """FINALIZED_SESSION_RESULT_REJECTED: sessiya allaqachon SUCCESS/TIMEOUT bo'lgach kelgan natija."""
    p = pipeline_instance
    p.plc_on()
    sid = p._active_session_id
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ123456"))
    assert p._sessions[sid].state.value == "SUCCESS"

    caplog.set_level(logging.WARNING)
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ654321"))   # allaqachon yakunlangan

    assert p._sessions[sid].vin == "NSTFA814ATJ123456", "yakunlangan sessiya o'zgarmasligi kerak"
    assert any("FINALIZED_SESSION_RESULT_REJECTED" in r.message for r in caplog.records)
    assert p._metrics["late_ocr_results_total"] == 1


def test_late_result_rejected_after_deadline_before_watchdog_finalizes(pipeline_instance, caplog):
    """
    LATE_RESULT_REJECTED: deadline allaqachon o'tgan, lekin watchdog hali
    finalize qilmagan (tor race oynasi) — natija baribir rad etilishi kerak.
    """
    p = pipeline_instance
    p.plc_on()
    sid = p._active_session_id

    with p._session_lock:
        p._sessions[sid].deadline = time.monotonic() - 1.0    # deadline "allaqachon o'tgan"

    caplog.set_level(logging.WARNING)
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ123456"))

    assert p._sessions[sid].vin is None
    assert any("LATE_RESULT_REJECTED" in r.message for r in caplog.records)
    assert p._metrics["late_ocr_results_total"] == 1
