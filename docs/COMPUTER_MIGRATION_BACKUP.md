# AI_CAM computer migration backup

This file is the handoff checklist for moving AI_CAM to another computer.

## Current backup artifacts

- `requirements.freeze.current.txt` records the currently installed Python packages.
- `backup_manifest.current.json` records critical folders, file counts, sizes, git status, pip check output, and key SHA256 hashes.
- `data/ai_cam.migration_backup.db` is a SQLite backup copy created because `data/ai_cam.db` was locked during hashing.

Keep these files with the project folder when moving the machine.

## Must-copy data

Git alone is not enough. These paths are ignored or runtime-generated and must be copied:

- `data/ai_cam.db`
- `data/ai_cam.migration_backup.db`
- `data/crops/`
- `data/vin_ocr_dataset/`
- `models/vin_slot_recognizer_pilot/`
- `models/vin_recognizer_pilot/`
- `runs/detect/*/weights/`
- `dataset/images_raw/`
- `dataset/collected_raw/`
- `config/settings.yaml`
- `logs/`

Also copy normal source and docs:

- `backend/`, `frontend/`, `tools/`, `tests/`, `docs/`
- `run.py`, `requirements.txt`, `requirements.freeze.current.txt`
- `README.md`, `TRAINING_GUIDE.md`, `.gitignore`, `yolov8n.pt`

## Recommended copy command

From the project root, use an external drive or a folder on the new machine:

```powershell
.\tools\create_migration_backup.ps1 -DestinationRoot E:\Backups
```

If there is enough space and you want to preserve the current virtual environment too:

```powershell
.\tools\create_migration_backup.ps1 -DestinationRoot E:\Backups -IncludeVenv
```

The script creates a timestamped folder such as `AI_CAM_MIGRATION_20260709_145000`.
It does not delete destination files and it refuses to write inside the source project.

## Restore on the new Windows computer

1. Copy the backup folder to the new computer.
2. Install Python 3.11.
3. Open PowerShell in the restored `AI_CAM` folder.
4. Create a clean venv:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If dependency install or runtime behavior differs, use `requirements.freeze.current.txt` as the exact-current-environment reference.

## Important dependency notes

`requirements.txt` and the current `.venv` are not identical. Current freeze includes:

- `torch==2.12.0`
- `torchvision==0.27.0`
- `paddleocr==2.10.0`
- `paddlepaddle==2.6.2`

`pip check` currently reports conflicts from optional/tooling packages:

- `anylabeling` wants newer `numpy` and `Pillow`
- `onnx` wants newer `protobuf`
- `torchaudio` wants `torch==2.3.1`

For production, prefer a clean venv from `requirements.txt`; use the freeze file only when reproducing the old machine exactly.

## Restore validation

Run these after installing dependencies:

```powershell
python -m py_compile backend\config.py backend\ai\ocr_worker.py backend\ai\vin_fusion.py run.py
python tests\test_vin_fusion.py
python run.py
```

Then open:

```text
http://localhost:8000/dashboard
```

Check logs for:

- YOLO model loaded
- PaddleOCR pool/preload
- `[VIN_SLOT] Model yuklandi`
- database ready

Do not wipe the old computer until the dashboard opens on the new computer and one OCR test path is confirmed.

