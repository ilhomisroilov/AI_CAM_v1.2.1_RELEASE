# Test Results — AI_CAM v1.2.1 Production Stabilization

`pytest tests/` on Windows/CPU (`.venv`): **417 passed, 4 skipped, 0 failed**
(2m37s). No hardware/GPU required for the suite; the Playwright test drives a real
headless Chromium.

## New / changed v1.2.1 Production Stabilization tests
| file | n | covers |
|------|---|--------|
| `test_plc_edge_audit.py` | 12 | debounce, pulse-width, interval, active-cycle, invalid width, audit files |
| `test_session_reliability_v13.py` | 4 | DUPLICATE_TRIGGER_IGNORED, SUSPICIOUS_EARLY_TRIGGER, accept-after-interval, audit |
| `test_ocr_submit_gate_v13.py` | 5 | YOLO 0.899 blocked / 0.900 allowed / best_q can't override / VIN_LOCKED cancel |
| `test_dataset_alignment_v13.py` | 5 | alignment source, engraved boxes preferred, equal-split not training-eligible |
| `test_ocr_disagreement_v13.py` | 7 | F/E·U/V disagreement, frame independence, weak-position guard |
| `test_failure_evidence_v13.py` | 4 | decoded>0 ⇒ evidence not empty (crop / frame / finalize) |
| `test_observe_production_v13.py` | 2 | 24h aggregation + COMPLETED/NOT COMPLETED report |
| `test_frontend_playwright.py` | 1 | real-browser dashboard/history polling (P0 regression) |

Existing session/PLC/OCR/stress/soak/chaos suites unchanged (the shared
`pipeline_instance` fixture runs in simulator/queue mode; production semantics are
covered by the dedicated `*_v13` tests).

## Not executed here (require the Ubuntu GPU host)
- `python run.py --self-check --require-gpu` (Torch cu118 + Paddle-GPU + YOLO cuda:0).
- Live PLC D2222 / RFID R700 / SICK camera integration.
- `python run.py --observe-hours 24` — 24h production observation.
