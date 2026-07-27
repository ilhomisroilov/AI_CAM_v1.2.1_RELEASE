# Rollback

## Application rollback

Keep the previous signed/reviewed ONEDIR deployment intact. Stop AI_CAM
gracefully, restore that directory as a unit, restore its matching environment
configuration, and run its self-check before reconnecting hardware.

Do not mix model files, `_internal` native libraries or Python packages across
release directories.

## Database rollback

The v1.2.1 OCR migration is additive. Before importing or replacing a production
database, take an offline copy. `tools/import_legacy_database.py` never modifies
its source and creates a timestamped backup when `--replace-existing` is
explicitly used.

To roll back data, stop the service, preserve the failed database for forensics,
copy the known-good backup to `runtime/data/ai_cam.db`, then run
`run.py --dry-run` against a separate safe database before normal startup.

## Git rollback

The clean release is a single independent root commit and an annotated tag.
Original repository history is not rewritten. Check out a previously reviewed
release branch/tag in a separate deployment directory; never reset a live
working directory containing unexported runtime data.
