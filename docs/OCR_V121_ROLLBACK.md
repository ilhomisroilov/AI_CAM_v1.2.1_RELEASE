# OCR v1.2.1 Rollback

Rollback is immediate and low-risk because the release is **additive + SHADOW**: production
`master` (b92324f) is untouched and SHADOW never changes the production OCR decision.

## Instant, no-git rollback (recommended)
Set in `config/settings.yaml`:
```yaml
ocr_release:
  release_mode: DISABLED     # or: engraved_enabled: false
```
Nothing new is computed or written; the legacy PaddleOCR path is unchanged. No restart of
the legacy pipeline logic is needed beyond a normal app restart.

## Git rollback
```powershell
pwsh -File scripts\rollback_v121.ps1            # report state (master untouched)
pwsh -File scripts\rollback_v121.ps1 -Hard      # detach this worktree to the pre-release baseline
git switch master                               # production baseline (b92324f)
```
Tags: `rollback/pre-v1.2.1-shadow-integration` → 188622a ;
`rollback/pre-v1.2.1-ocr-release` → 3ea037c.

## Database
The migration is additive-only (2 new tables + 7 additive columns). No existing row is
modified. To fully revert the schema, drop the two tables and the additive columns; existing
`vin_records` data is unaffected. No data restore is required.

## Verification
`scripts/rollback_v121.ps1` confirms `master` remains b92324f and reports the config-disable
path. Session ownership / capture-slot / MISSED_CAPTURE_WINDOW / stale-RFID behavior are
unchanged by the release (341-test suite green).
