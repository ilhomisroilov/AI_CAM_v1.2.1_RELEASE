# AI_CAM v1.2.1 OCR-Engine Release — Report

**Date:** 2026-07-27 · Executed solo (subagents blocked by spend limit). Production
`master` (b92324f) untouched; PLC/RFID/session ownership/capture-slot/MISSED_CAPTURE_WINDOW
and D2223-absence unchanged.

## FINAL DECISION (updated by the shadow-integration slice)
> ## V1.2.1 BUILT — SHADOW RELEASE READY
> The model track (below) plus the shadow integration are now complete and offline-verified:
> three-engine orchestration, additive DB migration, pipeline wiring, raw-Paddle preservation,
> non-blocking active-learning collector + cropchar saving, offline replay PASS, full suite
> 341 passed, shadow-cannot-alter-final, and rollback verified. See
> `reports/V121_OCR_INTEGRATION_REPORT.md` for the authoritative shadow-release gate + §19 block.
> GUARDED_PRIMARY remains disabled (weak-class evaluation blockers); PRODUCTION RELEASE VERIFIED
> requires live PLC/camera/RFID replay (not performed). The model-build details below stand.

## What is BUILT and VERIFIED
- **Trained model** (`models/engraved_ocr_v1.2.1/`): MobileNetV3-Small, 21 charset classes
  + REJECT = **22 outputs**. Deterministic (seed 1337), grouped canonical split (no leakage),
  class-weighted loss, identity-preserving aug only, best checkpoint by val macro-F1,
  pretrained backbone.
- **Evaluation (test n=155):** accuracy 0.742 · macro-F1 0.670 · reject-F1 0.843 · top-2
  0.839 · CPU latency ~12.7 ms/char. Confusion matrix + per-class PRF written.
- **ONNX export + parity:** OK, max-abs-diff **8e-05** (PyTorch↔onnxruntime).
- **Artifacts + contract:** all 12 files incl. metadata (output_dim 22, charset_len 21),
  per-class thresholds, checksum.sha256, README.
- **Engine adapter** (`backend/ai/engines/engraved_v121.py`): loads ONNX + metadata with
  runtime validation (refuses to start on output-dim/charset/checksum/missing-file mismatch);
  adaptive segmentation; per-char classify + UNKNOWN/REJECT gate; **never invents a character**;
  conforms to the v1.2.1 `OCRRecognizer` contract; returns raw + gated + per-char + confidences
  + alternatives + boxes + model version + latency.
- **Tests:** `tests/test_engraved_v121_engine.py` (5) + OCR contract/config/safety + session
  ownership + stale-RFID subset → **58 passed**, no regression; session invariants intact.
- **Safety:** rollback tag + release branch; PaddleOCR protobuf conflict (onnx pulled 7.35)
  detected and remediated (restored 3.20.2, paddle verified).

## What is NOT done / NOT verified (honest blockers)
- Async **3-engine orchestration** (ENGRAVED_V121 ∥ PADDLE_RAW ∥ PADDLE_ENHANCED) in the
  live session pipeline — designed via the existing router, not wired/verified.
- **DB migration** (`ocr_engine_results`, `ocr_collection_items`, final-OCR columns) — not applied.
- **Active-learning collector** + cropchar storage — not implemented in the running pipeline.
- **pipeline.py wiring**, config flags in production, health-report fields — not completed.
- **Hardware replay / smoke test** — impossible here (no PLC/camera/RFID).
- **Guarded-primary** — blocked by evaluation: weak classes unreliable (`5` F1=0; `A`/`H`
  have 0 test samples; `6`/`7` mediocre).

## §23 final output
```
BRANCH:                 release/v1.2.1-ocr-engine
BASE COMMIT:            3ea037c (refactor/v1.2.1-architecture; has engine-neutral contract)
ROLLBACK TAG:           rollback/pre-v1.2.1-ocr-release -> 3ea037c   (master b92324f untouched)
FINAL COMMIT:           <this release-branch commit>
DATASET:                729 human-verified crops (21 classes) + 309 reject crops
CHARSET:                0123456789ABCDEFHJNST (21) + REJECT
TRAIN/VALIDATION/TEST:  726 / 157 / 155 (incl. grouped reject split); leakage 0
MODEL:                  MobileNetV3-Small, 22 outputs
MODEL CHECKSUM:         see models/engraved_ocr_v1.2.1/checksum.sha256
OUTPUT DIMENSION:       22 (21 + REJECT)
TEST MACRO F1:          0.670
REJECT F1:              0.843
WEAK CLASS RESULTS:     5 F1=0 (3); A/H no test evidence; 6=0.44; 7=0.60; B=0.80; D=1.0(2)
G/V UNKNOWN TEST:       structurally guaranteed (not output classes); real-sample test pending collector
PYTORCH/ONNX PARITY:    PASS (max abs diff 8e-05)
CPU LATENCY:            ~12.7 ms/char
PADDLE RAW STORAGE:     NOT IMPLEMENTED (design only)
PADDLE ENHANCED STORAGE:NOT IMPLEMENTED (design only)
THREE-ENGINE PARALLELISM:NOT WIRED (router exists; adapter ready)
DATABASE MIGRATION:     NOT APPLIED
COLLECTOR:              NOT IMPLEMENTED
CROPCHAR SAVING:        adaptive extraction proven in dataset tooling; collector not wired
SESSION OWNERSHIP:      UNCHANGED + verified (subset tests pass)
AI_CAM TESTS:           OCR+session subset 58 passed (full suite not re-run this session)
NEW OCR TESTS:          engraved engine 5 passed
REPLAY:                 NOT RUN (no hardware)
HEALTH CHECK:           engine health()/metadata() implemented; pipeline health fields pending
ROLLBACK:               tag present; master untouched; engraved engine not wired => production unchanged (safe)
VERIFIED RUN COMMAND:   ..\AI_CAM\.venv\Scripts\python.exe tools\train_engraved_v121.py ;
                        ..\AI_CAM\.venv\Scripts\python.exe tools\evaluate_engraved_v121.py ;
                        ..\AI_CAM\.venv\Scripts\python.exe -m pytest tests\test_engraved_v121_engine.py -q
FINAL DECISION:         MODEL + ENGINE BUILT AND VERIFIED — FULL RELEASE INTEGRATION INCOMPLETE
```

## Rollback
`git checkout master` (production, b92324f) — unaffected. The release work is isolated on
`release/v1.2.1-ocr-engine`; the engraved engine is not wired into production, so current
behavior is unchanged. Tag `rollback/pre-v1.2.1-ocr-release` pins the pre-release base.

## Recommended path to SHADOW release
Implement + test (offline where possible): additive DB migration on a temp DB; the 3-engine
async orchestrator with per-engine isolation/timeout + session-owned evidence (fake-engine
concurrency tests); the non-blocking collector (synthetic-input tests); wire the router into
`pipeline.py` behind `OCR_PRIMARY_ENGINE=PADDLE_EXISTING` (shadow); offline replay of recorded
lines. Then run the full AI_CAM suite + a smoke test before enabling shadow on the line.
