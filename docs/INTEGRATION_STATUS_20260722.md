# AI_CAM v1.1.3 integration status — 2026-07-22

> Historical record. Production-derived crops and their VIN manifest are not
> distributed with the portable v1.2.1 release.

This is current worktree evidence, not a production sign-off.

## Verified changes

- `VERSION=1.1.3` is the backend/frontend release source of truth.
- Windows CPU fallback passes a real Torch and Paddle tensor self-check.
- Ubuntu/NVIDIA deployment fails closed unless both Torch and Paddle use CUDA.
- OCR supports current BL7M `ABTJ`/`ABUJ` structures and rejects trusted
  raw/CLAHE full-VIN conflicts.
- Production OCR requires redundant evidence (`accept_min_crops=2`,
  `accept_single_read=false`). A single high-confidence OCR read is not enough.
- In-flight OCR has a bounded 15 second grace after the normal session deadline.
- The unvalidated fixed-slot recognizer is disabled in production settings.
- PLC/RFID compatibility contracts were restored without changing the current
  D2222 pulse/latch behavior.

## Test evidence

- Syntax/compile and `git diff --check`: pass.
- Full pytest suite after the Linux CUDA/NVRTC loader fix: `262 passed,
  2 skipped` in 86.80 seconds.
- The two skips are explicitly gated real-Paddle hardware integration tests;
  run them with `AI_CAM_RUN_REAL_OCR=1` on a prepared OCR host.
- Session lifecycle, PLC pulse/handshake, camera reconnect/watchdog, frame
  ownership, OCR process supervision, database migration/idempotency, auth,
  settings secret masking, health, retention and version tests all pass.
- Windows `run.py --self-check --deep`: exit `0`, profile `cpu-fallback`.
- Windows `run.py --self-check --deep --require-gpu`: exit `2`, as required,
  because installed Torch/Paddle wheels are CPU-only.
- Local `data/ai_cam.db` migration is idempotent: all 134 existing rows are
  preserved, every row has a unique non-empty `session_id`, and a recoverable
  pre-migration backup exists.

The software integration suite is green. Ubuntu/NVIDIA deep runtime validation
and production-line HIL remain release gates; a unit/integration pass alone is
not a production accuracy sign-off.

## Real crop replay

Ground truth came from a 14-row, manually read production-crop manifest that
was intentionally excluded from the portable release. Full single-saved-crop
CPU replay after the final safety fix:

- exact accepted: `7/14` (`50.0%`);
- false accept: `0/14`;
- `OCR_AMBIGUOUS`: `5/14`;
- `NO_READ`: `2/14`.

The replay exposed a real unsafe case: ground-truth BL7M `...ABUJ...` was read
once as the structurally legal future code `...ABVJ...`. Disabling production
single-read acceptance changed this from false `ACCEPT` to safe
`OCR_AMBIGUOUS`. Evidence CSV: `data/ocr_eval_20260722.csv`.

This is a safety improvement, but 50% single-crop yield is not the 99% release
target. Live production normally supplies multiple top-k frames, which were not
archived for these events, so a live-event replay requires new capture evidence.

## Camera/lighting finding

Today’s QY crops were sharp (Laplacian variance roughly 1,400–1,700), including
a `NO_READ` crop sharper than a successful crop. The primary fault is therefore
not focus. QY images do have about 16–25% highlight clipping from glare.

Recommended HIL changes, one at a time with before/after metrics:

1. lock exposure/gain/white balance; reduce exposure until highlight clipping
   over the VIN ROI is below 5% without losing the dark engraving;
2. use diffuse off-axis illumination; for persistent metal glare, test crossed
   polarizers on light and lens;
3. keep the entire engraved row plus margin inside the ROI;
4. archive original top-k frames and camera exposure metadata for every event.

## 99% release gate

Collect at least 300 consecutive, manually verified production events covering
QY variants and BL7M, glare and movement. Release only if exact accepted VIN is
at least 99%, false accept is zero, p95 latency fits the session budget on the
deployed Ubuntu/NVIDIA host, and the complete reconciled state-machine/HIL suite
passes. A MES/batch expected-VIN list is strongly recommended for a second,
independent validation source; OCR structure alone cannot safely distinguish
every legal product/year character.
