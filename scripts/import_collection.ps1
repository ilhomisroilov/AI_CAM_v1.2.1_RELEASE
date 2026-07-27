# Import human-ACCEPTED reviewed collection items into the dataset (import-once).
param([string]$Known = "character_dataset\manifests\known_manifest.csv")
. "$PSScriptRoot\_v121_common.ps1"
& $Py (Join-Path $Repo "tools\import_reviewed_collection.py") --db $Db --known (Join-Path $Repo $Known)
exit $LASTEXITCODE
