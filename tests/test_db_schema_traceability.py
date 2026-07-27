"""
Rapid MVP — DB schema: D2222/D2223 traceability + latency ustunlari.

Yangi ustunlar (d2222_timestamp, d2223_timestamp, ocr_latency_ms,
rfid_latency_ms, total_session_ms) ADDITIVE observability — integrity emas,
shuning uchun eski session-DB'ga XAVFSIZ auto-ALTER bilan qo'shiladi (session_id
kabi RuntimeError EMAS). Production DB'ga TEGILMAYDI (tmp_path).
"""
from __future__ import annotations

import sqlite3

import pytest

from backend import config as cfg
from backend.database import db
from backend.pipeline import SessionState
from conftest import make_ocr_result, wait_until


def _cols(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(vin_records)").fetchall()}
    finally:
        conn.close()


_NEW = {"d2222_timestamp", "d2223_timestamp", "ocr_latency_ms",
        "rfid_latency_ms", "total_session_ms"}


def test_fresh_db_has_traceability_columns(tmp_db):
    assert _NEW <= _cols(tmp_db)


def test_auto_migrate_adds_columns_to_session_db(tmp_path):
    """
    Eski session-schema DB (session_id BOR, lekin yangi ustunlar YO'Q) -> init_db()
    ularni XAVFSIZ auto-ALTER bilan qo'shadi (RuntimeError EMAS — additive).
    """
    p = tmp_path / "legacy_session.db"
    conn = sqlite3.connect(str(p))
    # session_id + hamrohlari BOR (RuntimeError bo'lmaydi), lekin yangi ustunlar YO'Q.
    conn.execute("""CREATE TABLE vin_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        detected_vin TEXT NOT NULL, confidence REAL NOT NULL, image_path TEXT,
        raw_vin TEXT, model TEXT, status TEXT, rfid_epc TEXT, rfid_raw TEXT,
        session_id TEXT NOT NULL, trigger_sequence INTEGER, session_started_at TEXT,
        session_finalized_at TEXT, failure_reason TEXT, UNIQUE(session_id))""")
    conn.commit()
    conn.close()
    assert not (_NEW <= _cols(p))          # avval YO'Q

    db.set_db_path(p)
    try:
        db.init_db()                        # auto-ALTER (RuntimeError EMAS)
        assert _NEW <= _cols(p)             # endi BOR
    finally:
        db.set_db_path(None)


def test_finalize_populates_traceability(pipeline_instance, monkeypatch):
    monkeypatch.setattr(cfg.SESSION, "exit_signal_enabled", True)
    monkeypatch.setattr(cfg.SESSION, "post_exit_grace_ms", 2000)
    monkeypatch.setattr(cfg.RFID, "enabled", True)
    p = pipeline_instance
    p.rfid_service = None

    p.plc_on()
    with p._session_lock:
        sess = p._sessions.get(p._active_session_id)
    sid = sess.session_id
    p._on_ocr_result(make_ocr_result(sid, vin="NSTFA814ATJ400001"))
    from datetime import datetime
    from backend.rfid.rfid_service import RFIDResult
    now = datetime.now()
    p._on_rfid_result(RFIDResult(session_id=sid, epc="00001234", number="1234",
                                 antenna=1, rssi=-40.0, first_seen_at=now, received_at=now))
    p.on_exit_signal()      # ikkalasi tayyor -> SUCCESS
    assert wait_until(lambda: sess.state == SessionState.SUCCESS)

    row = db.get_all_records()[0]
    # D2222/D2223 timestamp + total latency yozilgan.
    assert row["d2222_timestamp"] is not None
    assert row["d2223_timestamp"] is not None
    assert row["ocr_latency_ms"] is not None and row["ocr_latency_ms"] >= 0
    assert row["rfid_latency_ms"] is not None and row["rfid_latency_ms"] >= 0
    assert row["total_session_ms"] is not None and row["total_session_ms"] >= 0
