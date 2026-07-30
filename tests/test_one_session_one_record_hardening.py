"""
============================================================
test_one_session_one_record_hardening.py
============================================================
Additional hardening for the hard invariant "one session = one vin_records
row" (session_id is UNIQUE; see backend/database/db.py), beyond what
tests/test_session_idempotency.py and tests/test_db_chaos.py already cover.
These tests exercise `backend.database.db` DIRECTLY (bypassing the
pipeline's application-level `_session_lock`) to prove the invariant is
enforced by the DATABASE ITSELF (UNIQUE constraint + UPDATE-not-INSERT
finalize), not merely by the pipeline's in-process lock -- this matters
because the planned hotfix adds new concurrent write paths (per-engine
evidence rows, pos10 verifier) that must not be able to create a second
`vin_records` row for the same session under ANY interleaving.

All tests are expected to PASS today (regression lock / defense-in-depth
verification) and must continue to pass after the hotfix lands.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from backend.database import db


# Entirely synthetic test values; not copied from production evidence.
SYNTHETIC_PREFIX = "NSTFA814A"
SYNTHETIC_YEAR_CODE = "T"
SYNTHETIC_SUFFIX = "J"


def _synthetic_vin(serial: str) -> str:
    return SYNTHETIC_PREFIX + SYNTHETIC_YEAR_CODE + SYNTHETIC_SUFFIX + serial


SYNTHETIC_VIN_ONE = _synthetic_vin("111111")
SYNTHETIC_VIN_TWO = _synthetic_vin("222222")
SYNTHETIC_VIN_ROLLBACK = _synthetic_vin("999999")
SYNTHETIC_VIN_ZERO = _synthetic_vin("000000")


# ---------------------------------------------------------------------------
# DB-level concurrency (no pipeline lock involved at all)
# ---------------------------------------------------------------------------
def test_finalize_session_record_race_from_n_threads_yields_exactly_one_row(tmp_db):
    """N threads race to finalize the SAME session_id with DIFFERENT VIN
    payloads, calling `db.finalize_session_record` directly (the pipeline's
    `_session_lock` is NOT involved). The UNIQUE(session_id) index + the
    UPDATE-first design must still guarantee exactly one row survives."""
    sid = "race-session-1"
    db.insert_pending_session(sid, 1, "2026-01-01 00:00:00")

    n = 12
    barrier = threading.Barrier(n)
    errors: list = []

    def _finalize(i: int):
        try:
            barrier.wait(timeout=5.0)
            db.finalize_session_record(
                sid, timestamp=f"2026-01-01 00:0{i % 9}:00",
                vin=_synthetic_vin(f"00000{i % 9}"),
                confidence=0.9, image_path=None, status="OK",
            )
        except Exception as exc:   # a losing writer may legitimately raise; must never duplicate
            errors.append(exc)

    threads = [threading.Thread(target=_finalize, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    rows = [r for r in db.get_all_records() if r["session_id"] == sid]
    assert len(rows) == 1, f"expected exactly one row after {n}-way race, got {len(rows)}"


def test_insert_pending_then_two_direct_finalize_calls_one_row(tmp_db):
    sid = "direct-double-finalize"
    db.insert_pending_session(sid, 5, "2026-01-01 00:00:00")
    db.finalize_session_record(sid, timestamp="2026-01-01 00:01:00", vin=SYNTHETIC_VIN_ONE,
                               confidence=0.9, image_path=None, status="OK")
    db.finalize_session_record(sid, timestamp="2026-01-01 00:02:00", vin=SYNTHETIC_VIN_TWO,
                               confidence=0.5, image_path=None, status="OK")
    rows = [r for r in db.get_all_records() if r["session_id"] == sid]
    assert len(rows) == 1
    # UPDATE semantics: the SECOND call's payload wins (last-write); the row
    # count invariant (never 2) is what this test protects, not which value wins.
    assert rows[0]["detected_vin"] == SYNTHETIC_VIN_TWO


def test_unique_constraint_blocks_a_second_insert_pending_for_same_session(tmp_db):
    sid = "dup-pending-guard"
    db.insert_pending_session(sid, 1, "2026-01-01 00:00:00")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_pending_session(sid, 2, "2026-01-01 00:01:00")
    assert len([r for r in db.get_all_records() if r["session_id"] == sid]) == 1


# ---------------------------------------------------------------------------
# DB write/rollback: a failed finalize must not corrupt/partially-write the row
# ---------------------------------------------------------------------------
class _Unbindable:
    """A Python object sqlite3 cannot bind as a parameter -- forces a genuine
    driver-level error mid-call, after `insert_pending_session` already
    committed the PENDING row, so we can prove the finalize UPDATE either
    fully applies or fully rolls back (never partially)."""


def test_failed_finalize_leaves_pending_row_completely_unchanged(tmp_db):
    sid = "rollback-guard-1"
    db.insert_pending_session(sid, 3, "2026-01-01 00:00:00")
    before = dict([r for r in db.get_all_records() if r["session_id"] == sid][0])

    with pytest.raises(sqlite3.ProgrammingError):
        db.finalize_session_record(
            sid, timestamp="2026-01-01 00:05:00", vin=SYNTHETIC_VIN_ROLLBACK,
            confidence=_Unbindable(),   # unsupported type -> sqlite3 raises on bind, before commit
            image_path=None, status="OK",
        )

    after = dict([r for r in db.get_all_records() if r["session_id"] == sid][0])
    assert before == after, (
        "a finalize call that raises before commit must leave the existing "
        "PENDING row byte-for-byte unchanged (true rollback, no partial write)"
    )
    assert after["status"] == "PENDING"


def test_database_write_failure_is_caller_visible_not_swallowed(tmp_db, monkeypatch):
    """Complements tests/test_db_chaos.py::test_db_write_failure_not_silently_success
    (pipeline-level) with a pure db.py-level check: a raised exception from
    finalize_session_record must propagate to the caller (so the pipeline's
    DATABASE_FAILED handling -- see backend/pipeline.py `_finalize_session`,
    lines ~1596-1607 -- has something to catch); it must never be silently
    swallowed inside db.py itself."""
    sid = "propagate-guard"
    db.insert_pending_session(sid, 1, "2026-01-01 00:00:00")

    def _boom(*a, **k):
        raise sqlite3.OperationalError("simulated: database is locked")

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(
        sqlite3.OperationalError("simulated: database is locked")))
    with pytest.raises(sqlite3.OperationalError):
        db.finalize_session_record(sid, timestamp="t", vin=SYNTHETIC_VIN_ZERO,
                                   confidence=0.9, image_path=None, status="OK")
