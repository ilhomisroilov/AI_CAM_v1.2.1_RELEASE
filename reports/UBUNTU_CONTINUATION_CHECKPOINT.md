# Ubuntu 26.04 Production Deployment Checkpoint

Checkpoint date: 2026-07-28

## Repository state

- Repository: `C:\Users\II4028\Documents\Projects\AI_CAM_v1.2.1_RELEASE`
- Start branch: `release/v1.2.1-clean`
- Deployment branch: `release/v1.2.1-ubuntu-production`
- Start commit: `86e9028a879d757e217506df139e486b065cc34b`
- Start status: clean
- Git layout: independent `.git` directory; not a linked worktree
- Remote: none

The original `AI_CAM` repository and the old worktree remain read-only and
untouched.

## Production configuration comparison

Read-only source: original repository `config/settings.yaml`.

Confirmed non-secret hardware values:

- Camera: `10.123.86.42`, CoLa `2111`, BLOB `2113`
- PLC: enabled `melsec`, `10.123.40.99:5003`, Q-series, D2222, trigger value
  `1`, bit/pulse semantics
- RFID: enabled `r700`, `10.123.18.3:80`, antennas `1,2,3,4`, transmit power
  `3150 cdbm`
- Web: `0.0.0.0:8080`

The original and clean release use the same camera timeout/reconnect settings,
PLC polling and handshake policy, RFID read/retry windows, OCR settings, model
profiles, capture timing, and session deadlines. D2223 is absent.

Credentials were inspected only for presence and are not recorded here. They
must be supplied through `/etc/ai-cam/ai-cam.env` and never committed.

## Model inventory and SHA-256

- YOLO `models/yolo/best.pt`:
  `82498eafe8ea1c631faaa32ebf8a8ae7767040103e8bde90be6241a31faaec30`
- Engraved PT `models/engraved_ocr_v1.2.1/best_model.pt`:
  `9d0bd03743bbb839f0cad7a37f73f2c738b5cc56fb28d3939e5e458d68b2df8f`
- Engraved ONNX `models/engraved_ocr_v1.2.1/model.onnx`:
  `715baac1cac8e4f7e824a6bfc8f2005359d7ebb12596ffd81aa50a0b2d6c3d19`
- Paddle det params/model:
  `83676ec730627ab4502f401410a4b6a3ce1c0bb98fa249b71db055b6bddae051`,
  `c4bfb1b05d9d1d5a760801eaf6d20180ef7e47bcc675fb17d1f3a89da5fef427`
- Paddle rec params/model:
  `75f64a1ffb70c56b7a25655963ca16f5bf3286202e3f52ac972bee05cdee2f56`,
  `85b952f05f709af259cfe4254012aa7208bef0998f71f57a15495446f25ccd43`
- Paddle cls params/model:
  `d1efda1b80e174b4fcb168a035ac96c1af4938892bd86a55f300a6027105d08c`,
  `3c4337ec61722a20b1dca2e5bfaffc313c0592bc89ad6e0d45168224186f6683`

## Work remaining

- Production/example/host-override configuration layering and secret validation
- Linux path, signal, singleton, health, and operational runtime controls
- Python 3.11 Linux dependency profiles and Ubuntu installer
- systemd units, backup/restore, diagnostics, update and service scripts
- Ubuntu documentation, Linux tests, regression verification and deployment
  archive
- Real Ubuntu/systemd/device/live-line validation (external server required)
