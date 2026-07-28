# Failure Evidence (v1.2.1 stabilization)

Every capture that decodes frames but fails to produce a VIN must leave an image.
**Invariant:** `decoded_frames > 0 AND no final VIN ⇒ evidence image path is not
empty` (enforced by `test_failure_evidence_v13.py`).

## What is retained
- As frames are processed, the **best high-confidence crop** is retained on the
  session (`Session.best_crop`, updated at the fusion buffer point).
- The pipeline always keeps the **last decoded frame** (`self._latest_frame`).

## What is saved, per failure mode
| mode | evidence saved |
|------|----------------|
| `OCR_EMPTY` / `OCR_AMBIGUOUS` / VIN timeout | best retained crop |
| `NO_CROP` (detection but crop rejected) | best retained crop, else last frame |
| `NO_DETECTION` (frames decoded, no detection) | last decoded frame |
| `MISSED_CAPTURE_WINDOW` (no payload) | no image — PLC edge ring-buffer + trigger metadata instead |

On a no-VIN finalize with `frames_decoded > 0`, `_save_failure_evidence(session)`
writes the crop (or frame) to `runtime/crops/NOVIN_<kind>_<session>_<ts>.jpg` and
records the path on the record. Best-effort: it never breaks finalize.

## PLC-suppressed triggers
Suppressed triggers (no payload/session) are audited to
`runtime/audit/suppressed_triggers_*.jsonl` with the trigger sequence, decision
(`DUPLICATE_TRIGGER_IGNORED` / `SUSPICIOUS_EARLY_TRIGGER`), reason and active
session — so external/bounce pulses are visible even without an image.
