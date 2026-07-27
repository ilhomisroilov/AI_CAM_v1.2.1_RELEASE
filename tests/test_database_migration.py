"""
============================================================
test_database_migration.py — P1 fix: session_id UNIQUE + qat'iy migratsiya
============================================================
Audit topilmasi: `vin_records` jadvalida `session_id` ustuni va UNIQUE
constraint YO'Q edi (bir xil VIN/EPC cheksiz takror yozilishi mumkin edi).
Fix talabi: DB avtomatik (jim) ALTER QILINMAYDI — `init_db()` eski sxemani
ko'rib ANIQ xato ko'taradi; `tools/migrate_session_id.py` operator tomonidan
QO'LDA, ANIQ ishga tushiriladi (backup + idempotent + deterministic legacy id).

MUHIM: HAMMA testlar FAQAT `tmp_path` asosidagi vaqtinchalik fayllarda
ishlaydi — `data/ai_cam.db` ga HECH QACHON tegilmaydi, migration skripti
ham shu tmp fayllar ustida ishga tushiriladi.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.database import db


def _create_legacy_schema(path, seed_rows=1) -> None:
    """session_id dan OLDINGI (eski) sxemani qo'lda quradi — migratsiya kerakligini simulyatsiya qiladi."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE vin_records (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT    NOT NULL,
            detected_vin  TEXT    NOT NULL,
            raw_vin       TEXT,
            model         TEXT,
            rfid_epc      TEXT,
            rfid_raw      TEXT,
            confidence    REAL    NOT NULL,
            status        TEXT,
            image_path    TEXT
        )
        """
    )
    for i in range(seed_rows):
        conn.execute(
            "INSERT INTO vin_records (timestamp, detected_vin, confidence, status) VALUES (?,?,?,?)",
            (f"2026-01-01 00:0{i}:00", f"NSTFA814ATJ12345{i}", 0.9, "OK"),
        )
    conn.commit()
    conn.close()


def test_unique_session_id_blocks_duplicate_insert(tmp_db):
    """12) UNIQUE fizik dublikatni bloklaydi; manual API idempotent ishlaydi."""
    db.insert_pending_session("dup-session-abc", 1, "2026-01-01 00:00:00")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_pending_session("dup-session-abc", 2, "2026-01-01 00:01:00")

    # insert_record (legacy/manual yo'l) retry paytida yangi qator yaratmasligi kerak.
    first_id = db.insert_record(
        "2026-01-01 00:02:00", "NSTFA814ATJ654321", 0.9, None,
        session_id="dup-session-xyz",
    )
    second_id = db.insert_record(
        "2026-01-01 00:03:00", "NSTFA814ATJ000000", 0.5, None,
        session_id="dup-session-xyz",
    )

    assert second_id == first_id
    matching = [
        row for row in db.get_all_records()
        if row["session_id"] == "dup-session-xyz"
    ]
    assert len(matching) == 1


def test_insert_record_requires_non_empty_session_id(tmp_db):
    with pytest.raises(ValueError):
        db.insert_record("2026-01-01 00:00:00", "NSTFA814ATJ123456", 0.9, None, session_id="")


def test_init_db_raises_on_legacy_schema_missing_session_id(tmp_path):
    """
    P1 fix: session_id yo'q eski baza uchun `init_db()` jim ALTER qilmaydi —
    ANIQ RuntimeError ko'taradi (operatorni migratsiya skriptiga yo'naltiradi).
    """
    legacy_path = tmp_path / "legacy.db"
    _create_legacy_schema(legacy_path, seed_rows=2)

    db.set_db_path(legacy_path)
    try:
        with pytest.raises(RuntimeError, match="session-integrity"):
            db.init_db()
    finally:
        db.set_db_path(None)

    # Muhim: xato ko'tarilgandan keyin ham fayl O'ZGARTIRILMAGAN (jim ALTER yo'q).
    conn = sqlite3.connect(str(legacy_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vin_records)").fetchall()}
    conn.close()
    assert "session_id" not in cols, "init_db() sxemani JIM o'zgartirmasligi kerak edi"


def test_migration_script_dry_run_does_not_write(tmp_path):
    from tools.migrate_session_id import migrate

    legacy_path = tmp_path / "legacy_dry.db"
    _create_legacy_schema(legacy_path, seed_rows=1)

    rc = migrate(legacy_path, dry_run=True)
    assert rc == 0

    conn = sqlite3.connect(str(legacy_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vin_records)").fetchall()}
    conn.close()
    assert "session_id" not in cols, "--dry-run hech narsa yozmasligi kerak"


def test_migration_script_fixes_legacy_db_then_init_db_succeeds(tmp_path):
    """
    Operator oqimi: eski DB -> migrate_session_id.py (--yes) -> init_db()
    endi xatosiz ishlaydi -> legacy qatorlarga deterministik 'legacy-{id}'
    session_id berilgan.
    """
    from tools.migrate_session_id import migrate

    legacy_path = tmp_path / "legacy_migrate.db"
    _create_legacy_schema(legacy_path, seed_rows=3)

    rc = migrate(legacy_path, dry_run=False)
    assert rc == 0

    db.set_db_path(legacy_path)
    try:
        db.init_db()          # endi XATO chiqmasligi kerak
        rows = db.get_all_records()
        assert len(rows) == 3
        for r in rows:
            assert r["session_id"] == f"legacy-{r['id']}"
        # UNIQUE constraint migratsiyadan keyin ham amal qiladi.
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_pending_session(rows[0]["session_id"], 1, "2026-01-01 00:00:00")
    finally:
        db.set_db_path(None)


def test_migration_script_is_idempotent(tmp_path):
    """Migratsiya skripti ikki marta ishga tushirilsa — ikkinchi safar hech narsa o'zgarmaydi."""
    from tools.migrate_session_id import migrate

    legacy_path = tmp_path / "legacy_idempotent.db"
    _create_legacy_schema(legacy_path, seed_rows=2)

    assert migrate(legacy_path, dry_run=False) == 0
    conn = sqlite3.connect(str(legacy_path))
    before = conn.execute("SELECT id, session_id FROM vin_records ORDER BY id").fetchall()
    conn.close()

    assert migrate(legacy_path, dry_run=False) == 0    # ikkinchi marta — o'zgarish YO'Q
    conn = sqlite3.connect(str(legacy_path))
    after = conn.execute("SELECT id, session_id FROM vin_records ORDER BY id").fetchall()
    conn.close()
    assert before == after


def test_fresh_db_gets_new_schema_directly_no_migration_needed(tmp_path):
    """Jadval umuman mavjud bo'lmagan (yangi) DB uchun init_db() to'g'ridan-to'g'ri to'liq sxema yaratadi."""
    fresh_path = tmp_path / "fresh.db"
    db.set_db_path(fresh_path)
    try:
        db.init_db()        # xato chiqmasligi kerak — jadval yo'q edi, to'liq yangi sxema yaratiladi
        rec_id = db.insert_record("2026-01-01 00:00:00", "NSTFA814ATJ123456", 0.9, None,
                                  session_id="fresh-session-1")
        assert rec_id == 1
    finally:
        db.set_db_path(None)
