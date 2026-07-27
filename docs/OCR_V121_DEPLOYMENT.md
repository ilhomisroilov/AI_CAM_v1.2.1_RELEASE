# OCR v1.2.1 Deployment

Release branch `release/v1.2.1-ocr-engine`. Do NOT merge to master. Default mode: **SHADOW**.

## Prerequisites (verified)
- torch 2.4.1+cpu, torchvision, onnx/onnxruntime (protobuf pinned ≤3.20.2 for PaddleOCR).
- Model artifacts present: `models/engraved_ocr_v1.2.1/` (charset.txt, model.onnx,
  model_metadata.json, confidence_thresholds.json, checksum.sha256). Binaries are gitignored;
  reproduce with `tools/train_engraved_v121.py` + `tools/evaluate_engraved_v121.py`.

## Config (`config/settings.yaml` → `ocr_release`)
`release_mode: SHADOW` · `engraved_enabled: true` · `collection_enabled: true` ·
`weak_classes_enabled: false`. Change `release_mode: DISABLED` to fully turn the new engine off.

## Verified commands (Windows PowerShell, from the worktree root)
```powershell
pwsh -File scripts\migrate_v121.ps1              # additive DB migration (idempotent)
pwsh -File scripts\health_v121.ps1               # readiness/self-check JSON
pwsh -File scripts\replay_v121.ps1               # offline 3-engine replay (must PASS)
pwsh -File scripts\run_v121_shadow.ps1 -SelfCheck   # validate SHADOW without starting
pwsh -File scripts\run_v121_shadow.ps1           # start AI_CAM in SHADOW (default safe)
pwsh -File scripts\run_v121_guarded.ps1 -Override # REFUSES until eval gates pass (weak classes)
```

## Rollout order
1. SHADOW (default) — collect evidence + active-learning data on the line (no production impact).
2. Only after the model clears evaluation gates (weak classes) and a hardware replay:
   consider GUARDED_PRIMARY (still per-gate, fallback to Paddle). Never ENGRAVED_STRICT by default.

## Status
**SHADOW RELEASE READY** (offline-verified). GUARDED_PRIMARY blocked by weak-class evaluation.
PRODUCTION RELEASE VERIFIED requires a live PLC/camera/RFID replay (not performed).
