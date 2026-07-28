# AI_CAM v1.2.1 Ubuntu 26.04 Production Readiness

BRANCH: `release/v1.2.1-ubuntu-production`

START COMMIT: `86e9028a879d757e217506df139e486b065cc34b`

FINAL COMMIT: The commit containing this report on
`release/v1.2.1-ubuntu-production`; resolve with `git rev-parse HEAD` at
handoff. The exact immutable hash is also written into the post-commit
archive `PROVENANCE.txt` (a Git commit cannot contain its own hash).

WINDOWS TESTS: PASS — 370 passed, 4 skipped (2 Linux-only and 2 opt-in
real-OCR cases); the separate real three-engine dry-run and Windows source
startup/HTTP health/CTRL_BREAK graceful exit passed.

LINUX TESTS: PASS for locally executable/static contracts — environment/path
layering, production YAML, secret redaction, Python 3.14 rejection, overridden
DB creation and WAL, lock contention, device-offline/no-hardware preflight,
systemd contract, shell portability, and Linux lock checks. `bash -n` passed
for all nine scripts. The online WAL backup/integrity/checksum flow passed
inside WSL. The pytest Linux backup and SIGTERM process-group cases were
skipped on Windows and remain Ubuntu-suite acceptance items.

CONFIG SOURCE: Exact working values were read from
`C:\Users\II4028\Documents\Projects\AI_CAM` without modifying it. Tracked base
is `config/settings.yaml`; host override is
`/etc/ai-cam/settings.production.yaml`; environment is
`/etc/ai-cam/ai-cam.env`.

CAMERA CONFIG: Restored `10.123.86.42`, CoLa-A `2111/tcp`, BLOB
`2113/tcp`; original protocol, receive/capture timeout, stale-frame, and
bounded reconnect settings retained. Password is environment-only.

PLC CONFIG: Restored enabled Mitsubishi MC/Q at
`10.123.40.99:5003`, single D2222 bit-0 rising pulse, trigger value 1,
100 ms polling, zero rearm, bounded reconnect, and original full-stop/auto-off
behavior. No active D2223/exit register.

RFID CONFIG: Restored enabled Impinj R700 at `10.123.18.3:80`, antennas
1–4, transmit power 3150, original inventory/retry/warmup windows and decimal
tag rules. Credentials are environment-only.

SECRET STORAGE: Required secrets load from root-owned mode-0640
`/etc/ai-cam/ai-cam.env`. A local ignored deployment env was derived from the
original config without printing values. Real mode fails clearly when
required secrets are absent. No secret is tracked or archived.

PYTHON VERSION: 64-bit CPython 3.11 contract (`.python-version`); installer
locates 3.11 or builds verified Python 3.11.9 without changing
`/usr/bin/python3`; Linux runtime rejects Python 3.14.

LINUX VENV: `/opt/ai-cam/.venv`, created explicitly by Python 3.11.

PADDLE VERSION: CPU `paddlepaddle==2.6.2`; optional NVIDIA profile uses the
official compatible `paddlepaddle-gpu==2.6.1` CUDA 11.8/cuDNN-in wheel.
`paddleocr==2.10.0`; `protobuf==3.20.2`.

PYTORCH VERSION: `torch==2.4.1`, `torchvision==0.19.1`; exact CPU wheels or
official CUDA 12.1 channel according to selected profile.

CPU PROFILE: Default, headless, pinned and wheel-availability checked for
CPython 3.11 Linux amd64. Uses `opencv-python-headless==4.11.0.86`.
Installation and OCR inference passed in the Python 3.11.9 Windows reference
environment; Ubuntu execution remains required.

GPU PROFILE: Installer verifies `nvidia-smi`, driver state, Torch CUDA and
Paddle CUDA computations. Failure restores the CPU engines and exits nonzero.
Not executed on an Ubuntu NVIDIA host; no GPU claim is made.

RUN.PY: Remains the only application launcher. Preserves `--self-check`,
`--dry-run`, normal startup, VIN shadow modes, and Windows frozen behavior;
adds `--migrate`, `--health-check`, `--print-config`, and `--no-hardware`.
Self-check/dry-run/no-hardware perform no industrial-device connection.

SYSTEMD UNIT: `deploy/systemd/ai-cam.service` directly calls
`/opt/ai-cam/.venv/bin/python /opt/ai-cam/run.py`; one worker, network-online
ordering, restart-on-crash, 240-second preload, 90-second stop, SIGTERM,
mixed process-tree cleanup, UMask 0027, NOFILE 65536, and conservative
hardening. Static syntax/contract checked; host verification pending.

SERVICE USER: `aicam` system user/group; service never runs as root.

APPLICATION ROOT: `/opt/ai-cam` (root-owned/readable).

DATA ROOT: `/var/lib/ai-cam` (aicam-owned/writable), including data, crops,
temp, collector evidence, dataset collection, and backups.

LOG ROOT: `/var/log/ai-cam`; systemd defaults to console/journald with optional
bounded file logging.

DATABASE: `/var/lib/ai-cam/data/ai_cam.db`; outside `/opt`, WAL,
10-second busy timeout, foreign keys, transactional unique-session behavior,
quick/integrity checks, and restart recovery.

MIGRATIONS: `run.py --migrate` creates/verifies the base schema and applies
the v1.2.1 OCR migration twice to prove idempotency. Local overridden-root
creation/WAL/integrity passed.

MODEL PATHS: Project-local under `/opt/ai-cam/models`; startup verifies
required model presence, metadata contract, sizes, and SHA-256. No hidden
Paddle cache/model download is accepted by dry-run.

DEVICE PREFLIGHT: Concurrent read-only TCP connect/close probes for camera
control/BLOB, PLC, and RFID. Offline endpoints yield degraded health/logs, not
a permanent startup crash. Public health omits industrial IPs and raw socket
errors; diagnostics contains the exact endpoints.

RECONNECT: Existing camera watchdog/backoff, PLC retry loop, and R700 retry
behavior retained; offline-preflight behavior locally tested. Live
offline/online recovery is pending.

SIGTERM: Uvicorn lifespan stops PLC trigger intake first, then processing and
camera, RFID, shadow OCR/collector, and OCR pools with bounded joins. Windows
graceful process-group smoke passed. Linux process-group test is implemented
and pending Ubuntu execution.

PROCESS POOLS: Spawn-isolated OCR workers retained; shutdown and collector
joins are bounded. Health reports child processes and raises a leak warning
above the configured maximum. Ubuntu orphan-process verification pending.

HEALTH: Public `/health` reports application/version, DB integrity/size,
devices and socket roles, YOLO, Paddle RAW, Paddle Enhanced, Engraved OCR,
runtime CPU/GPU profile, process/collector/session state, frames, reconnect
data, disk thresholds, paths, and config sources without credentials or
industrial IPs. Local HTTP 200 smoke passed.

BACKUP: Consistent SQLite online backup API, source/destination integrity
checks, temporary output plus atomic rename, SHA-256, timestamp, lock, and
configurable retention. Daily persistent systemd timer included. Restore is
checksum/integrity checked, current-DB-backed-up, atomic, migration-verified,
and requires `--yes`.

LOG ROTATION: journald is primary by default. Optional file logs have
`deploy/logrotate/ai-cam` with daily/50 MB rotation, compression, 14 retained
files, and `aicam` permissions.

DISK PROTECTION: Health warning at 10 GiB free and critical/HTTP 503 at
2 GiB by default; critical space rejects a new PLC trigger before capture;
crop count retention is bounded; logs/backups are bounded.
Collector evidence is never silently deleted (`retention_days=0`) and requires
an explicit site policy.

ONE-INSTANCE LOCK: Cross-platform exclusive runtime lock with PID record;
contention test passed.

LINUX ARCHIVE: Builder:
`tools/build_linux_archive.py`. Post-commit output:
`dist-linux/AI_CAM_v1.2.1_ubuntu26_amd64.tar.gz` and
`dist-linux/SHA256SUMS`. It includes tracked source/models/docs/units/scripts,
provenance and an internal SHA-256 manifest; excludes Git, venv, runtime,
Windows build/dist, logs, caches and secrets.

WINDOWS REGRESSION: PASS — full 370-test suite, Windows Python 3.11.9
`run.py --self-check`, real three-engine `run.py --dry-run`, and source server
health/graceful-stop smoke passed. A fresh ONEDIR/PyInstaller build also
passed packaged self-check, real dry-run, HTTP health, graceful-stop, and
child-process cleanup with zero orphans; generated runtime state was scrubbed
from the finished bundle. Windows-specific guarded signal/native-runtime
behavior remains.

UBUNTU SELF-CHECK: NOT RUN — no Ubuntu 26.04 server was available. Command is
installed and documented.

UBUNTU DRY-RUN: NOT RUN — no Ubuntu 26.04 server was available. Command is
installed and documented.

UBUNTU SYSTEMD START: NOT RUN — requires the target server, protected secrets,
and VLAN routes.

REBOOT AUTOSTART: NOT RUN — requires the target server.

LIVE CAMERA: NOT RUN.

LIVE PLC: NOT RUN.

LIVE RFID: NOT RUN.

LIVE SESSION: NOT RUN.

ROLLBACK: Pre-update consistent DB backup plus timestamped code archive under
`/var/lib/ai-cam/backups/releases`; documented stop/extract/self-check/migrate/
restart/health workflow. DB restore is confirmation-gated and returns the
service to its prior state.

FINAL DECISION: UBUNTU DEPLOYMENT BUILT — SERVER VALIDATION REQUIRED
