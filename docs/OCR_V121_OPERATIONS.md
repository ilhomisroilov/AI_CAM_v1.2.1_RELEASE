# OCR v1.2.1 Operations

## Health
`scripts/health_v121.ps1` (or `ocr_v121_health()` in-app) reports: release_mode, active final
engine policy, per-engine readiness, model version + checksum, charset (0123456789ABCDEFHJNST),
output dimension (22), thresholds loaded, unsupported classes (G/V), weak-class policy,
collector status + root, migration state, last per-engine latency, timeout/error counts,
collected item count. Health failures of the shadow components are visible but do NOT prevent
legacy startup in SHADOW.

## Day-to-day (SHADOW)
- The engraved engine + collector run as evidence only. Production VIN/RFID binding is the
  legacy Paddle decision. Watch `ocr_engine_results` (3 rows/session) and `vin_records.final_ocr_*`.
- Active-learning crops accumulate under `data/engraved_ocr_collection/`. Review them:
  ```
  pwsh -File scripts\build_collection_queue.ps1
  ..\AI_CAM\.venv\Scripts\python.exe tools\glyph_review_app\server.py --reviewer "Ilhom" --queue character_dataset\manifests\collection_review_queue.csv --open
  pwsh -File scripts\import_collection.ps1
  ```
  Then `tools/rebuild_registry.py` → `tools/build_production_split.py` → retrain
  (`tools/train_engraved_v121.py` + `tools/evaluate_engraved_v121.py`).

## Alarms / safety
- Collector or DB-evidence errors are counted and non-blocking (never stop OCR/session).
- A late/old-session result stores evidence under its OWN session and never overwrites a
  newer session's final (verified). Stale-RFID / capture-slot / MISSED_CAPTURE_WINDOW behavior
  is unchanged.
- G and V always resolve to UNKNOWN; they are never emitted as a confident known class.

## Promotion checklist to GUARDED_PRIMARY
Model macro-F1 gate + real test evidence for weak classes (5/A/H) + latency within takt +
`scripts/run_v121_guarded.ps1` no longer refusing (exit 0) + a hardware replay. Until then keep
SHADOW.
