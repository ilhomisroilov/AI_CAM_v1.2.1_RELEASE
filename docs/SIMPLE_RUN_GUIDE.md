# AI_CAM v1.2.1 — Simple Run Guide

Copy the project to **any** folder, create a virtual environment, install the
requirements, and start it with a single command. No `/opt`, no `/etc`, no
systemd, no root, no service user, no Docker.

---

## Ubuntu (GPU production server)

```bash
cd ~/AI_CAM
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-gpu.txt      # NVIDIA / CUDA 11.8 engine stack
python run.py
```

## Windows (dev / CPU)

```bat
cd C:\Projects\AI_CAM
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

> Windows/CPU does not need `requirements-gpu.txt`; `run.py` auto-detects the
> absence of CUDA and falls back to a safe CPU runtime.

Then open **http://\<this-machine-ip\>:8080/dashboard** from any PC on the LAN.
Default web login is **admin / admin** (change it in `config/settings.yaml` →
`auth.password`, or set `AI_CAM_AUTH_PASSWORD`).

---

## What `python run.py` does (in order)

1. Finds the project root (works from any folder; PyInstaller-aware).
2. Loads the canonical config `config/settings.yaml` (+ optional env overrides).
3. Creates the `runtime/` folders (`data logs crops temp backups
   engraved_ocr_collection`).
4. Migrates the SQLite database `runtime/data/ai_cam.db` (idempotent — never
   resets or deletes existing records).
5. Validates the models (YOLO, Engraved ONNX, PaddleOCR det/rec/cls).
6. Detects GPU vs CPU and loads YOLO + the three OCR engines.
7. Starts the PLC (D2222), RFID (R700) and camera services.
8. Serves the FastAPI backend + dashboard/history/API on `0.0.0.0:8080`.

Missing hardware or credentials produce a **clear warning and the app still
starts** — the dashboard shows the affected device as *disconnected* rather than
crashing.

---

## Credentials

The camera / RFID passwords are auto-loaded (if present) from the git-ignored
`deploy/local/ai-cam.env`, or from environment variables — both optional. To set
them explicitly, edit `deploy/local/ai-cam.env`:

```
AI_CAM_CAMERA_PASSWORD="…"
AI_CAM_RFID_USERNAME="root"
AI_CAM_RFID_PASSWORD="…"
```

Never commit real secrets — `deploy/local/` and `*.env` are already git-ignored.

---

## Useful non-server commands

```bash
python run.py --self-check     # validate models + deps + GPU/CPU, then exit
python run.py --dry-run        # + real OCR engine init/inference on a temp DB
python run.py --migrate        # apply DB migration only
python run.py --print-config   # effective config (secrets redacted)
python run.py --health-check    # probe a running local server's /health
python run.py --no-hardware    # start with PLC/RFID in simulator-safe mode
```
