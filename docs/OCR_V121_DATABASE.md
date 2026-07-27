# OCR v1.2.1 Database

Implemented additively in `backend/database/ocr_v121_db.py` (idempotent). Full details +
guarantees: `reports/V121_DATABASE_MIGRATION_REPORT.md`.

## Tables / columns
- `ocr_engine_results` — evidence, one row per engine per session (ENGRAVED_V121, PADDLE_RAW,
  PADDLE_ENHANCED): raw_text, gated_text, raw_payload_json, per_character_json, confidence_json,
  boxes_json, preprocessing_profile, model_name/version/checksum, latency_ms, status, error_code.
- `ocr_collection_items` — active-learning items (dedup UNIQUE(source_hash, position, crop_hash,
  session_id)); label_status ∈ {TRUSTED_LABEL, UNLABELED, LABEL_CONFLICT}; review_status ∈
  {PENDING, ACCEPTED, REJECTED, UNCERTAIN, IMPORTED_TO_DATASET}.
- `vin_records.final_ocr_*` — the ONE session-owned final decision (text, engine, confidence,
  decision_reason, ocr_model_version, ocr_disagreement, ocr_completed_at).

## Rules
- Exactly **one** final production record per session (vin_records); three engine rows are
  evidence, not production records.
- Additive + idempotent + old-record-compatible; `migration_state()` detects already-applied.
- Apply with `scripts/migrate_v121.ps1`. Rollback: drop the two tables + additive columns, or
  set `ocr_release.release_mode: DISABLED`.

## API
`migrate(conn)`, `migration_state(conn)`, `store_engine_result(...)`, `set_final_ocr(...)`,
`store_collection_item(item)` (returns False on dedup), `get_engine_results(conn, session_id)`.
