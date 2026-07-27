# OCR v1.2.1 Collection Workflow

Turns production hard/uncertain/target cases into new human-verified training data. Full
detail: `reports/V121_COLLECTION_REPORT.md`.

## Loop
```
production (SHADOW)  →  ocr_collection_items (PENDING) + cropchar files
   → scripts/build_collection_queue.ps1            (priority: V,G,A,D,H,B,5,6,7,0,2,3)
   → glyph review app --queue collection_review_queue.csv   (human ACCEPT/REJECT/UNCERTAIN)
   → scripts/import_collection.ps1                 (ACCEPT + TRUSTED_LABEL only → KNOWN; import-once)
   → tools/rebuild_registry.py                     (register derived files; unresolved=0)
   → tools/build_production_split.py               (leakage-safe grouped split)
   → tools/train_engraved_v121.py + evaluate       (retrain; re-check gates)
```

## Rules
- Only **explicit human ACCEPT** with a **TRUSTED_LABEL** enters KNOWN. REJECT/UNCERTAIN never do.
- Trusted labels come from the session-owned DB VIN / RFID association / operator only — never
  OCR predictions or filenames.
- **G/V are never imported as known classes** (unsupported); they wait for enough real samples.
- Import is idempotent: imported rows are marked `IMPORTED_TO_DATASET` and cannot be imported
  twice (verified test).

## Priority
Collector targets the deficit/weak classes first (V,G,A,D,H,B,5,6,7,0,2,3) plus disagreement,
UNKNOWN, low-confidence, low-margin, and segmentation-reject cases — exactly the evidence needed
to clear the GUARDED_PRIMARY weak-class gates.
