# GUARDED PRIMARY run. REFUSES to start unless -Override is supplied AND all evaluation
# gates pass. Current model has weak-class blockers (5 F1=0; A/H no test evidence), so this
# will refuse by default. Do not make GUARDED the default.
param([switch]$Override)
. "$PSScriptRoot\_v121_common.ps1"
& $Py (Join-Path $Repo "tools\v121_selfcheck.py") --mode GUARDED_PRIMARY
$code = $LASTEXITCODE
if ($code -eq 3) {
    Write-Error "GUARDED_PRIMARY blocked by evaluation gates (weak classes). Refusing."
    exit 3
}
if ($code -ne 0) { Write-Error "engine not ready. Refusing."; exit $code }
if (-not $Override) {
    Write-Error "GUARDED_PRIMARY requires explicit -Override even when gates pass. Refusing."
    exit 4
}
Write-Warning "Starting AI_CAM in GUARDED_PRIMARY (override). Set ocr_release.release_mode: GUARDED_PRIMARY."
Push-Location $Repo
try { & $Py "run.py" } finally { Pop-Location }
