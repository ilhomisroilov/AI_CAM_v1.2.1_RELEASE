# AI_CAM v1.3.0 (rc1)

AI_CAM is an industrial engraved-VIN capture service for **Station 509**: SICK
camera + YOLO plate detection, a three-engine OCR stack (ENGRAVED_V121 primary
candidate, PADDLE_RAW and PADDLE_ENHANCED validators), a Mitsubishi-Q PLC (**D2222**)
+ Impinj R700 RFID body-cycle, a FastAPI dashboard/history, SQLite traceability, and
active-learning dataset collection. One PLC trigger → one body-cycle → **exactly one**
database record.

Portable and self-contained: runtime data under `runtime/`, all models under
`models/`, no user-profile cache. One command: **`python run.py`**. No `/opt`,
`/etc`, `/var`, systemd or service-user dependency.

> **v1.3.0-rc1** adds: D2222 forensic edge audit; the production body-cycle
> invariant (duplicate/early triggers suppressed, no queue); a strict YOLO ≥ 0.90
> OCR gate; F/E·U/V disagreement audit + weak-position guard; VIN-locked retry
> cancel; character-alignment that never ships an equal-width split as training
> data; failure-evidence images; and a real 24h production-observation tool. Final
> `v1.3.0` is gated on that 24h run on the Ubuntu GPU host.

## Documentation
- Architecture + diagrams: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- OCR fusion & three engines: [docs/OCR_FUSION.md](docs/OCR_FUSION.md)
- Dataset character alignment: [docs/DATASET_CHARACTER_ALIGNMENT.md](docs/DATASET_CHARACTER_ALIGNMENT.md)
- Failure evidence: [docs/FAILURE_EVIDENCE.md](docs/FAILURE_EVIDENCE.md)
- 24h observation: [docs/PRODUCTION_OBSERVATION.md](docs/PRODUCTION_OBSERVATION.md)
- PLC & config: [docs/PLC_AND_CONFIG.md](docs/PLC_AND_CONFIG.md) · Run guide: [docs/SIMPLE_RUN_GUIDE.md](docs/SIMPLE_RUN_GUIDE.md)
- Release notes: [docs/releases/v1.3.0-rc1.md](docs/releases/v1.3.0-rc1.md) · Doc inventory: [docs/DOCUMENTATION_INVENTORY.md](docs/DOCUMENTATION_INVENTORY.md)

## Hardware (Station 509)
| device | address | notes |
|--------|---------|-------|
| PLC (Mitsubishi Q) | `10.123.40.99:5003` | trigger **D2222**=1 (~3 s pulse); D2223 not used |
| SICK camera | `10.123.86.42` | CoLa `2111`, BLOB `2113` |
| RFID (Impinj R700) | `10.123.18.3:80` | 30 s parallel scan |
| GPU | NVIDIA (e.g. GTX 1650) | Torch cu118, Paddle-GPU, YOLO `cuda:0` |

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
