# Build a glyph-review queue from PENDING active-learning collection items.
param([string]$Out = "character_dataset\manifests\collection_review_queue.csv")
. "$PSScriptRoot\_v121_common.ps1"
& $Py (Join-Path $Repo "tools\build_collection_review_queue.py") --db $Db --out (Join-Path $Repo $Out)
exit $LASTEXITCODE
