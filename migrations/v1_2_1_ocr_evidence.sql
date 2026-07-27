-- AI_CAM v1.2.1 additive OCR evidence schema.
-- Authoritative idempotent runner: backend/database/ocr_v121_db.py
CREATE TABLE IF NOT EXISTS ocr_engine_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    engine_name TEXT NOT NULL,
    engine_role TEXT,
    raw_text TEXT,
    gated_text TEXT,
    raw_payload_json TEXT,
    per_character_json TEXT,
    confidence_json TEXT,
    boxes_json TEXT,
    preprocessing_profile TEXT,
    model_name TEXT,
    model_version TEXT,
    model_checksum TEXT,
    latency_ms REAL,
    status TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ocr_collection_items (
    collection_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    expected_vin TEXT,
    expected_character TEXT,
    character_position INTEGER,
    model_character TEXT,
    model_confidence REAL,
    paddle_raw_character TEXT,
    paddle_enhanced_character TEXT,
    collection_reason TEXT,
    source_frame_path TEXT,
    normalized_line_path TEXT,
    character_crop_path TEXT,
    crop_box_json TEXT,
    source_hash TEXT,
    crop_hash TEXT,
    label_status TEXT,
    review_status TEXT,
    model_version TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_hash, character_position, crop_hash, session_id)
);

CREATE INDEX IF NOT EXISTS idx_oer_session ON ocr_engine_results(session_id);
CREATE INDEX IF NOT EXISTS idx_oer_engine ON ocr_engine_results(engine_name);
CREATE INDEX IF NOT EXISTS idx_oci_session ON ocr_collection_items(session_id);
CREATE INDEX IF NOT EXISTS idx_oci_review ON ocr_collection_items(review_status);
