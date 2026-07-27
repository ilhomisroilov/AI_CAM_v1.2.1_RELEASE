# Security and Runtime-Data Audit

Audit date: 2026-07-27  
Scope: final release source candidates and, after orphan staging, the complete
index. Generated `.venv/`, `runtime/`, `build/`, and `dist/` content is ignored.

## Decision

PASS. No API token, private key, production credential, database, RFID export,
collected production image, log, or user-profile runtime dependency is a
release candidate.

## Suspected matches and disposition

- `CHANGE_ME` in `config/settings.yaml` and `backend/config.py`: intentional
  non-secret placeholder. Real-device modes are disabled simulators and
  production startup blocks a placeholder auth password.
- Empty RFID username/password and `.env.example` values: intentional secret
  injection contract; no supplied secret is committed.
- Password/token strings in `tests/`: deterministic dummy values exercising
  auth, masking, protocol, and CSRF behavior. They do not match deployment
  defaults or external systems.
- Credential examples in `docs/SECURITY.md`: labelled examples/placeholders,
  not accepted production values.
- `admin/admin` text in security policy/tests: description of credentials that
  the production blocker rejects, not an active default.
- Official `paddleocr.ai` URL and negative `.paddleocr` comments: documentation
  or forbidden-fallback explanation, not a cache dependency.
- Historical absolute paths: limited to explicitly labelled historical
  migration/provenance reports; see `reports/PORTABLE_PATH_AUDIT.md`.

## Production-image finding remediated

`tests/fixtures/vin_crop_sample.jpg` was inspected and found to contain a
photographed real VIN. It was removed from the release working tree. The opt-in
real-Paddle integration test now renders a deterministic synthetic
`TEST123456789ABCD` sample in memory while preserving the same real process,
inference, ownership, latency, and model-reuse assertions.

The unused historical `tests/fixtures/ocr_ground_truth_20260722.csv` contained
14 production-derived VIN identifiers and crop filenames. It was also removed;
historical validation documents retain aggregate metrics and selected
non-secret technical regression examples, while replay commands use redacted
authorized-dataset placeholders. The repository contains no standalone
production identifier export.

## Runtime-data controls

- `.gitignore` excludes DBs, logs, runtime crops/temp/collector data, caches,
  local environments, and build output.
- PyInstaller includes only `.gitkeep` runtime sentinels, never the source
  runtime directory contents.
- `build_exe.ps1` removes verification-generated data from `dist/runtime` and
  fails if a file other than `.gitkeep` remains.
- The optional old-DB importer is read-only by default, copies through a
  temporary destination, applies migrations there, and never edits its source.

The post-staging review additionally verifies `git diff --cached --name-only`,
credential-pattern candidates, model LFS pointers, and the absence of
database/log/JPEG/PPTX/ZIP files in the index.
