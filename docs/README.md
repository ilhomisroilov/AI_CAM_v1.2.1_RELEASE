# AI_CAM v1.1.3 documentation index

This directory contains current production guidance, release evidence and historical engineering
reports. The executable release version is maintained only in the repository-level [`VERSION`](../VERSION)
file; documentation must not introduce another version constant.

## Start here

- [`../README.md`](../README.md) — current architecture, setup, operation and acceptance target.
- [`RUNTIME_DEPLOYMENT.md`](RUNTIME_DEPLOYMENT.md) — Windows CPU fallback and Ubuntu/NVIDIA runtime installation and verification.
- [`INTEGRATION_STATUS_20260722.md`](INTEGRATION_STATUS_20260722.md) — current worktree tests, real-crop replay and remaining production blockers.
- [`HIL_PRODUCTION_VALIDATION_PLAN.md`](HIL_PRODUCTION_VALIDATION_PLAN.md) — hardware-in-the-loop release gate.
- [`AI_CAM_v1.1.3_MVP_RELEASE_PLAN.md`](AI_CAM_v1.1.3_MVP_RELEASE_PLAN.md) — v1.1.3 release scope and remaining gates.
- [`PROJECT_INVENTORY.md`](PROJECT_INVENTORY.md) — cleanup decisions, preserved assets and follow-up candidates.
- [`PLC_AND_CONFIG.md`](PLC_AND_CONFIG.md) — PLC and centralized configuration notes.
- [`VIN_OCR_PIPELINE.md`](VIN_OCR_PIPELINE.md) — VIN OCR pipeline reference.
- [`../TRAINING_GUIDE.md`](../TRAINING_GUIDE.md) — detector/model training workflow.

## Audit and verification

- `AI_CAM_AUDIT_REPORT.md`, `FINAL_FINDING_CLOSURE_MATRIX.md`, `FINAL_MVP_HARDENING_REPORT.md`
  and `PRODUCTION_HARDENING_CHANGES.md` document the hardening work.
- `evidence/` contains point-in-time test outputs. Evidence files are immutable snapshots, not a
  statement that the current dirty worktree passes the same suite.
- `workflow/` and `animation/` contain operator/architecture visuals and their editable sources.

## Historical reports

`MERGE_REVIEW_production_hardening_triggering.md`, `OCR_MIGRATION_REPORT.md`,
`OCR_TRIGGER_DEBUG_REPORT.md`, `CAMERA_DECODE_LOG_ANALYSIS.md` and
`AI_CAM_PERFORMANCE_AND_RELEASE_PLAN.md` describe earlier baselines or investigations. References
to v1.1.2 inside explicitly historical comparisons are intentionally preserved.

Generated data, crops, databases, model weights, logs and backup archives are not documentation
and must not be deleted as part of a docs cleanup.
