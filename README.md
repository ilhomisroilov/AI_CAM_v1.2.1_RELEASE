# AI_CAM v1.2.1

AI_CAM is an industrial VIN capture service with SICK camera, YOLO plate
detection, PaddleOCR, an Engraved v1.2.1 shadow recognizer, PLC/RFID session
ownership, a FastAPI dashboard, SQLite traceability and active-learning evidence
collection.

This release is portable and self-contained. Runtime data stays below `runtime/`;
YOLO, Engraved ONNX and Paddle det/rec/cls models stay below `models/`; no
user-profile Paddle cache or another AI_CAM checkout is required.

## Supported runtime

- 64-bit CPython 3.11
- Windows x64 (verified release target)
- CPU-safe default dependency profile
- Hardware integration requires operator-supplied network settings and secrets

## Setup

```powershell
Set-Location C:\path\to\AI_CAM_v1.2.1_RELEASE
.\setup.ps1
```

`setup.ps1` creates `.venv` inside this repository, installs
`requirements-lock.txt`, runs `pip check`, verifies imports and Paddle-compatible
protobuf, then executes both release gates:

```powershell
.\.venv\Scripts\python.exe run.py --self-check
.\.venv\Scripts\python.exe run.py --dry-run
```

## Run

Source mode:

```powershell
.\.venv\Scripts\python.exe run.py
```

or:

```bat
run.bat
```

Executable mode after `.\build_exe.ps1`:

```powershell
.\dist\AI_CAM_v1.2.1\AI_CAM.exe
```

The tracked settings are safe/offline defaults: PLC and RFID are disabled
simulators, device IPs use documentation-only addresses, and credentials are
placeholders. Supply secrets through the environment variables documented in
`.env.example`. Production mode rejects empty/default authentication passwords.

## Release gates

- `--self-check`: path containment, required artifact/hash/contract checks,
  dependency imports and runtime diagnostics; server is not started.
- `--dry-run`: all self-checks plus a temporary clean database, two idempotent
  v1.2.1 migration passes, empty-cache Paddle isolation, Engraved initialization,
  and independent RAW/ENHANCED Paddle smoke inference. No PLC, camera, RFID or
  permanent server is started.

Startup reports are written below ignored `runtime/logs/`.

## Database

The canonical database is `runtime/data/ai_cam.db`. First start creates the base
schema and applies the additive OCR evidence migration. Development databases are
not included.

Inspect or import an older database without changing the source file:

```powershell
.\.venv\Scripts\python.exe tools\import_legacy_database.py --source D:\backup\ai_cam.db
.\.venv\Scripts\python.exe tools\import_legacy_database.py --source D:\backup\ai_cam.db --yes
```

The default invocation is a read-only dry run.

## OCR v1.2.1 behavior

When a session-owned input crop is ready, the non-blocking shadow stage launches:

1. `ENGRAVED_V121`
2. `PADDLE_RAW`
3. `PADDLE_ENHANCED`

RAW and ENHANCED execute independent real Paddle calls and persist separate raw
text, confidence, native payload/boxes, latency, status and error evidence. In
the default `SHADOW` mode they cannot change production VIN/RFID binding.
AMBIGUOUS and NO_READ evidence still reaches the collector.

Retries require new evidence or a material quality improvement. Unchanged
evidence becomes `STABLE_AMBIGUITY`; insufficient deadline is terminal; capture
is never reacquired after plate exit.

## Build

```powershell
.\build_exe.ps1
```

The build is PyInstaller ONEDIR, not one-file. The script verifies the packaged
models/config/frontend/migrations and executes:

```powershell
.\dist\AI_CAM_v1.2.1\AI_CAM.exe --self-check
.\dist\AI_CAM_v1.2.1\AI_CAM.exe --dry-run
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Hardware tests are intentionally isolated from the offline suite. Passing the
offline suite does not constitute live PLC/camera/RFID production validation.

## Provenance and reports

The release was assembled from source commit `2e81457`. See
`RELEASE_NOTES_v1.2.1.md`, `reports/BUILD_PROVENANCE.md` and
`reports/FINAL_RELEASE_CONSOLIDATION_REPORT.md`.
