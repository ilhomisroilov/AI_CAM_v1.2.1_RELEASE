# Portable Path Audit

Audit date: 2026-07-27  
Scope: release source tree, excluding `.git/`, `.venv/`, generated `build/`,
`dist/`, and ignored `runtime/` evidence.

## Result

PASS. No active application source, configuration, launcher, setup script,
packaging script, or test fixture depends on a user-profile checkout, the
v1.2.1 worktree, the original AI_CAM repository, `.paddleocr`, or `.cache`.
The startup self-check also resolved every required model and writable runtime
path beneath the release root.

## Classified matches

| Match | Location | Classification |
|---|---|---|
| `C:\Users\II4028\Documents\Projects\AI_CAM_v1.2.1_RELEASE` | `reports/CONTINUATION_CHECKPOINT.md` | Required continuation evidence recording the inspected root; not consumed at runtime. |
| `C:\Users\II4028\Documents\Projects\AI_CAM_v1.2.1_RELEASE` | `reports/FINAL_RELEASE_CONSOLIDATION_REPORT.md` | Required final-report field recording the verified release root; not consumed at runtime. |
| Original `AI_CAM` checkout | `docs/OCR_MIGRATION_REPORT.md` | Explicitly labelled historical, non-operational migration record. |
| `AI_CAM_v1.2.1_worktree` | `reports/V121_OCR_INTEGRATION_REPORT.md` | Explicitly labelled historical command from the predecessor release record; not a current instruction. |
| `~/.paddleocr` | `backend/ai/ocr_process.py` | Negative safety comment explaining that cache fallback is forbidden. |
| `~/.paddleocr` | `docs/OCR_MIGRATION_REPORT.md` | Explicitly labelled historical, non-operational pre-v1.2.1 advice. |
| `.paddleocr` substring | `docs/RUNTIME_DEPLOYMENT.md` | False positive inside the official `paddleocr.ai` documentation hostname. |
| `.cache` | none | No matches. |

## Runtime path contract

- `runtime/data/ai_cam.db`
- `runtime/logs`
- `runtime/crops`
- `runtime/temp`
- `runtime/engraved_ocr_collection`
- `models/yolo/best.pt`
- `models/engraved_ocr_v1.2.1/model.onnx`
- `models/engraved_ocr_v1.2.1/model_metadata.json`
- `models/paddle/det`
- `models/paddle/rec`
- `models/paddle/cls`

Paddle initialization receives all three explicit model directories. A real
dry-run was executed with `PADDLE_HOME` and `PADDLEOCR_HOME` redirected to an
empty ignored runtime directory; no model-cache file was created there.
