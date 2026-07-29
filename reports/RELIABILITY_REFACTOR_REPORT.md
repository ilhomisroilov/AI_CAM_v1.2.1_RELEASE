# AI_CAM v1.2.1 Production Stabilization Reliability & OCR Dataset Overhaul — Report

Branch `stabilization/v1.2.1-production`. Built and verified on a Windows/CPU dev box;
GPU (`cuda:0`, Paddle-GPU) and live hardware (D2222 / R700 / SICK camera) and the
24h observation are **NOT run here** — they belong to the Ubuntu production host and
are marked accordingly. Nothing is faked.

## Per-item status

| # | Item | Status | Evidence |
|---|------|--------|----------|
| — | PLC/session reliability (prior slice, kept) | DONE | `6e3b9f0` + regression |
| 1 | `_submit_ocr_frames` strict YOLO ≥ 0.90 gate | DONE | `test_ocr_submit_gate_v13.py` |
| 2 | Same-source preprocess variants not independent | DONE | `independent_frame_count`, `test_ocr_disagreement_v13.py` |
| 3 | Store ENGRAVED_V121 / PADDLE_RAW / PADDLE_ENHANCED separately | DONE (pre-existing, verified) | `ocr_shadow_stage.store_engine_result` |
| 4 | F/E · U/V per-character disagreement audit | DONE | `char_disagreements`, tests |
| 5 | High-confidence U/V false-positive guard | DONE | `weak_position_ok`, tests |
| 6 | Cancel OCR retries after accepted VIN (VIN_LOCKED) | DONE | `Session.vin_locked`, tests |
| 7 | Remove equal-width 17-split from production | DONE | collector alignment_source, tests |
| 8 | Char crops via engraved boxes / CTC alignment | PARTIAL | ENGRAVED_BOXES + ADAPTIVE_PROJECTION done; CTC deferred |
| 9 | Alignment fail → PENDING_REVIEW, equal split not training-eligible | DONE | `test_dataset_alignment_v13.py` |
| 10 | Failure evidence images (NO_CROP/NO_DETECTION/OCR_EMPTY/OCR_AMBIGUOUS) | DONE | `_save_failure_evidence`, tests |
| 11 | `decoded_frames > 0` ⇒ evidence path not empty | DONE | `test_failure_evidence_v13.py` |
| 12 | `tools/observe_production.py` + 24h report generator | DONE (tool) | `test_observe_production_v13.py`; 24h run: on server |
| 13 | README / ARCHITECTURE / OCR_FUSION / DATASET_CHARACTER_ALIGNMENT / release notes | DONE | this commit |
| 14 | Markdown inventory (KEEP/UPDATE/MERGE/ARCHIVE/DELETE) | DONE | `docs/DOCUMENTATION_INVENTORY.md` |
| 15 | Full regression | DONE | **421 passed, 4 skipped, 0 failed** |
| 16 | Morning preflight (`tools/morning_preflight.py --require-gpu`) | DONE | `test_stabilization_tools_v13.py`; verified PASS on clean tree |
| 17 | Observation run-metadata (version/commit/config/model hashes) | DONE | stamped into `PRODUCTION_24H_*` |
| — | New release tag / GitHub release | **NOT CREATED (by design)** | withheld until the 24h observation passes |
| — | 24-hour production observation | **NOT COMPLETED** (no hardware here) | run on Ubuntu host |
| — | Real GPU / hardware integration | **NOT COMPLETED** (no GPU/LAN here) | `python tools/morning_preflight.py --require-gpu` on host |

## Key root causes addressed
- **best_q could override YOLO** → strict submit gate (item 1).
- **Preprocess variants inflated "independent" evidence** → source-frame-hash
  independence + weak-position guard (items 2, 5).
- **Validator silently rewrote raw OCR (E→F, U→V)** → disagreement audit +
  VALIDATOR_CHANGED_CHARACTER flag; no hidden correction (item 4).
- **Equal 17-sector split produced bad character crops** → alignment-source tracking;
  equal split never training-eligible (items 7, 9).
- **Decoded-but-failed cycles had empty evidence** → failure-evidence saver
  (items 10, 11).

## Commits (on top of the PLC/session slice `6e3b9f0`)
`a630f38` YOLO gate + VIN lock · `99b5f8d` dataset alignment · `44c5fb7`
disagreement/guard · `b0ab59b` failure evidence · `fb415c5` observation tool ·
`f4e1f68` docs · `e1ba31c` v1.2.1 stabilization metadata correction (VERSION 1.2.1,
branch rename, v1.3.0-rc1 tag deleted, morning preflight) · `chore` runtime/audit gitkeep.

No version tag or GitHub release is created; that is withheld until the 24-hour
observation passes on the Ubuntu GPU host.

Rollback: `git checkout master` (production untouched); the work is isolated on
`stabilization/v1.2.1-production`.
