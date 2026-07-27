from __future__ import annotations

import hashlib
import sqlite3

from tools.import_legacy_database import import_database


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE vin_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        detected_vin TEXT NOT NULL,
        raw_vin TEXT,
        model TEXT,
        rfid_epc TEXT,
        rfid_raw TEXT,
        confidence REAL NOT NULL,
        status TEXT,
        image_path TEXT)"""
    )
    conn.execute(
        "INSERT INTO vin_records(timestamp,detected_vin,confidence,status) "
        "VALUES ('2026-01-01','NSTFC814ETJ042250',0.9,'OK')"
    )
    conn.commit()
    conn.close()


def test_legacy_import_is_explicit_and_source_is_unchanged(tmp_path):
    source = tmp_path / "old.db"
    destination = tmp_path / "runtime" / "ai_cam.db"
    _legacy(source)
    before = _hash(source)

    dry = import_database(source, destination, apply=False)
    assert dry["vin_records"] == 1
    assert not destination.exists()

    report = import_database(source, destination, apply=True)
    assert report["imported"]
    assert _hash(source) == before
    with sqlite3.connect(destination) as conn:
        session_id = conn.execute(
            "SELECT session_id FROM vin_records"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert session_id == "legacy-1"
    assert {"ocr_engine_results", "ocr_collection_items"} <= tables
