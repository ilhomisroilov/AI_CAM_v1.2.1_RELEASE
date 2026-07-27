"""
============================================================
test_session_idempotency.py — P0-2 / P1 fix: bitta sessiya = bitta yozuv
============================================================
Audit topilmasi: `_finalize_session` faqat bitta bool flag (`_session_finalized`)
bilan himoyalangan edi; concurrent SUCCESS/TIMEOUT yoki ikki marta finalize
chaqiruvi holatida nazariy jihatdan ikki DB yozuvi (masalan TIMEOUT + OK)
paydo bo'lishi mumkin edi.

Bu fayl tekshiradi:
  7. Concurrent SUCCESS va TIMEOUT finalize — bitta record.
  8. Bir sessiya uchun ikki finalize urinishi — bitta record.
 13. Restart-recovery siyosati: tugallanmagan (PENDING) sessiya keyingi
     ishga tushirishda ANIQ FAILED qilib yopiladi (hech qachon "yozuv yo'q"
     holati bo'lmaydi).
"""
from __future__ import annotations

import threading

from backend.database import db

from conftest import make_ocr_result


def test_concurrent_success_and_timeout_finalize_yields_one_record(pipeline_instance):
    """7) SUCCESS va TIMEOUT bir vaqtda (concurrent) chaqirilsa — bitta record qoladi."""
    p = pipeline_instance
    p.plc_on()
    sid = p._active_session_id
    with p._session_lock:
        p._sessions[sid].vin = "NSTFA814ATJ123456"     # SUCCESS shartini qondiramiz

    barrier = threading.Barrier(2)

    def call_success():
        barrier.wait(timeout=2.0)
        p._finalize_session(sid, reason="SUCCESS")

    def call_timeout():
        barrier.wait(timeout=2.0)
        p._finalize_session(sid, reason="TIMEOUT")

    t1 = threading.Thread(target=call_success)
    t2 = threading.Thread(target=call_timeout)
    t1.start(); t2.start()
    t1.join(timeout=5.0); t2.join(timeout=5.0)

    records = [r for r in db.get_all_records() if r["session_id"] == sid]
    assert len(records) == 1, "concurrent finalize chaqiruvlari BITTA record berishi kerak"
    # G'olib qaysi bo'lishidan qat'i nazar, holat terminal bo'lishi kerak.
    assert p._sessions[sid].state.value in ("SUCCESS", "TIMEOUT")


def test_double_finalize_call_yields_one_record(pipeline_instance):
    """8) Bitta sessiya uchun ikki finalize urinishi — bitta record (ikkinchisi darhol qaytadi)."""
    p = pipeline_instance
    p.plc_on()
    sid = p._active_session_id

    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ123456"))   # birinchi finalize (SUCCESS)
    assert p._sessions[sid].state.value == "SUCCESS"

    p._finalize_session(sid, reason="MANUAL_RETRY")     # ikkinchi (qo'lda) urinish

    records = [r for r in db.get_all_records() if r["session_id"] == sid]
    assert len(records) == 1


def test_restart_recovery_marks_pending_sessions_failed(tmp_db):
    """
    13) Restart-recovery siyosati: ilova qulab, sessiya PENDING holida qolsa
    (finalize qilinmagan), keyingi `init_db()` chaqiruvi (restart) uni ANIQ
    FAILED qilib yopadi — "hatto TIMEOUT ham yozilmaydi" muammosi FIX qilindi:
    endi HAR DOIM biror ANIQ yakuniy holat (bu holda FAILED) yoziladi.
    """
    # 1-jarayon: sessiya boshlandi (PENDING yozuv), keyin ILOVA QULADI (finalize yo'q).
    db.insert_pending_session("crash-session-1", 7, "2026-01-01 00:00:00")
    assert db.count_records() == 1
    row = db.get_all_records()[0]
    assert row["status"] == "PENDING"
    assert row["session_finalized_at"] is None

    # 2-jarayon (restart): init_db() qayta chaqiriladi -> recovery AVTOMATIK ishlaydi.
    db.init_db()

    row2 = db.get_all_records()[0]
    assert row2["status"] == "FAILED"
    assert row2["failure_reason"] == "RESTART_RECOVERY"
    assert row2["session_finalized_at"] is not None
    # Hech qachon ikkinchi (duplicate) qator yaratilmagan.
    assert db.count_records() == 1


def test_pipeline_start_session_writes_pending_row_recovered_on_restart(pipeline_instance):
    """
    Pipeline._start_session PENDING placeholder yozadi; agar jarayon finalize
    qilishga ULGURMASDAN 'qulasa' (bu testda: finalize chaqirmaymiz), keyingi
    init_db() (restart) uni FAILED qilib tiklaydi.
    """
    p = pipeline_instance
    p.plc_on()
    sid = p._active_session_id

    row = [r for r in db.get_all_records() if r["session_id"] == sid][0]
    assert row["status"] == "PENDING"

    # Restart simulyatsiyasi — Session finalize qilinmagan holda process qayta ishga tushadi.
    db.init_db()

    row2 = [r for r in db.get_all_records() if r["session_id"] == sid][0]
    assert row2["status"] == "FAILED"
    assert row2["failure_reason"] == "RESTART_RECOVERY"
