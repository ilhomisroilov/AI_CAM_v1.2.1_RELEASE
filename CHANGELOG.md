# Changelog

## 1.3.0-rc1 — 2026-07-28 (release candidate)

Reliability & OCR dataset overhaul (branch `release/v1.3.0-reliability`). Final
`v1.3.0` is gated on a 24-hour production observation on the Ubuntu GPU host.

- **PLC forensic audit** of D2222: debounced rising/falling timeline with
  pulse-width, inter-edge interval, ring-buffer context and per-edge decision
  (`runtime/audit/plc_edges_*.csv/jsonl`).
- **Production body-cycle invariant** — 1 body = 1 cycle = 1 record. Triggers during
  an active cycle → `DUPLICATE_TRIGGER_IGNORED`; earlier than 90 s →
  `SUSPICIOUS_EARLY_TRIGGER`; both audited. No pending-trigger queue in production
  (queue retained for the simulator/test bench). D2222-only; D2223 stays disabled.
- **Strict YOLO ≥ 0.90 OCR submit gate** — `best_q` cannot override detector
  confidence. `VIN_LOCKED` cancels OCR retries after an accepted VIN.
- **F/E · U/V per-character disagreement audit** + weak-position false-positive
  guard (independent frames by source-frame hash, not preprocess variants). Three
  engines stored separately.
- **Dataset character alignment** — engraved boxes / adaptive projection preferred;
  equal-width split never production training-eligible (→ `PENDING_REVIEW`).
- **Failure-evidence images** — decoded-but-no-VIN cycles always keep an image.
- **`tools/observe_production.py`** + `run.py --observe-hours N` — real 24h
  observation and report generator; never mocked, never faked.
- New docs: `ARCHITECTURE`, `OCR_FUSION`, `DATASET_CHARACTER_ALIGNMENT`,
  `FAILURE_EVIDENCE`, `PRODUCTION_OBSERVATION`, `DOCUMENTATION_INVENTORY`, release
  notes. Full regression **417 passed, 4 skipped**.

## 1.2.1 — 2026-07-27

- Consolidated a portable, independent release from source commit `2e81457`.
- Moved runtime DB/log/crop/temp/collection data under `runtime/`.
- Bundled YOLO, Engraved v1.2.1 ONNX/PT and Paddle det/rec/cls artifacts.
- Added canonical self-check, dry-run, startup path audit and idempotent DB gates.
- Enforced explicit project-local Paddle model directories with no cache fallback.
- Replaced history and dashboard timer duplication with single polling owners.
- Added stable-evidence OCR retry deduplication and plate-exit terminal handling.
- Moved three-engine shadow orchestration to the session-owned common-input stage.
- Added independent real Paddle RAW/ENHANCED calls and native evidence persistence.
- Added local environment/setup/run scripts and PyInstaller ONEDIR packaging.
- Replaced tracked deployment credentials with safe placeholders/environment input.
