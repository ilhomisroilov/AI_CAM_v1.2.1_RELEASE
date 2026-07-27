# Engraved OCR v1.2.1 — Model Artifacts

Trained MobileNetV3-Small engraved-character classifier for AI_CAM v1.2.1.

## Contract
- Input: grayscale, aspect-preserving pad to 64×64, replicated to 3 channels, normalized (x-0.5)/0.5.
- Charset (21): `0123456789ABCDEFHJNST` + 1 REJECT/UNKNOWN → **output dimension 22**.
- **G and V are NOT output classes** (no verified source). The model structurally cannot
  emit G/V; real G/V rejection needs collected samples (active-learning collector).

## Files
`best_model.pt` (PyTorch), `model.onnx` (opset 13), `charset.txt`, `label_to_index.json`,
`index_to_label.json`, `confidence_thresholds.json` (per-class; weak classes stricter),
`model_metadata.json`, `training_history.csv`, `confusion_matrix.csv`, `checksum.sha256`.

## Test metrics (canonical grouped test set, n=155)
accuracy 0.742 · macro-F1 0.670 · reject-F1 0.843 · top-2 0.839 · CPU latency ~12.7 ms/char ·
ONNX↔PyTorch parity max-abs-diff 8e-05.

## Honest limitations
Weak classes are unreliable: `5` F1=0 (3 samples), `A`/`H` have **0 test samples** (no test
evidence), `6`/`7` mediocre. **Not guarded-primary ready** — intended for SHADOW / active
data collection. Runtime validation refuses to load if output-dim/charset/checksum/files
mismatch metadata.

## Reproduce
```
..\AI_CAM\.venv\Scripts\python.exe tools\train_engraved_v121.py
..\AI_CAM\.venv\Scripts\python.exe tools\evaluate_engraved_v121.py
```
Deterministic seed 1337; grouped split from AI_CAM_OCR_DATASET_v1/character_dataset/splits/production_current_v1.
