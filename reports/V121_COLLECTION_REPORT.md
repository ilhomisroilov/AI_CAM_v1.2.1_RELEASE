# V121 Active-Learning Collection Report

Non-blocking collector (`backend/ai/ocr_collector.py`) that automatically acquires hard /
uncertain / target character examples for the next dataset round, using adaptive crops and
trusted labels only.

## Triggers
UNKNOWN character · low confidence · low top-1/top-2 margin · weak class (5,6,7,A,B,D,H) ·
Engraved/Paddle disagreement · Paddle RAW/ENHANCED disagreement · segmentation reject ·
trusted VIN contains an unsupported class (G/V) · configured target char. Priority chars:
**V,G,A,D,H,B,5,6,7,0,2,3**.

## Label trust (never OCR/filename)
1) session-owned trusted DB VIN, 2) valid RFID/VIN session association, 3) operator review.
No trusted VIN → item stored `UNLABELED`. **Unsupported classes (G/V) keep
`model_character=UNKNOWN_CHAR`** — never renamed to a visually similar known class (verified:
`test_collector_never_renames_unsupported_as_known`).

## Storage
`data/engraved_ocr_collection/<date>/SESSION_<id>/{source/frame.png, source/normalized_line.png,
chars/pos_NN_expected_X.png, metadata.json}` + a DB row per crop in `ocr_collection_items`
(hashes, box, confidences, Paddle raw/enhanced chars, reasons, label_status, review_status).
Atomic writes; dedup by (source_hash, position, crop_hash, session). **Collector errors are
logged + counted and never fail the production session** (verified: non-blocking + error
isolation tests + replay 34 crops / 0 errors).

## Review → dataset
`tools/build_collection_review_queue.py` → glyph review app (`--queue`) → human ACCEPT/REJECT/
UNCERTAIN → `tools/import_reviewed_collection.py` (only ACCEPT + TRUSTED_LABEL enter KNOWN;
G/V excluded; import-once via `IMPORTED_TO_DATASET`) → `tools/rebuild_registry.py` →
`tools/build_production_split.py` → retrain. Verified: `test_collection_queue_and_import_once`.
