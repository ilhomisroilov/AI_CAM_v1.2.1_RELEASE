# AI_CAM v1.2.1 Release Notes

## Provenance

This clean release was consolidated from verified source commit
`2e814577430b889feaf7de8509b42a17f228b769` on
`release/v1.2.1-ocr-engine`.

## Operational decision

Engraved OCR remains `SHADOW` by default. Production final VIN/RFID semantics
remain controlled by the legacy accepted Paddle result. The new Engraved,
Paddle RAW and Paddle ENHANCED outputs are stored as isolated evidence.

## Portability

All model and writable runtime paths resolve below the release root. Paddle is
constructed with explicit bundled det/rec/cls directories; no hidden user cache
is an accepted fallback. Safe settings ship with live devices disabled.

## Known validation boundary

Offline unit/integration, source startup and packaged-executable results are
recorded in the final consolidation report. Live SICK camera, Mitsubishi PLC and
Impinj R700 validation must be performed by the controls/operator team in the
authorized line environment.

## Historical migration context

Older engineering documents may describe previous checkout locations or
user-cache bootstrap procedures. Those references are historical only and are
not active v1.2.1 runtime instructions. Use this repository's `setup.ps1`,
project-local `models/` tree and explicit database import tool instead.
