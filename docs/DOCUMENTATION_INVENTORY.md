# Documentation Inventory (v1.2.1 stabilization)

Decision per Markdown file. Physical `ARCHIVE`/`DELETE` moves are a documented
follow-up (not executed here) to avoid breaking in-repo links mid-release; git
history preserves everything regardless.

## Current & canonical — KEEP / UPDATE
| file | decision |
|------|----------|
| `README.md` (root) | UPDATE — v1.2.1 Production Stabilization overview + Mermaid + doc links |
| `docs/ARCHITECTURE.md` | KEEP (new) |
| `docs/OCR_FUSION.md` | KEEP (new) |
| `docs/DATASET_CHARACTER_ALIGNMENT.md` | KEEP (new) |
| `docs/FAILURE_EVIDENCE.md` | KEEP (new) |
| `docs/PRODUCTION_OBSERVATION.md` | KEEP (new) |
| `docs/PLC_AND_CONFIG.md` | KEEP |
| `docs/SIMPLE_RUN_GUIDE.md` | KEEP |
| `docs/SECURITY.md` | KEEP |
| `docs/PRODUCTION_CONFIGURATION.md` | KEEP |
| `docs/OCR_V121_{ARCHITECTURE,DATABASE,COLLECTION_WORKFLOW,DEPLOYMENT,OPERATIONS,ROLLBACK}.md` | KEEP — still accurate for the OCR engine layer |
| `docs/UBUNTU_26_{DEPLOYMENT,GPU,OPERATIONS,NETWORK,BACKUP_RESTORE,TROUBLESHOOTING}.md` | KEEP — Ubuntu host runbooks |
| `docs/RUNTIME_DEPLOYMENT.md`, `docs/ROLLBACK.md`, `docs/VIN_OCR_PIPELINE.md` | KEEP |
| `CHANGELOG.md`, `CHANGELOG.md` | UPDATE / KEEP |
| `reports/RELIABILITY_REFACTOR_REPORT.md`, `reports/TEST_RESULTS_STABILIZATION.md` | KEEP (new) |

## Historical / superseded — ARCHIVE (move to docs/archive/ later)
| file | decision |
|------|----------|
| `docs/AI_CAM_v1.1.3_MVP_RELEASE_PLAN.md`, `docs/OCR_1.1.3_VALIDATION.md` | ARCHIVE — v1.1.3 era |
| `docs/# OCR alternativlarini baholash.md` | ARCHIVE — early evaluation notes |
| `docs/COMPUTER_MIGRATION_BACKUP.md`, `docs/INTEGRATION_STATUS_20260722.md` | ARCHIVE — point-in-time |
| `docs/CAMERA_DECODE_LOG_ANALYSIS.md`, `docs/OCR_TRIGGER_DEBUG_REPORT.md` | ARCHIVE — investigation logs |
| `docs/MERGE_REVIEW_production_hardening_triggering.md` | ARCHIVE — merge review |
| `docs/FINAL_FINDING_CLOSURE_MATRIX.md`, `docs/FINAL_MVP_HARDENING_REPORT.md` | ARCHIVE — MVP closure |
| `docs/PRODUCTION_RECOVERY_REPORT.md`, `docs/PRODUCTION_HARDENING_CHANGES.md` | ARCHIVE |
| `docs/AI_CAM_AUDIT_REPORT.md`, `docs/AI_CAM_PERFORMANCE_AND_RELEASE_PLAN.md`, `docs/HIL_PRODUCTION_VALIDATION_PLAN.md` | ARCHIVE — superseded by ARCHITECTURE + this release |
| `docs/OCR_MIGRATION_REPORT.md`, `docs/PROJECT_INVENTORY.md` | ARCHIVE |
| `reports/V121_*.md`, `reports/UBUNTU_*_CHECKPOINT.md`, `reports/CONTINUATION_CHECKPOINT.md`, `reports/FINAL_RELEASE_CONSOLIDATION_REPORT.md` | ARCHIVE — prior-release artifacts |

## MERGE
- `reports/TEST_RESULTS.md` → MERGE into `reports/TEST_RESULTS_STABILIZATION.md` (keep the v1.2.1 Production Stabilization one canonical).
- `docs/PORTABLE_RUNTIME_REPORT.md` + `reports/FINAL_PORTABLE_VALIDATION.md` → MERGE the portable-runtime story under one report; both KEEP for now.

## DELETE
- None. Nothing is uniquely useful yet redundant enough to hard-delete; git history
  is the backup, so ARCHIVE is preferred over DELETE.
