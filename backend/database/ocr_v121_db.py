"""
ocr_v121_db.py — additive, backward-compatible OCR-evidence schema for v1.2.1.

Isolated from db.py's strict session-integrity code. Idempotent (safely detectable as
already applied). Adds:
  * ocr_engine_results   — one evidence row per engine per session (3 engines -> 3 rows)
  * ocr_collection_items — active-learning collector items
  * vin_records.final_ocr_* columns — the ONE session-owned final OCR decision
There is still exactly one production record per session (vin_records); the three engine
rows are evidence, not production records.
"""
from __future__ import annotations
import json, sqlite3, time
from typing import Any, Optional

ENGINE_TABLE = "ocr_engine_results"
COLLECTION_TABLE = "ocr_collection_items"
FINAL_COLS = {
    "final_ocr_text": "TEXT", "final_ocr_engine": "TEXT", "final_ocr_confidence": "REAL",
    "final_ocr_decision_reason": "TEXT", "ocr_model_version": "TEXT",
    "ocr_disagreement": "INTEGER", "ocr_completed_at": "TEXT",
}


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def migrate(conn: sqlite3.Connection) -> dict:
    """Apply the additive migration on an open connection. Returns a state dict."""
    applied = []
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {ENGINE_TABLE} (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id           TEXT NOT NULL,
            engine_name          TEXT NOT NULL,
            engine_role          TEXT,
            raw_text             TEXT,
            gated_text           TEXT,
            raw_payload_json     TEXT,
            per_character_json   TEXT,
            confidence_json      TEXT,
            boxes_json           TEXT,
            preprocessing_profile TEXT,
            model_name           TEXT,
            model_version        TEXT,
            model_checksum       TEXT,
            latency_ms           REAL,
            status               TEXT,
            error_code           TEXT,
            created_at           TEXT NOT NULL
        )""")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {COLLECTION_TABLE} (
            collection_id          TEXT PRIMARY KEY,
            session_id             TEXT NOT NULL,
            expected_vin           TEXT,
            expected_character     TEXT,
            character_position     INTEGER,
            model_character        TEXT,
            model_confidence       REAL,
            paddle_raw_character   TEXT,
            paddle_enhanced_character TEXT,
            collection_reason      TEXT,
            source_frame_path      TEXT,
            normalized_line_path   TEXT,
            character_crop_path    TEXT,
            crop_box_json          TEXT,
            source_hash            TEXT,
            crop_hash              TEXT,
            label_status           TEXT,
            review_status          TEXT,
            model_version          TEXT,
            created_at             TEXT NOT NULL,
            UNIQUE(source_hash, character_position, crop_hash, session_id)
        )""")
    for idx, cols in [
        ("idx_oer_session", f"{ENGINE_TABLE}(session_id)"),
        ("idx_oer_engine", f"{ENGINE_TABLE}(engine_name)"),
        ("idx_oer_created", f"{ENGINE_TABLE}(created_at)"),
        ("idx_oer_version", f"{ENGINE_TABLE}(model_version)"),
        ("idx_oer_status", f"{ENGINE_TABLE}(status)"),
        ("idx_oci_session", f"{COLLECTION_TABLE}(session_id)"),
        ("idx_oci_review", f"{COLLECTION_TABLE}(review_status)"),
        ("idx_oci_char", f"{COLLECTION_TABLE}(expected_character)"),
    ]:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {cols}")
    if _table_exists(conn, "vin_records"):
        for col, decl in FINAL_COLS.items():
            if not _has_column(conn, "vin_records", col):
                conn.execute(f"ALTER TABLE vin_records ADD COLUMN {col} {decl}")
                applied.append(col)
    conn.commit()
    return migration_state(conn) | {"columns_added_this_call": applied}


def migration_state(conn: sqlite3.Connection) -> dict:
    return {
        "engine_table": _table_exists(conn, ENGINE_TABLE),
        "collection_table": _table_exists(conn, COLLECTION_TABLE),
        "final_ocr_columns": (all(_has_column(conn, "vin_records", c) for c in FINAL_COLS)
                              if _table_exists(conn, "vin_records") else False),
        "applied": (_table_exists(conn, ENGINE_TABLE) and _table_exists(conn, COLLECTION_TABLE)),
    }


def _j(v: Any) -> Optional[str]:
    if v is None: return None
    try: return json.dumps(v, default=str)
    except Exception: return json.dumps(str(v))


def store_engine_result(conn: sqlite3.Connection, *, session_id: str, engine_name: str,
                        engine_role: str = "", raw_text: str = "", gated_text: str = "",
                        raw_payload=None, per_character=None, confidence=None, boxes=None,
                        preprocessing_profile: str = "", model_name: str = "",
                        model_version: str = "", model_checksum: str = "",
                        latency_ms: float = 0.0, status: str = "", error_code: str = "") -> int:
    cur = conn.execute(
        f"""INSERT INTO {ENGINE_TABLE}
        (session_id,engine_name,engine_role,raw_text,gated_text,raw_payload_json,
         per_character_json,confidence_json,boxes_json,preprocessing_profile,model_name,
         model_version,model_checksum,latency_ms,status,error_code,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (session_id, engine_name, engine_role, raw_text, gated_text, _j(raw_payload),
         _j(per_character), _j(confidence), _j(boxes), preprocessing_profile, model_name,
         model_version, model_checksum, latency_ms, status, error_code,
         time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    return cur.lastrowid


def set_final_ocr(conn: sqlite3.Connection, *, session_id: str, text: str, engine: str,
                  confidence: float, decision_reason: str, model_version: str,
                  disagreement: bool) -> None:
    """Set the ONE final session-owned OCR decision (additive columns on vin_records)."""
    conn.execute(
        """UPDATE vin_records SET final_ocr_text=?, final_ocr_engine=?, final_ocr_confidence=?,
           final_ocr_decision_reason=?, ocr_model_version=?, ocr_disagreement=?, ocr_completed_at=?
           WHERE session_id=?""",
        (text, engine, confidence, decision_reason, model_version, 1 if disagreement else 0,
         time.strftime("%Y-%m-%dT%H:%M:%S"), session_id))
    conn.commit()


def store_collection_item(conn: sqlite3.Connection, item: dict) -> bool:
    """Insert a collection item; returns False if it is a duplicate (dedup key)."""
    keys = ["collection_id","session_id","expected_vin","expected_character","character_position",
            "model_character","model_confidence","paddle_raw_character","paddle_enhanced_character",
            "collection_reason","source_frame_path","normalized_line_path","character_crop_path",
            "crop_box_json","source_hash","crop_hash","label_status","review_status","model_version",
            "created_at"]
    vals = [item.get(k) for k in keys]
    try:
        conn.execute(f"INSERT INTO {COLLECTION_TABLE} ({','.join(keys)}) VALUES ({','.join('?'*len(keys))})", vals)
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_engine_results(conn: sqlite3.Connection, session_id: str) -> list:
    cur = conn.execute(f"SELECT * FROM {ENGINE_TABLE} WHERE session_id=? ORDER BY id", (session_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
