# AI_CAM v1.2.1 — Final Portable Validation

Branch `refactor/portable-one-command-runtime`. Verified on Windows 11 / CPU with
the project `.venv` (Torch 2.4.1+cpu, Paddle 2.6.2). Items marked **VERIFY-ON-SERVER**
require the Ubuntu GPU host / factory-LAN hardware and were not (and cannot be)
executed from this dev box — no result is fabricated for them.

| Area | Result | Evidence |
|------|--------|----------|
| Configuration (single canonical file) | **PASS** | `--print-config`: source = `config/settings.yaml` only, `load_errors=[]`, `missing_env_refs=[]`, `release_mode=GUARDED_PRIMARY`. |
| One-command start (`python run.py`) | **PASS** | Default full start → `/health` 200, all services up (no `--no-hardware` needed). |
| Portable paths (no /opt,/etc,/var,C:\Users) | **PASS** | `test_portable_runtime.py`; all runtime/model roots under `PROJECT_ROOT`. |
| Credential auto-load (secret-safe) | **PASS** | camera/RFID resolved from git-ignored `deploy/local/ai-cam.env`; path-root vars ignored; `deploy/local/` git-ignored. |
| admin/admin login (non-blocking) | **PASS** | Browser login admin/admin → dashboard; `auth`/secret gates now warn, never raise (`test_auth_security.py`). |
| Startup self-check | **PASS** | `run.py --self-check` → `ok:true`, 7/7 models validated, all deps import. |
| Dry-run (OCR engine init + inference) | **PASS (path)** | `run.py --dry-run` wired; CPU engine init/inference on a temp DB. |
| GPU real (Torch cu118 / Paddle-GPU / YOLO cuda:0) | **VERIFY-ON-SERVER** | Dev box is CPU (`profile=cpu-fallback`); run `run.py --self-check --require-gpu` on the GPU host. |
| YOLO model load | **PASS** | `/health` → `yolo.ready=true` (device cpu here / cuda:0 on server). |
| PaddleOCR (RAW + Enhanced) | **PASS (CPU)** | OCR preload ready; det/rec/cls dirs self-contained under `models/paddle/`. |
| Engraved OCR (GUARDED_PRIMARY) | **PASS** | `release_mode=GUARDED_PRIMARY`; guarded decision + fallback covered by `test_ocr_v121_integration.py`. |
| Camera (10.123.86.42) | **VERIFY-ON-SERVER** | Not reachable from dev box; service starts, reports disconnected. |
| PLC D2222 (10.123.40.99) | **VERIFY-ON-SERVER** | `/api/plc/status`: running, mode melsec, connected=false (no hardware). D2222 only, no D2223. |
| RFID R700 (10.123.18.3) | **VERIFY-ON-SERVER** | `/api/rfid/status` 200; service starts, reports disconnected. |
| API endpoints | **PASS** | `/health /api/status /api/plc/status /api/rfid/status /api/records` all 200 with expected fields. |
| Dashboard status + polling | **PASS** | Browser: cards populate from `/api/status`; single-owner visibility-aware poller; pause when hidden. |
| History records | **PASS** | `/api/records` 200; table rendered existing DB record (VIN/RFID/model/confidence/session timing). |
| Video feed endpoint | **PASS (endpoint)** | `/video_feed` wired; streams only when camera connected (placeholder otherwise). |
| Browser console errors | **PASS** | Zero console errors on dashboard and history. |
| Static asset freshness | **PASS** | `Cache-Control: no-cache` on `/static` + `?v={app_version}` on key assets. |
| Database (runtime/data, idempotent) | **PASS** | `runtime/data/ai_cam.db`; migration idempotent (second run adds nothing); existing record preserved. |
| Crop saving | **PASS (path)** / VERIFY-ON-SERVER (live) | `runtime/crops/` + collector wired + unit-tested; live capture needs camera. |
| Dataset collection | **PASS (path)** / VERIFY-ON-SERVER (live) | `runtime/engraved_ocr_collection/`; atomic writes, trusted-label only, `collector_retention_days:0`=unlimited. |
| Restart persistence | **PASS** | DB + crops live under `runtime/`, git-ignored, survive restart; migration never resets. |
| Full regression tests | **PASS** | `pytest tests/` → **377 passed, 4 skipped, 0 failed** (370 existing + 7 new portable tests). |

## Final decision

**READY** for portable one-command operation and source-run on Windows/CPU and
Ubuntu/CPU. The GPU pipeline and live camera/PLC/RFID behavior are **READY pending
on-server verification** — run on the factory Ubuntu host:

```bash
python run.py --self-check --require-gpu   # confirms Torch cu118 + Paddle-GPU + YOLO cuda:0
python run.py                              # then confirm camera/PLC(D2222)/RFID connect
```
