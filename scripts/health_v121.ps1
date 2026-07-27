# Print the v1.2.1 OCR release self-check / health status.
. "$PSScriptRoot\_v121_common.ps1"
& $Py (Join-Path $Repo "tools\v121_selfcheck.py") --mode SHADOW
exit $LASTEXITCODE
