"""
============================================================
test_raw_to_final_evidence_linkage.py
============================================================
Locks traceability between the RAW OCR evidence (what the engines/OCR
actually saw) and the FINAL persisted decision, across both database
surfaces:

  * backend/database/db.py `vin_records.raw_vin` (legacy, always-on) vs
    `vin_records.detected_vin` (validated/structure-corrected) -- these must
    remain two DISTINCT columns; structure correction must never overwrite
    the raw evidence.
  * backend/database/ocr_v121_db.py `ocr_engine_results` (one evidence row
    per engine per session, `raw_text` verbatim from the engine) vs
    `vin_records.final_ocr_*` (the one session-owned final decision) --
    both must be joinable by session_id, and the per-engine raw evidence
    must never be mutated by whatever the final decision ends up being.

This matters for the hotfix because a pos10 verifier / dedup / fusion
change that "fixes" a character must still leave an auditable trail showing
what was actually read vs what was concluded -- required both for the
"never invent" guarantee and for future active-learning dataset review.

All tests are expected to PASS today (regression lock).
"""
from __future__ import annotations

import sqlite3

from backend.database import db, ocr_v121_db as odb


# Entirely synthetic test values; not copied from production evidence.
SYNTHETIC_FUSION_PREFIX = "NSTFB814E"
SYNTHETIC_YEAR_CODE = "T"
SYNTHETIC_SERIAL = "039378"
SYNTHETIC_VALIDATED_VIN = (
    SYNTHETIC_FUSION_PREFIX + SYNTHETIC_YEAR_CODE + "J" + SYNTHETIC_SERIAL
)
SYNTHETIC_RAW_VIN = (
    SYNTHETIC_FUSION_PREFIX + SYNTHETIC_YEAR_CODE + "1" + SYNTHETIC_SERIAL
)

SYNTHETIC_AUDIT_PREFIX = "NSTFA814A"
SYNTHETIC_AUDIT_VIN = (
    SYNTHETIC_AUDIT_PREFIX + SYNTHETIC_YEAR_CODE + "J" + "123456"
)
SYNTHETIC_AUDIT_ALTERNATE_VIN = (
    SYNTHETIC_AUDIT_PREFIX + SYNTHETIC_YEAR_CODE + "J" + "999999"
)
SYNTHETIC_ENGRAVED_PARTIAL = SYNTHETIC_AUDIT_VIN[:-1] + "?"


def test_finalize_session_record_preserves_raw_vin_distinct_from_detected_vin(tmp_db):
    """A structure-corrected VIN (e.g. OCR raw pos11='1' auto-forced to the
    constant 'J') must be stored as `detected_vin`, while the verbatim raw
    OCR string is preserved unchanged in `raw_vin` -- never overwritten,
    never merged."""
    sid = "raw-final-1"
    db.insert_pending_session(sid, 1, "2026-01-01 00:00:00")
    # pos11 (index 10) is a model-independent constant ('J'); OCR actually saw
    # the confusable '1' there (see vin_postprocess.CONFUSIONS["1"] includes "J").
    validated = SYNTHETIC_VALIDATED_VIN  # fusion/postprocess structure-corrected
    raw = SYNTHETIC_RAW_VIN              # OCR read before the 'J' enforcement
    db.finalize_session_record(
        sid, timestamp="2026-01-01 00:01:00", vin=validated, raw_vin=raw,
        model="QY", confidence=0.95, image_path=None, status="OK",
    )
    row = [r for r in db.get_all_records() if r["session_id"] == sid][0]
    assert row["detected_vin"] == validated
    assert row["raw_vin"] == raw
    assert row["raw_vin"] != row["detected_vin"], (
        "raw_vin and detected_vin must be able to differ and BOTH be preserved "
        "verbatim -- neither field may silently mirror or overwrite the other"
    )


def test_finalize_session_record_preserves_raw_vin_even_when_identical_to_detected(tmp_db):
    """When OCR reads exactly the validated VIN (no correction needed), both
    columns legitimately hold the same value -- this must not be confused
    with the raw column being dropped/blanked."""
    sid = "raw-final-2"
    db.insert_pending_session(sid, 1, "2026-01-01 00:00:00")
    vin = SYNTHETIC_VALIDATED_VIN
    db.finalize_session_record(sid, timestamp="t", vin=vin, raw_vin=vin, model="QY",
                               confidence=0.98, image_path=None, status="OK")
    row = [r for r in db.get_all_records() if r["session_id"] == sid][0]
    assert row["raw_vin"] == vin and row["detected_vin"] == vin


def _connect(tmp_path):
    path = tmp_path / "evidence_link.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE vin_records (id INTEGER PRIMARY KEY, session_id TEXT UNIQUE,
        detected_vin TEXT, raw_vin TEXT, timestamp TEXT, confidence REAL)"""
    )
    conn.commit()
    odb.migrate(conn)
    return path


def test_per_engine_evidence_rows_are_joinable_by_session_id_and_preserve_raw_text(tmp_path):
    path = _connect(tmp_path)
    sid = "evidence-join-1"
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO vin_records(session_id, detected_vin, raw_vin, timestamp, confidence) "
        "VALUES (?,?,?,?,?)", (sid, SYNTHETIC_AUDIT_VIN, SYNTHETIC_AUDIT_VIN, "t", 0.9),
    )
    conn.commit()

    engine_raws = {
        "ENGRAVED_V121": SYNTHETIC_ENGRAVED_PARTIAL,
        "PADDLE_RAW": SYNTHETIC_AUDIT_VIN,
        "PADDLE_ENHANCED": SYNTHETIC_AUDIT_VIN,
    }
    for name, raw in engine_raws.items():
        odb.store_engine_result(conn, session_id=sid, engine_name=name, raw_text=raw,
                                gated_text=raw.replace("?", ""), status="OK")
    odb.set_final_ocr(conn, session_id=sid, text=SYNTHETIC_AUDIT_VIN, engine="PADDLE_RAW",
                      confidence=0.9, decision_reason="SHADOW_ONLY", model_version="1.2.1",
                      disagreement=True)

    # Join by session_id: every engine's raw evidence is retrievable and unaltered.
    rows = {r["engine_name"]: r["raw_text"] for r in odb.get_engine_results(conn, sid)}
    assert rows == engine_raws, (
        "per-engine raw_text must be preserved verbatim and independently "
        "retrievable regardless of what the final decision concluded"
    )

    final = conn.execute(
        "SELECT final_ocr_text, final_ocr_engine, ocr_disagreement, raw_vin "
        "FROM vin_records WHERE session_id=?", (sid,)
    ).fetchone()
    assert final == (SYNTHETIC_AUDIT_VIN, "PADDLE_RAW", 1, SYNTHETIC_AUDIT_VIN)
    conn.close()


def test_engine_evidence_rows_are_not_mutated_by_a_later_final_decision_write(tmp_path):
    """Calling set_final_ocr AFTER storing per-engine evidence must not
    touch ocr_engine_results at all -- the two tables are updated
    independently, and the evidence is immutable once stored."""
    path = _connect(tmp_path)
    sid = "evidence-immutable-1"
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO vin_records(session_id, detected_vin, raw_vin, timestamp, confidence) "
        "VALUES (?,?,?,?,?)", (sid, "", "", "t", 0.0),
    )
    conn.commit()
    odb.store_engine_result(conn, session_id=sid, engine_name="ENGRAVED_V121",
                            raw_text=SYNTHETIC_AUDIT_VIN,
                            gated_text=SYNTHETIC_AUDIT_VIN, status="OK")
    before = odb.get_engine_results(conn, sid)

    odb.set_final_ocr(conn, session_id=sid, text=SYNTHETIC_AUDIT_ALTERNATE_VIN,
                      engine="PADDLE_RAW",
                      confidence=0.5, decision_reason="SHADOW_ONLY", model_version="1.2.1",
                      disagreement=False)
    after = odb.get_engine_results(conn, sid)

    assert before == after, "storing a final decision must never mutate stored per-engine evidence"
    conn.close()


def test_set_final_ocr_round_trips_decision_reason_and_disagreement_flag_for_audit(tmp_path):
    path = _connect(tmp_path)
    sid = "audit-roundtrip-1"
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO vin_records(session_id, detected_vin, raw_vin, timestamp, confidence) "
        "VALUES (?,?,?,?,?)", (sid, "", "", "t", 0.0),
    )
    conn.commit()
    odb.set_final_ocr(conn, session_id=sid, text=SYNTHETIC_AUDIT_VIN,
                      engine="ENGRAVED_V121",
                      confidence=0.91, decision_reason="ENGRAVED_ACCEPTED", model_version="1.2.1",
                      disagreement=False)
    row = conn.execute(
        "SELECT final_ocr_text, final_ocr_engine, final_ocr_confidence, "
        "final_ocr_decision_reason, ocr_model_version, ocr_disagreement, ocr_completed_at "
        "FROM vin_records WHERE session_id=?", (sid,)
    ).fetchone()
    conn.close()
    assert row[:6] == (
        SYNTHETIC_AUDIT_VIN, "ENGRAVED_V121", 0.91,
        "ENGRAVED_ACCEPTED", "1.2.1", 0)
    assert row[6], "ocr_completed_at must be stamped for audit"
