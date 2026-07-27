"""
Rapid MVP — Tier 8: Database chaos.

DB idempotency/UNIQUE avvalgi remediation'da (test_database_migration.py,
test_session_idempotency.py) qoplangan: duplicate session_id UNIQUE bilan
bloklanadi, ikki finalize -> bitta record.

Bu fayl QOLGAN kritik holatni qoplaydi (spec Tier 8):
  * DB write xatosi JIMGINA "SUCCESS" sifatida ko'rsatilmasin -> sessiya
    DATABASE_FAILED, database_write_failure_total metrikasi oshadi;
  * image save failure (rel_path=None) -> record BARIBIR yoziladi;
  * bir session uchun ko'pi bilan bitta record (idempotent finalize).

Real production DB'ga TEGILMAYDI (tmp_db). Deterministik.
"""
from __future__ import annotations

import pytest

from backend import config as cfg
from backend.database import db
from backend.pipeline import SessionState
from conftest import make_ocr_result, wait_until


def _active(p):
    with p._session_lock:
        return p._sessions.get(p._active_session_id) if p._active_session_id else None


def test_db_write_failure_not_silently_success(pipeline_instance, monkeypatch):
    """
    DB yozuvi xato bersa — sessiya SUCCESS bo'lib QOLMASLIGI kerak:
    DATABASE_FAILED holati + database_write_failure_total metrikasi.
    """
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id

    def _boom(*a, **k):
        raise RuntimeError("disk to'la / DB locked (simulyatsiya)")
    monkeypatch.setattr(db, "finalize_session_record", _boom)

    before = p._metrics.get("database_write_failure_total", 0)
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ300001"))
    assert wait_until(lambda: sess.state in
                      (SessionState.FAILED, SessionState.SUCCESS, SessionState.TIMEOUT))
    # JIM SUCCESS EMAS — DATABASE_FAILED sifatida belgilanadi.
    assert sess.state == SessionState.FAILED
    assert sess.failure_reason == "DATABASE_FAILED"
    assert p._metrics["database_write_failure_total"] == before + 1


def test_image_save_failure_still_writes_record(pipeline_instance, monkeypatch):
    """rel_path=None (crop yozilmadi) bo'lsa ham DB record BARIBIR yoziladi."""
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id
    # rel_path=None bilan VIN natijasi (crop saqlanmagan holat).
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ300002"))
    assert wait_until(lambda: sess.state == SessionState.SUCCESS)
    rows = db.get_all_records()
    assert len(rows) == 1
    assert rows[0]["detected_vin"] == "NSTFA814ATJ300002"
    assert rows[0]["image_path"] in (None, "")


def test_double_finalize_one_record(pipeline_instance, monkeypatch):
    """Bir session uchun ikki finalize urinishi -> ko'pi bilan BITTA record."""
    p = pipeline_instance
    p.plc_on()
    sess = _active(p)
    sid = sess.session_id
    p._finalize_session(sid, reason="A")
    p._finalize_session(sid, reason="B")     # ikkinchi urinish -> idempotent no-op
    assert db.count_records() == 1
