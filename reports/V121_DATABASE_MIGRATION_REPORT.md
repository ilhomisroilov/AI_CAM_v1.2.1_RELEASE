# V121 Database Migration Report

Additive, backward-compatible, idempotent. Implemented in `backend/database/ocr_v121_db.py`
(isolated from db.py's strict session-integrity code); applied at shadow-hook build time and
via `scripts/migrate_v121.ps1`.

## Schema added
- **`ocr_engine_results`** — one evidence row per engine per session: id, session_id,
  engine_name (ENGRAVED_V121 / PADDLE_RAW / PADDLE_ENHANCED), engine_role, raw_text,
  gated_text, raw_payload_json, per_character_json, confidence_json, boxes_json,
  preprocessing_profile, model_name, model_version, model_checksum, latency_ms, status,
  error_code, created_at.
- **`ocr_collection_items`** — active-learning items (see collection report); UNIQUE
  (source_hash, character_position, crop_hash, session_id) for dedup.
- **`vin_records.*`** additive columns: final_ocr_text, final_ocr_engine, final_ocr_confidence,
  final_ocr_decision_reason, ocr_model_version, ocr_disagreement, ocr_completed_at.
- **Indexes:** session_id, engine_name, created_at, model_version, status (engine table);
  session_id, review_status, expected_character (collection table).

## Guarantees (verified)
- **Idempotent / already-applied detection:** `migration_state()` + `CREATE TABLE IF NOT
  EXISTS` / guarded `ALTER`; re-running `migrate()` is a no-op (test:
  `test_migration_additive_idempotent_and_old_records`).
- **Old records readable + unchanged** after migration (verified).
- **One final production record per session** (vin_records); the three engine rows are
  evidence, not production records (verified in `test_shadow_stage_stores_three_evidence_one_final`
  and the replay).
- **JSON round-trip** for per_character/confidence/boxes/raw_payload (verified).

## Rollback
Additive-only: dropping the two new tables + the seven additive columns fully reverts, with
no impact on existing rows. Or simply set `ocr_release.release_mode: DISABLED` — nothing new
is written. Production `master` never carried this schema.
