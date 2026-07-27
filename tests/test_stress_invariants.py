"""
Rapid MVP — Tier 1 (functional) + §16 PRODUCTION INVARIANT stress testlari.

100+ sessiyani turli tartiblarda (VIN/RFID/D2223 kombinatsiyalari) exit-grace
rejimida haydaydi va spec §16 invariantlarini tekshiradi:

  accepted_D2222  ==  finalized_DB_records
  COUNT(DISTINCT session_id) == COUNT(session_id)        (duplicate yo'q)
  session_mismatch_total == 0
  silent_trigger_drop == 0   (dropped hisoblanadi, jim yo'qolmaydi)

Bu — "READY FOR CONTROLLED HIL TEST" qarori uchun asosiy acceptance gate.
HECH QANDAY real qurilma ULANMAYDI; tmp_path DB; deterministik.
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from backend import config as cfg
from backend.database import db
from backend.pipeline import SessionState
from conftest import make_ocr_result, wait_until

_TERMINAL = (SessionState.SUCCESS, SessionState.PARTIAL_SUCCESS,
             SessionState.TIMEOUT, SessionState.FAILED, SessionState.CANCELLED)


def _rfid(session_id, number="1234"):
    from backend.rfid.rfid_service import RFIDResult
    now = datetime.now()
    return RFIDResult(session_id=session_id, epc="0000" + number, number=number,
                      antenna=1, rssi=-42.0, first_seen_at=now, received_at=now)


def _active(p):
    with p._session_lock:
        return p._sessions.get(p._active_session_id) if p._active_session_id else None


def _run_one(p, sid_seq, pattern):
    """Bitta kuzov sikli. `pattern` natija/tartib kombinatsiyasini belgilaydi."""
    p.plc_on()
    sess = _active(p)
    assert sess is not None
    sid = sess.session_id
    vin = f"NSTFA814ATJ{sid_seq:06d}"

    if pattern == "vin_rfid_exit":
        p._on_ocr_result(make_ocr_result(sid, vin=vin))
        p._on_rfid_result(_rfid(sid))
        p.on_exit_signal()                       # ikkalasi tayyor -> erta SUCCESS
    elif pattern == "rfid_vin_exit":
        p._on_rfid_result(_rfid(sid))
        p._on_ocr_result(make_ocr_result(sid, vin=vin))
        p.on_exit_signal()
    elif pattern == "exit_then_vin_rfid":
        p.on_exit_signal()                       # avval D2223, keyin natija (grace ichida)
        p._on_ocr_result(make_ocr_result(sid, vin=vin))
        p._on_rfid_result(_rfid(sid))
    elif pattern == "exit_vin_only":
        p._on_ocr_result(make_ocr_result(sid, vin=vin))
        p.on_exit_signal()                       # RFID yo'q -> PARTIAL (grace kutadi)
    elif pattern == "exit_rfid_only":
        p._on_rfid_result(_rfid(sid))
        p.on_exit_signal()                       # VIN yo'q -> PARTIAL
    elif pattern == "exit_no_result":
        p.on_exit_signal()                       # hech narsa -> BOTH_TIMEOUT (grace)
    elif pattern == "no_exit_failsafe":
        pass                                     # D2223 yo'q -> hard-deadline failsafe
    # Sessiya terminal bo'lishini kutamiz (deterministik yopilish). Timeout
    # SAXOVATLI (20s): to'liq suite yukida (spawn qilingan OCR processlar + ko'p
    # thread) timer-asosli finalize sekinlashishi mumkin — invariant mantig'i
    # to'g'ri, faqat kutish marjasi yuk ostida yetarli bo'lishi kerak (flakiness
    # oldini olish). Har session baribir aniq terminal holatga yetadi.
    assert wait_until(lambda: sess.state in _TERMINAL, timeout=20.0), \
        f"sessiya #{sid} ({pattern}) terminal bo'lmadi"
    return sess


def test_100_sessions_invariants_hold(pipeline_instance, monkeypatch):
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "post_exit_grace_ms", 80)
    monkeypatch.setattr(cfg.SESSION, "max_session_duration_sec", 0.4)
    monkeypatch.setattr(cfg.SESSION, "allow_result_after_d2223", True)
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    p = pipeline_instance
    p.rfid_service = None       # RFID qo'lda inject qilinadi

    patterns = [
        "vin_rfid_exit", "rfid_vin_exit", "exit_then_vin_rfid",
        "exit_vin_only", "exit_rfid_only", "exit_no_result",
        "vin_rfid_exit", "no_exit_failsafe",
    ]
    N = 100
    expected_status = {
        "vin_rfid_exit": "SUCCESS", "rfid_vin_exit": "SUCCESS",
        "exit_then_vin_rfid": "SUCCESS", "exit_vin_only": "PARTIAL_SUCCESS",
        "exit_rfid_only": "PARTIAL_SUCCESS", "exit_no_result": "TIMEOUT",
        "no_exit_failsafe": "TIMEOUT",
    }
    seen_status = {}
    for i in range(1, N + 1):
        pat = patterns[i % len(patterns)]
        sess = _run_one(p, i, pat)
        seen_status[sess.session_id] = (pat, sess.state.value)

    # --- §16 INVARIANTLAR ---
    accepted_d2222 = p._trigger_sequence - p._dropped_trigger_count
    assert accepted_d2222 == N
    assert p._dropped_trigger_count == 0                 # silent/overflow drop yo'q

    rows = db.get_all_records()
    assert len(rows) == N, f"accepted_D2222={N} != finalized_records={len(rows)}"
    session_ids = [r["session_id"] for r in rows]
    assert len(set(session_ids)) == len(session_ids)     # duplicate session_id YO'Q
    assert None not in session_ids and "" not in session_ids

    # Ownership: hech qanday natija noto'g'ri sessiyaga tushmagan.
    assert p._metrics["session_mismatch_total"] == 0

    # Har yozuvning statusi kutilgan pattern bilan mos (natija aralashmagan).
    by_sid = {r["session_id"]: r for r in rows}
    for sid, (pat, state) in seen_status.items():
        assert by_sid[sid]["status"] == expected_status[pat], \
            f"{pat}: kutilgan {expected_status[pat]}, olindi {by_sid[sid]['status']}"


def test_queue_overflow_is_counted_not_silent(pipeline_instance, monkeypatch):
    """
    §16: trigger silent-drop YO'Q — navbat to'lsa dropped_trigger_count oshadi
    (jim yo'qolmaydi). Faol sessiya + navbat to'ldirilib, keyingi trigger RAD
    etiladi va HISOBLANADI.
    """
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "max_pending_triggers", 3)
    monkeypatch.setattr(cfg.SESSION, "max_session_duration_sec", 30.0)  # ochiq turadi
    p = pipeline_instance

    p.plc_on()                       # A -> active (ochiq turadi, exit yo'q)
    for _ in range(3):
        p.plc_on()                   # navbatni to'ldiradi (3 ta)
    dropped_before = p._dropped_trigger_count
    p.plc_on()                       # overflow -> RAD + hisoblanadi
    p.plc_on()                       # yana overflow
    assert p._dropped_trigger_count == dropped_before + 2   # jim emas — HISOBLANDI
    # Accepted + dropped = jami trigger (hech biri "yo'qolmadi").
    assert p._trigger_sequence == (1 + 3) + 2
