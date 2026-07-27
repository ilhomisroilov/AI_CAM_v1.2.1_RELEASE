FINAL ROOT: C:\Users\II4028\Documents\Projects\AI_CAM_v1.2.1_RELEASE
SOURCE PROVENANCE: release/v1.2.1-ocr-engine at 2e814577430b889feaf7de8509b42a17f228b769; recorded in RELEASE_NOTES_v1.2.1.md and reports/BUILD_PROVENANCE.md.
INDEPENDENT GIT: VERIFIED — .git is a real directory, this repository is not a linked worktree, no remote points to an authoritative backup, and all changes were confined to FINAL ROOT.
CURRENT BRANCH: release/v1.2.1-clean
FINAL CLEAN COMMIT: release/v1.2.1-clean HEAD — one parentless root commit; its self-referential hash must be read with git rev-parse HEAD and is included in the external handoff.
GIT DIRECTORY SIZE: 34.0 MiB after obsolete-ref removal, reflog expiry, and aggressive GC (reduced from 1,300.2 MiB); includes nine required local LFS objects.
GITHUB REMOTE: NONE — no URL was supplied or inherited; no URL was invented.
GITHUB BRANCH: NOT PUSHED — local release/v1.2.1-clean is ready.
GITHUB TAG: NOT CREATED — v1.2.1-shadow must be created only after a valid remote is verified.
GIT LFS: VERIFIED — git-lfs 3.7.1; *.pt, *.onnx, *.pdmodel, *.pdiparams, and *.nb are configured, and all staged model artifacts are LFS pointers.
PYTHON VERSION: CPython 3.11.9, 64-bit Windows.
LOCAL VENV: VERIFIED — independent .venv inside FINAL ROOT; no copy, symlink, or runtime dependency on the original shared environment.
DEPENDENCY INSTALL: PASS — exact .\setup.ps1 completed from requirements-lock.txt, pip check/import verification passed, and no editable or absolute-path requirement exists.
PROTOBUF/PADDLE COMPATIBILITY: PASS — protobuf 3.20.2, paddlepaddle 2.6.2, paddleocr 2.10.0; CPU fallback was exercised because the pinned Paddle build is CPU-only.
YOLO MODEL: PASS — models/yolo/best.pt is local, present, hashed, LFS-managed, and used by project-relative configuration.
ENGRAVED MODEL: PASS — local ONNX, PT, charset, metadata, and output contract validated; dummy inference executed in source and packaged modes.
PADDLE DET MODEL: PASS — models/paddle/det explicit local path; inference.pdmodel and inference.pdiparams present and hashed.
PADDLE REC MODEL: PASS — models/paddle/rec explicit local path; inference.pdmodel, inference.pdiparams, and dictionary present and hashed.
PADDLE CLS MODEL: PASS — models/paddle/cls explicit local path; inference.pdmodel and inference.pdiparams present and hashed.
MODEL HASHES: YOLO best.pt 82498eafe8ea1c631faaa32ebf8a8ae7767040103e8bde90be6241a31faaec30; Engraved ONNX 715baac1cac8e4f7e824a6bfc8f2005359d7ebb12596ffd81aa50a0b2d6c3d19; Engraved PT 9d0bd03743bbb839f0cad7a37f73f2c738b5cc56fb28d3939e5e458d68b2df8f; Paddle det params/model 83676ec730627ab4502f401410a4b6a3ce1c0bb98fa249b71db055b6bddae051 / c4bfb1b05d9d1d5a760801eaf6d20180ef7e47bcc675fb17d1f3a89da5fef427; rec params/model 75f64a1ffb70c56b7a25655963ca16f5bf3286202e3f52ac972bee05cdee2f56 / 85b952f05f709af259cfe4254012aa7208bef0998f71f57a15495446f25ccd43; cls params/model d1efda1b80e174b4fcb168a035ac96c1af4938892bd86a55f300a6027105d08c / 3c4337ec61722a20b1dca2e5bfaffc313c0592bc89ad6e0d45168224186f6683.
PORTABLE PATH AUDIT: PASS — active source/config contains no absolute user checkout, original repository, worktree, .paddleocr-cache, or .cache dependency; every required model/runtime path resolves beneath PROJECT_ROOT.
SOURCE SELF-CHECK: PASS — exact .\.venv\Scripts\python.exe run.py --self-check exited 0 with dependencies, artifacts, contracts, paths, and startup migration state validated.
SOURCE DRY-RUN: PASS — exact .\.venv\Scripts\python.exe run.py --dry-run exited 0 after temporary clean DB creation, two idempotent migration applications, empty-cache isolation, and real ENGRAVED_V121/PADDLE_RAW/PADDLE_ENHANCED inference.
SOURCE STARTUP: PASS — exact Python launcher command was started hardware-free, /health returned HTTP 200/status ok, CTRL_BREAK caused graceful ASGI shutdown, and the process exited 0.
DATABASE FIRST RUN: PASS — a clean database is created under runtime/data, base schema and additive OCR schema are initialized, and no development DB is shipped.
MIGRATION IDEMPOTENCE: PASS — empty, second-run, and legacy-schema tests passed; the second startup migration adds no duplicate columns.
HISTORY LOOP FIX: PASS — one ES5 polling owner, 7-second interval, limit=150, initial fetch, no overlap, AbortController, visibility pause/resume, and unload cleanup; deterministic JavaScript regression tests passed.
DASHBOARD LOOP FIX: PASS — a single 5-second polling owner handles all status widgets, with no reconnect/rerender multiplication and with visibility/unload lifecycle coverage.
OCR RETRY DEDUP: PASS — stable evidence signatures suppress unchanged expensive OCR, allow new crops/material quality improvements, honor deadline budget, and emit STABLE_AMBIGUITY; focused and full-suite tests passed.
PLATE-EXIT FIX: PASS — an ambiguous late result after plate exit cannot reacquire capture or schedule retry, emits OCR_RETRY_IMPOSSIBLE_AFTER_PLATE_EXIT, remains session-owned, and allows terminal completion.
REAL PADDLE RAW: PASS — performs its own local-model Paddle invocation on raw/minimally normalized input and persists exact text, confidence, boxes/payload, latency, status, and error.
REAL PADDLE ENHANCED: PASS — performs a second independent local-model Paddle invocation after documented enhancement and stores evidence separately without overwriting RAW.
THREE-ENGINE ORCHESTRATION: PASS — ENGRAVED_V121, PADDLE_RAW, and PADDLE_ENHANCED start asynchronously from the same session-owned input stage; counters/tests prove two distinct Paddle calls and preserve SHADOW production semantics.
AMBIGUOUS/NO_READ COLLECTION: PASS — accepted, ambiguous, no-read, low-confidence, unknown, and segmentation-reject outcomes reach the non-blocking collector; AMBIGUOUS and NO_READ regression tests passed.
SESSION OWNERSHIP: PASS — late/stale results remain attached to the originating session, one final record is written per session, evidence rows remain separate, and retry ownership is not reopened.
FULL TESTS: PASS — 355 passed, 2 intentionally skipped, 0 failed in 104.70 seconds. Skips require opt-in AI_CAM_RUN_REAL_OCR=1; canonical dry-run independently executed all three real local engines. PLC/camera/R700 hardware tests were not run.
EXE BUILD: PASS — exact .\build_exe.ps1 produced PyInstaller 6.21.0 ONEDIR dist/AI_CAM_v1.2.1 (22,598 files, 1,977,106,367 bytes); only five .gitkeep files remain under packaged runtime after verification scrub.
EXE SELF-CHECK: PASS — exact .\dist\AI_CAM_v1.2.1\AI_CAM.exe --self-check executed and exited 0 against packaged dependencies, contracts, local models, and relative paths.
EXE DRY-RUN: PASS — exact .\dist\AI_CAM_v1.2.1\AI_CAM.exe --dry-run executed and exited 0 with clean temporary DB/migrations, empty Paddle cache, and real packaged three-engine inference; packaged startup also returned /health ok and exited 0.
SECRETS SCAN: PASS — staged credential-pattern candidates were classified as placeholders, examples, or deterministic test values; no API token, private key, production password, or connection secret is included.
RUNTIME DATA SCAN: PASS — no DB, DB sidecar, log, photographed/collected image, RFID export, PPTX, ZIP, cache, development build, or generated runtime payload is included; runtime contains only .gitkeep sentinels.
ROLLBACK: DOCUMENTED — docs/ROLLBACK.md covers artifact/config/database backup, version rollback, migration compatibility, verification, and recovery boundaries.
HARDWARE STATUS: PENDING LIVE SITE VALIDATION — NVIDIA hardware was detected but pinned Torch/Paddle builds correctly used safe CPU fallback; PLC, production cameras, Mitsubishi protocol, and R700 RFID were deliberately disabled and not claimed verified.
GITHUB PUSH: BLOCKED ONLY BY EXTERNAL INPUT — no remote exists and GitHub CLI is not installed/authenticated. Supply a verified repository URL, install/authenticate gh (or equivalent Git credential), then run git remote add origin <URL>; git push -u origin release/v1.2.1-clean; git tag -a v1.2.1-shadow -m "AI_CAM v1.2.1 shadow release"; git push origin v1.2.1-shadow; verify git lfs objects and remote commit.
VERIFIED SOURCE COMMAND: .\setup.ps1; .\.venv\Scripts\python.exe run.py --self-check; .\.venv\Scripts\python.exe run.py --dry-run; controlled .\.venv\Scripts\python.exe run.py startup with /health check and graceful shutdown — all PASS.
VERIFIED EXE COMMAND: .\build_exe.ps1; .\dist\AI_CAM_v1.2.1\AI_CAM.exe --self-check; .\dist\AI_CAM_v1.2.1\AI_CAM.exe --dry-run; controlled packaged startup with /health check and graceful shutdown — all PASS.
FINAL DECISION: CLEAN RELEASE BUILT — GITHUB AUTH/URL REQUIRED
