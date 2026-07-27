# Changelog

## 1.2.1 — 2026-07-27

- Consolidated a portable, independent release from source commit `2e81457`.
- Moved runtime DB/log/crop/temp/collection data under `runtime/`.
- Bundled YOLO, Engraved v1.2.1 ONNX/PT and Paddle det/rec/cls artifacts.
- Added canonical self-check, dry-run, startup path audit and idempotent DB gates.
- Enforced explicit project-local Paddle model directories with no cache fallback.
- Replaced history and dashboard timer duplication with single polling owners.
- Added stable-evidence OCR retry deduplication and plate-exit terminal handling.
- Moved three-engine shadow orchestration to the session-owned common-input stage.
- Added independent real Paddle RAW/ENHANCED calls and native evidence persistence.
- Added local environment/setup/run scripts and PyInstaller ONEDIR packaging.
- Replaced tracked deployment credentials with safe placeholders/environment input.
