# AI_CAM v1.2.1 Test Results

Audit date: 2026-07-27  
Interpreter: independent `.venv`, CPython 3.11.9

## Complete suite

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --durations=10
```

Final post-packaging rerun result: **355 passed, 2 skipped, 0 failed in
104.70 seconds**.

The two skips are the explicitly opt-in tests in
`tests/test_ocr_real_integration.py`. They require
`AI_CAM_RUN_REAL_OCR=1` and a particular positive VIN image assertion. They
remain disabled in the deterministic full suite so it stays independent of
hardware/network/cache state. This is not a model-initialization omission:
`run.py --dry-run` separately initialized the project-local Engraved model and
two independent Paddle process pools, then ran real dummy inference for
ENGRAVED_V121, PADDLE_RAW, and PADDLE_ENHANCED with an empty user cache.

## Additional runtime gates

- `setup.ps1`: passed, including import verification, `pip check`, source
  self-check, and source dry-run.
- Source startup: `/health` returned HTTP 200/status `ok`; CTRL_BREAK produced
  graceful ASGI shutdown and process exit 0.
- Database importer and schema set: 14 passed.
- Auth/settings/health/import set: 26 passed after safe-default validation.
- Deterministic frontend polling harness: 3 passed.

Hardware PLC, camera, and R700 RFID validation was not performed. Their release
defaults are disabled simulators, and production credentials are intentionally
absent.
