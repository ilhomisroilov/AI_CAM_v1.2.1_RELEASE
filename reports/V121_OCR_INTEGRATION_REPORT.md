# Historical Migration Record — AI_CAM v1.2.1 Shadow OCR Integration

Any worktree command/path below records original integration activity only. It
is not an installation or runtime instruction for the clean release.

**Date:** 2026-07-27 · Branch `release/v1.2.1-ocr-engine` · Executed solo (no subagents).
Production `master` (b92324f) untouched; PLC/RFID/session-ownership/capture-slot/
MISSED_CAPTURE_WINDOW unchanged; D2223 not reintroduced.

## FINAL DECISION
> ## V1.2.1 BUILT — SHADOW RELEASE READY
> All offline SHADOW release gates are complete and verified. The three engines run
> concurrently for the same session-owned input, engine evidence + one final session-owned
> decision are stored additively, raw Paddle evidence is preserved, the active-learning
> collector saves cropchar images non-blockingly, the offline replay passes, the full
> AI_CAM suite passes, SHADOW cannot alter the production result, and rollback is verified.
> GUARDED_PRIMARY is implemented but **disabled** (weak-class evaluation blockers).
> Hardware (PLC/camera/RFID) replay is NOT claimed — that is required only for PRODUCTION
> RELEASE VERIFIED.

## What was built (offline-verified)
- **Three-engine async orchestrator** (`backend/ai/ocr_orchestrator.py`): ENGRAVED_V121 ∥
  PADDLE_RAW ∥ PADDLE_ENHANCED, per-engine timeout + error isolation (one failure/timeout
  never cancels the others), session-owned results, disagreement detection, and a documented
  SHADOW/GUARDED decision policy with the full reason enum. Never invents a hybrid string.
- **Paddle RAW / ENHANCED** (`backend/ai/engines/paddle_profiles.py`): independent real
  adapter instances; RAW preserves the untouched raw plurality text; ENHANCED uses a
  controlled contrast profile. Neither mutates the other.
- **Additive DB migration** (`backend/database/ocr_v121_db.py`): `ocr_engine_results` +
  `ocr_collection_items` tables + `vin_records.final_ocr_*` columns + indexes; idempotent,
  old-record-compatible, JSON round-trip. One final production record per session; three
  engine rows are evidence.
- **Active-learning collector** (`backend/ai/ocr_collector.py`): non-blocking background
  worker; adaptive character-crop saving (never blind equal-width); trusted-label-only
  (session VIN); dedup by (source_hash, position, crop_hash, session); atomic writes; error
  counters; never blocks production. Unsupported G/V are never renamed to a known class.
- **Shadow stage + health** (`backend/ai/ocr_shadow_stage.py`): single session-owned entry
  that runs the orchestrator, stores evidence + the one final decision, dispatches the
  collector, and re-checks session ownership before any final write. `ocr_v121_health()`
  reports mode/engines/charset/output-dim/unsupported/weak-policy/collector/migration/latency.
- **Pipeline wiring** (`backend/ai/ocr_shadow_hook.py` + one guarded call in
  `Pipeline._on_ocr_result`): additive, flag-guarded, fully isolated — SHADOW is evidence
  only and cannot alter the session's VIN/RFID binding or any session-safety behavior.
- **Collection review → import** (`tools/build_collection_review_queue.py`,
  `tools/import_reviewed_collection.py`): PENDING → priority queue (V/G/A/D/H/B/5/6/7) →
  only explicit human ACCEPT enters KNOWN; import-once (IMPORTED_TO_DATASET); G/V excluded.
- **Config** (`ocr_release` section + `OCRReleaseConfig`); **scripts** (`scripts/*.ps1`,
  self-check/dry-run tested); **offline replay** (`tools/replay_v121.py`).

## Verification evidence
- **Offline replay: PASS** (all 12 invariants incl. late/old-session isolation): 3 evidence
  rows, 1 final, shadow-final-is-legacy, raw-paddle-preserved, disagreement, late-session
  isolation, 34 cropchar files, collector non-blocking 0 errors, health migration+engraved ready.
- **Full AI_CAM suite: 341 passed, 2 skipped** (no regression; session-safety intact).
- **v1.2.1 integration tests: 16 passed** (concurrency, timeout/exception isolation,
  shadow-can't-alter, guarded accept/weak-block, session ownership, migration idempotent,
  3-evidence/1-final, collector crop/dedup/non-blocking/never-rename-unsupported,
  collection import-once, health). Engine tests: 5 passed.
- **Scripts self-check:** health OK; shadow -SelfCheck OK; **guarded REFUSES (exit 3)**;
  rollback reports master untouched.

## §19 final output
```
BRANCH:                 release/v1.2.1-ocr-engine
START COMMIT:           188622a
FINAL COMMIT:           <this commit>
ROLLBACK TAG:           rollback/pre-v1.2.1-shadow-integration -> 188622a ;
                        rollback/pre-v1.2.1-ocr-release -> 3ea037c ; master untouched b92324f
MODEL:                  MobileNetV3-Small, 21 charset + REJECT = 22 outputs
MODEL CHECKSUM:         models/engraved_ocr_v1.2.1/checksum.sha256
MODEL METRICS:          test acc 0.742, macro-F1 0.670, reject-F1 0.843, top-2 0.839, ONNX parity 8e-05
RELEASE MODE:           SHADOW (safe default)
THREE ENGINES:          COMPLETE (concurrent, isolated, session-owned) — verified
DATABASE MIGRATION:     additive + idempotent — verified (temp DB + replay)
RAW PADDLE STORAGE:     preserved unchanged — verified
COLLECTOR:              non-blocking, trusted-label-only, dedup — verified
CROPCHAR SAVING:        adaptive crops saved (34 in replay) — verified
PIPELINE WIRING:        COMPLETE (additive, flag-guarded, isolated); session invariants intact
SESSION OWNERSHIP:      UNCHANGED + verified (late/old-session isolation in replay + tests)
OFFLINE REPLAY:         PASS
FULL TESTS:             341 passed, 2 skipped
POWERSHELL SCRIPTS:     8 scripts; self-check/dry-run exercised
HEALTH:                 implemented + reported
ROLLBACK:               verified (master untouched; config DISABLED path; tag; script)
HARDWARE STATUS:        NOT run (offline only) — required only for PRODUCTION RELEASE VERIFIED
VERIFIED SHADOW RUN COMMAND:
  cd C:\Users\II4028\Documents\Projects\AI_CAM_v1.2.1_worktree
  pwsh -File scripts\run_v121_shadow.ps1 -SelfCheck   # validate; drop -SelfCheck to start
FINAL DECISION:         V1.2.1 BUILT — SHADOW RELEASE READY
```

## Not claimed / next steps
- **GUARDED_PRIMARY** stays disabled until the model clears evaluation gates (weak classes
  `5` F1=0, `A`/`H` no test evidence). The collector is enabled to gather exactly those
  (V/G/A/D/H/B/5/6/7) + disagreement cases for the next dataset round.
- **PRODUCTION RELEASE VERIFIED** requires a real PLC/camera/RFID replay on the line.
