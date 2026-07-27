# Database migrations

AI_CAM v1.2.1 applies migrations through
`backend.database.ocr_v121_db.migrate()` at canonical startup.

`v1_2_1_ocr_evidence.sql` records the additive table/index schema for audit and
packaging. SQLite column-existence checks for `vin_records.final_ocr_*` are
implemented in Python because SQLite has no portable
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` form. The launcher executes the
migration twice during dry-run and fails unless the second pass is idempotent.

Legacy databases that predate session ownership must first be imported or
migrated explicitly with the tools under `tools/`; they are never silently
rewritten in place.
