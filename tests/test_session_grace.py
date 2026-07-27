"""
Rapid MVP — D2222/D2223 dual-signal GRACE-based close state-machine testlari.

Bu testlar `SESSION.exit_signal_enabled=True` (yangi MVP rejim) da:
  * D2223 (EXIT) sessiyani DARHOL yopmasligini (EXIT_SEEN -> GRACE_WAIT);
  * grace oynasi ICHIDA kelgan OCR/RFID natijasi QABUL qilinishini;
  * grace TUGAGACH kelgan natija RAD etilishini;
  * D2223 kelmasa hard-deadline failsafe ishlashini;
  * ketma-ket kuzovlar (overlap: A grace'da, B active) aralashmasligini;
  * har D2222 uchun AYNAN BITTA DB yozuvi bo'lishini;
  * duplicate D2223 idempotent ekanini;
  * VIN yoki RFID (biri) bo'lsa PARTIAL_SUCCESS bo'lishini
tekshiradi.

HECH QANDAY real qurilma ULANMAYDI, `data/ai_cam.db` ISHTIROK ETMAYDI
(tmp_db + monkeypatched kamera/OCR/RFID). Deterministik — tarmoqqa chiqmaydi.
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from backend import config as cfg
from backend.database import db
from backend.pipeline import SessionState
from conftest import make_ocr_result, wait_until


# ------------------------------------------------------------------
# Yordamchilar
# ------------------------------------------------------------------
def _enable_exit_mode(monkeypatch, grace_ms=1000, max_dur=30.0):
    """MVP exit-signal rejimini yoqadi (config singletoniga surgical)."""
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "post_exit_grace_ms", grace_ms)
    monkeypatch.setattr(cfg.SESSION, "max_session_duration_sec", max_dur)
    monkeypatch.setattr(cfg.SESSION, "allow_result_after_d2223", True)


def _rfid_result(session_id, number="1234", epc="000000000001234"):
    from backend.rfid.rfid_service import RFIDResult
    now = datetime.now()
    return RFIDResult(session_id=session_id, epc=epc, number=number,
                      antenna=1, rssi=-42.0, first_seen_at=now, received_at=now)


def _active(p):
    with p._session_lock:
        return p._sessions.get(p._active_session_id) if p._active_session_id else None


# ------------------------------------------------------------------
# 1) D2223 DARHOL yopmaydi — EXIT_SEEN -> GRACE_WAIT
# ------------------------------------------------------------------
def test_d2223_does_not_immediately_finalize(pipeline_instance, monkeypatch):
    _enable_exit_mode(monkeypatch, grace_ms=1500)
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    assert sess is not None and sess.state == SessionState.ACTIVE

    p.on_exit_signal()   # D2223

    # DARHOL yopilmaydi: GRACE_WAIT, terminal EMAS, finalize bo'lmagan.
    assert sess.state == SessionState.GRACE_WAIT
    assert sess.finalized_at is None
    assert sess.d2223_at is not None
    # Capture-owner bo'shadi (keyingi D2222 yangi sessiya ocha oladi).
    assert p._active_session_id is None
    # Grace tugagach deterministik yopiladi.
    assert wait_until(lambda: sess.state in (SessionState.TIMEOUT,
                                             SessionState.PARTIAL_SUCCESS,
                                             SessionState.SUCCESS))


# ------------------------------------------------------------------
# 2) Grace ICHIDA kelgan OCR (VIN) QABUL qilinadi
# ------------------------------------------------------------------
def test_grace_accepts_ocr_after_d2223(pipeline_instance, monkeypatch):
    _enable_exit_mode(monkeypatch, grace_ms=1500)
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id

    p.on_exit_signal()
    assert sess.state == SessionState.GRACE_WAIT

    # Grace ichida VIN keladi -> QABUL qilinadi (RFID o'chiq -> darhol SUCCESS).
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ100002"))
    assert sess.vin == "NSTFA814ATJ100002"
    assert wait_until(lambda: sess.state == SessionState.SUCCESS)
    assert db.count_records() == 1
    assert db.get_all_records()[0]["status"] == "SUCCESS"


# ------------------------------------------------------------------
# 3) Grace TUGAGACH kelgan OCR RAD etiladi (late-result)
# ------------------------------------------------------------------
def test_result_after_grace_rejected(pipeline_instance, monkeypatch):
    _enable_exit_mode(monkeypatch, grace_ms=150)
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id

    p.on_exit_signal()
    # Grace tugab, sessiya terminal bo'lishini kutamiz (VIN yo'q -> TIMEOUT).
    assert wait_until(lambda: sess.state in _terminal())
    late_before = p._metrics["late_ocr_results_total"]
    recs_before = db.count_records()

    # Endi kech OCR keladi -> RAD etiladi, VIN yozilmaydi, yangi DB yozuvi yo'q.
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ100003"))
    assert sess.vin is None
    assert p._metrics["late_ocr_results_total"] == late_before + 1
    assert db.count_records() == recs_before   # ikkinchi yozuv YO'Q


# ------------------------------------------------------------------
# 4) Grace ICHIDA kelgan RFID QABUL qilinadi
# ------------------------------------------------------------------
def test_grace_accepts_rfid_after_d2223(pipeline_instance, monkeypatch):
    _enable_exit_mode(monkeypatch, grace_ms=400)
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    p = pipeline_instance
    p.rfid_service = None       # _start_session RFID blokini o'tkazadi (qo'lda inject qilamiz)
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id

    p.on_exit_signal()
    assert sess.state == SessionState.GRACE_WAIT

    # Grace ichida RFID keladi -> QABUL qilinadi (VIN yo'q -> PARTIAL_SUCCESS kutiladi).
    p._on_rfid_result(_rfid_result(sid, number="4321"))
    assert sess.rfid_epc == "4321"
    assert wait_until(lambda: sess.state == SessionState.PARTIAL_SUCCESS)
    assert sess.failure_reason == "RFID_OK_VIN_TIMEOUT"


# ------------------------------------------------------------------
# 5) D2223 kelmasa — hard-deadline failsafe yopadi (D2223_TIMEOUT)
# ------------------------------------------------------------------
def test_hard_deadline_failsafe_when_no_d2223(pipeline_instance, monkeypatch):
    _enable_exit_mode(monkeypatch, grace_ms=1000, max_dur=1.0)
    p = pipeline_instance
    t0 = time.monotonic()
    p.plc_on()
    sess = _active(p)

    # D2223 HECH QACHON kelmaydi -> hard_deadline (1s) failsafe yopadi.
    assert wait_until(lambda: sess.state in _terminal(), timeout=4.0)
    elapsed = time.monotonic() - t0
    # Soft deadline (timeout_sec=90s) EMAS, hard_deadline (~1s) ishladi.
    assert elapsed < 3.0
    assert sess.state == SessionState.TIMEOUT
    assert db.count_records() == 1


# ------------------------------------------------------------------
# 6) Duplicate D2223 idempotent — bitta grace, bitta DB yozuvi
# ------------------------------------------------------------------
def test_duplicate_d2223_idempotent(pipeline_instance, monkeypatch):
    _enable_exit_mode(monkeypatch, grace_ms=1200)
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id

    p.on_exit_signal()
    p.on_exit_signal()   # takroriy — no-op bo'lishi kerak
    p.on_exit_signal()
    assert sess.state == SessionState.GRACE_WAIT

    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ100006"))
    assert wait_until(lambda: sess.state == SessionState.SUCCESS)
    assert db.count_records() == 1


# ------------------------------------------------------------------
# 7) Overlap — A grace'da turganda B (yangi D2222) boshlanadi, aralashmaydi
# ------------------------------------------------------------------
def test_overlap_new_d2222_during_grace(pipeline_instance, monkeypatch):
    _enable_exit_mode(monkeypatch, grace_ms=1500)
    p = pipeline_instance
    p.plc_on()
    sess_a = _active(p)
    sid_a = sess_a.session_id

    p.on_exit_signal()                     # A -> GRACE_WAIT, capture-owner bo'shadi
    assert sess_a.state == SessionState.GRACE_WAIT

    p.plc_on()                             # B -> yangi active sessiya
    sess_b = _active(p)
    sid_b = sess_b.session_id
    assert sid_b != sid_a
    assert sess_b.state == SessionState.ACTIVE

    # A ning natijasi A ga, B ning natijasi B ga (aralashmaydi).
    p._on_ocr_result(make_ocr_result(sid_a, vin="NSTFA814ATJ1000AA"))
    p.on_exit_signal(sid_b)
    p._on_ocr_result(make_ocr_result(sid_b, vin="NSTFA814ATJ1000BB"))

    assert wait_until(lambda: sess_a.state == SessionState.SUCCESS)
    assert wait_until(lambda: sess_b.state == SessionState.SUCCESS)
    assert sess_a.vin == "NSTFA814ATJ1000AA"
    assert sess_b.vin == "NSTFA814ATJ1000BB"
    # Aynan ikkita, ALOHIDA session_id li DB yozuvi.
    rows = db.get_all_records()
    assert len(rows) == 2
    assert {r["session_id"] for r in rows} == {sid_a, sid_b}


# ------------------------------------------------------------------
# 8) PARTIAL_SUCCESS — faqat VIN (RFID timeout)
# ------------------------------------------------------------------
def test_partial_success_vin_only(pipeline_instance, monkeypatch):
    _enable_exit_mode(monkeypatch, grace_ms=250)
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    p = pipeline_instance
    p.rfid_service = None       # RFID hech qachon kelmaydi
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id

    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ100008"))
    # ACTIVE holatda exit-mode da erta yopilmaydi (D2223 kutiladi).
    assert sess.state == SessionState.ACTIVE

    p.on_exit_signal()          # -> GRACE_WAIT; RFID yo'q -> grace tugagach PARTIAL
    assert wait_until(lambda: sess.state == SessionState.PARTIAL_SUCCESS)
    assert sess.failure_reason == "VIN_OK_RFID_TIMEOUT"
    row = db.get_all_records()[0]
    assert row["status"] == "PARTIAL_SUCCESS"
    assert row["detected_vin"] == "NSTFA814ATJ100008"


# ------------------------------------------------------------------
# 9) Ikkalasi tayyor bo'lsa grace ichida ERTA yopiladi (grace'ni kutmaydi)
# ------------------------------------------------------------------
def test_early_close_when_both_ready_in_grace(pipeline_instance, monkeypatch):
    _enable_exit_mode(monkeypatch, grace_ms=3000)   # uzun grace
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    p = pipeline_instance
    p.rfid_service = None
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id

    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ100009"))
    p._on_rfid_result(_rfid_result(sid, number="9999"))
    assert sess.state == SessionState.ACTIVE   # D2223 dan oldin erta yopilmaydi

    t0 = time.monotonic()
    p.on_exit_signal()                          # ikkalasi tayyor -> DARHOL SUCCESS
    assert wait_until(lambda: sess.state == SessionState.SUCCESS, timeout=1.0)
    # 3000ms grace'ni KUTMADI (erta yopildi).
    assert time.monotonic() - t0 < 1.0
    assert db.count_records() == 1
    assert db.get_all_records()[0]["status"] == "SUCCESS"


# ------------------------------------------------------------------
# 10) Backward-compat: exit rejimi O'CHIQ bo'lsa on_exit_signal ta'sir qilmaydi
# ------------------------------------------------------------------
def test_exit_mode_disabled_preserves_legacy(pipeline_instance, monkeypatch):
    # exit_signal_enabled default FALSE — VIN muvaffaqiyati eski usulda darhol yopadi.
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id

    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ100010"))
    # Eski xatti-harakat: RFID o'chiq + VIN -> DARHOL SUCCESS (grace yo'q).
    assert wait_until(lambda: sess.state == SessionState.SUCCESS)
    assert db.get_all_records()[0]["status"] == "SUCCESS"


def _terminal():
    return (SessionState.SUCCESS, SessionState.PARTIAL_SUCCESS,
            SessionState.TIMEOUT, SessionState.FAILED, SessionState.CANCELLED)
