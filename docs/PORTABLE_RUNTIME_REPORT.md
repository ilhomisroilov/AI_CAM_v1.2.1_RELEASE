# AI_CAM v1.2.1 — Portable One-Command Runtime Report

Branch: `refactor/portable-one-command-runtime`
Goal: make AI_CAM run from **any** folder on Windows or Ubuntu with a single
`python run.py`, with no `/opt`, `/etc`, `/var`, systemd, root or service-user
dependency — while preserving the D2222 session/RFID/capture invariants,
credentials, database, models and crops.

> **Verification scope.** Everything below was built and verified on a Windows /
> CPU dev box. GPU (Torch cu118 / Paddle-GPU) and live hardware (camera
> 10.123.86.42, PLC 10.123.40.99, RFID 10.123.18.3) are on the factory-LAN Ubuntu
> server and could **not** be exercised from here; the exact commands to verify
> them there are given, and no GPU/hardware result is claimed as passed.

---

## 1. Old problems → root causes

| # | Symptom | Root cause |
|---|---------|-----------|
| 1 | `python run.py` aborts: *"real hardware mode requires secrets from /etc/ai-cam/ai-cam.env"* | `release_startup.prepare_normal_startup()` treated a missing env secret as a **fatal** `StartupGateError`. |
| 2 | `python run.py` aborts: *"admin/admin … taqiqlangan"* | Two independent fatal gates on the default password: `config.validate_required_secrets()` **and** `auth.enforce_startup_security_policy()` (raised `InsecureDefaultCredentialsError` in production mode). |
| 3 | Camera / RFID 401, credentials "missing" | `settings.yaml` used `${AI_CAM_*}` refs that only resolved from an `/etc/ai-cam/ai-cam.env` systemd `EnvironmentFile`; on any other host they expanded to empty. |
| 4 | Config confusion / multiple layers | Tracked `settings.yaml` + `/etc/ai-cam/settings.production.yaml` override + `ai-cam.env` + systemd `EnvironmentFile` — four overlapping layers. |
| 5 | `--print-config` / `--self-check` JSON polluted | Config warnings were printed to **stdout** at import. |
| 6 | Browser could serve stale JS after a release | Static assets had no cache-busting version and no revalidation header. |

## 2. Changed architecture (fixes)

**Canonical single-file config.** `config/settings.yaml` is the one source of
truth: hardware IP/ports, server/admin login, GPU/CPU mode, model paths, OCR and
runtime tuning. Environment variables are **optional overrides only** and their
absence never stops startup. Header and comments updated; no `/etc`/systemd
requirement.

**Credential auto-load (portable, secret-safe).** `config.autoload_local_secrets()`
loads a *whitelist* of credential vars (camera/RFID) from the git-ignored
`deploy/local/ai-cam.env` (or `AI_CAM_ENV_FILE`, `config/credentials.local.env`,
`.env`) if present. Path-root variables (`AI_CAM_PROJECT_ROOT/_DATA_ROOT/_LOG_ROOT/
_CONFIG`, `PADDLE_HOME`…) are **deliberately ignored** so runtime paths stay
project-relative. Real environment variables always win. Absent file = no error.
Secrets never enter Git (`deploy/local/` + `*.env` are git-ignored).

**Non-fatal startup.** `prepare_normal_startup()` now emits clear, settings.yaml-
oriented **warnings** for empty/placeholder credentials and continues. The
`/etc/ai-cam/ai-cam.env` requirement is gone. `auth.enforce_startup_security_policy()`
logs an admin/admin warning instead of raising. The app **always starts**; a
device that lacks a credential simply reports *disconnected*.

**admin/admin default.** `auth.username/password = admin/admin` (LAN default,
warning-only). The previously-configured strong password is preserved in
`deploy/local/ai-cam.env` and can be re-enabled with `AI_CAM_ADMIN_PASSWORD`.

**Project-relative paths.** All roots derive from `PROJECT_ROOT =
Path(__file__).resolve().parent[.parent]` (PyInstaller-aware). Runtime data lives
under `runtime/{data,logs,crops,temp,backups,engraved_ocr_collection}`; models
under `models/`. No `/opt`, `/etc`, `/var`, or `C:\Users` in the code defaults.

**Static freshness.** `RevalidatingStaticFiles` adds `Cache-Control: no-cache`
(ETag/Last-Modified already sent ⇒ cheap 304s), and the key assets carry
`?v={app_version}`. A new release can never be served stale JS/CSS.

**Config warnings → stderr**, so `--print-config`/`--self-check` emit clean JSON.

## 3. Frontend — real root cause (P0)

The frontend **was** broken (corrects an earlier mistaken "already correct"
finding — see `reports/P0_FRONTEND_FIX_REPORT.md`). On a **visible** page, neither
the dashboard nor history issued any API request; cards stayed on `—`; three
uncaught **"Illegal invocation"** errors were thrown.

Root cause in `polling.js`: the native `window.setInterval`/`clearInterval` were
stored as instance properties and invoked as `this.setIntervalFn(...)`, i.e. with
`this === PollOwner`. Browsers require these to run with `this === window`, so they
threw inside `_arm()` — which `start()` calls **before** `runNow()` — killing the
initial request and the interval. It was invisible earlier because (a) `_arm()`
bails out when the page is hidden, so the in-app **hidden** browser pane never hit
the line, and (b) the WSH unit harness injects **fake** timers, so the native
binding was never exercised — hence real-browser testing is mandatory.

Fix: bind the native fallbacks to `window`
(`root.setInterval.bind(root)` / `root.clearInterval.bind(root)`); test doubles are
still honoured. `history.js` top-level `addEventListener` wirings were also made
null-safe so template drift can't throw before the poller is created.

Verified with Playwright (headless, `visibilityState=visible`): `/api/status`,
`/api/plc/status`, `/api/rfid/status` fire → 200; cards populate (camera *Offline*
with reason, AI *Ready*); `/api/records` fires → 200 and renders; **0** console and
**0** page errors. `dashboard.js`/`history.js` already use same-origin relative
URLs and the single-owner, visibility-aware poller (pause on hidden, resume + one
immediate run on visible, `AbortController` non-overlap, unload cleanup); PLC
simulator buttons are disabled when `is_simulator` is false. Asset versioning +
`no-cache` (§2) close the remaining stale-JS gap.

## 4. OCR — new engine architecture (GUARDED_PRIMARY)

`ocr_release.release_mode: GUARDED_PRIMARY`. The orchestrator runs the three
engines concurrently and decides with `ENGRAVED_V121` as the **primary candidate**:
accepted as final only when it is complete, high-confidence, all-supported-charset,
no `UNKNOWN_CHAR`, and (with `weak_classes_enabled: false`) free of weak classes —
otherwise a **documented Paddle fallback** (`PADDLE_RAW`/`PADDLE_ENHANCED`
consensus/raw/enhanced/disagree). It never invents a character and never builds a
hybrid string; uncertain positions stay `UNKNOWN_CHAR`. All three raw results plus
the final fused decision, per-character confidence, disagreement, reason and
latency are stored (`ocr_engine_results` + `vin_records.final_ocr_*`). The guarded
decision is recorded as the one session-owned final OCR decision.

*Not changed (deliberately):* the legacy `detected_vin` finalization path is left
intact. Swapping the production VIN to the guarded engraved read must be validated
against live engraved crops first and must not perturb the D2222 one-record /
capture-ownership / RFID-correlation invariants, so it is gated on hardware
validation. Covered by `tests/test_ocr_v121_integration.py` (GUARDED_PRIMARY /
ENGRAVED_ACCEPTED paths pass).

## 5. Crop / dataset collection

Canonical `runtime/crops/` and `runtime/engraved_ocr_collection/`. The
active-learning collector is non-blocking (queue + worker thread), writes images
atomically with metadata (session_id, timestamp, per-char reasons, paddle raw/
enhanced sequences, trusted label), imports only trusted labels, and never renames
unsupported G/V. `operations.collector_retention_days: 0` = unlimited. Paths and
the collector are unit-tested; **live crop-saving** during a real capture needs the
camera/PLC and is validated on the server.

## 6. GPU / runtime

`run.py` detects NVIDIA + CUDA, runs real Torch and Paddle compute probes, selects
`profile=gpu` (YOLO `cuda:0`, OCR GPU) when both pass, else CPU fallback. It scans
only the active `.venv` — no user-home scan.

* **This dev box (verified):** `profile=cpu-fallback`, Torch `2.4.1+cpu`, Paddle
  `2.6.2`, `--self-check` → `ok`, all 7 models validated.
* **GPU server (verify there):** `python run.py --self-check --require-gpu`
  (expects Torch `2.4.1+cu118`, PaddlePaddle-GPU `2.6.x`, YOLO `cuda:0`).

## 7. Hardware

Config pins **D2222 only** (`plc.signal_address: D2222`, `exit_address: ''` —
no D2223 reintroduced). Camera `10.123.86.42` (CoLa 2111 / BLOB 2113), PLC
`10.123.40.99:5003` (Mitsubishi Q), RFID `10.123.18.3:80` (Impinj R700). These are
unreachable from the dev box; services start and report *disconnected*. Real
connection, D2222 trigger, single-session and single-record behavior must be
validated on the factory LAN (no PLC register writes were issued from here).

## 8. Files changed

`backend/config.py` (credential auto-load, advisory secret warnings, warnings→
stderr), `backend/release_startup.py` (non-fatal startup), `backend/auth.py`
(admin/admin warning not fatal), `backend/server.py` (RevalidatingStaticFiles),
`config/settings.yaml` (canonical: admin/admin, GUARDED_PRIMARY, header),
`frontend/templates/{base,dashboard,history}.html` (asset `?v=`),
`requirements-gpu.txt` (new), `tests/{test_ubuntu_deployment,test_auth_security}.py`
(policy alignment), `tests/test_portable_runtime.py` (new regression suite).
