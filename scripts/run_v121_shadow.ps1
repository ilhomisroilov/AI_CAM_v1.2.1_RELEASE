# Default SAFE run: v1.2.1 SHADOW mode. Engraved engine runs as evidence only; the legacy
# Paddle decision remains the production final. Use -SelfCheck to validate without starting.
param([switch]$SelfCheck)
. "$PSScriptRoot\_v121_common.ps1"
Write-Host "v1.2.1 OCR release: SHADOW (safe default). Ensure config/settings.yaml ocr_release.release_mode: SHADOW"
& $Py (Join-Path $Repo "tools\v121_selfcheck.py") --mode SHADOW
if ($LASTEXITCODE -ne 0) { Write-Error "self-check failed; not starting."; exit $LASTEXITCODE }
if ($SelfCheck) { Write-Host "SELF-CHECK OK (SHADOW runnable). Not starting server."; exit 0 }
Write-Host "Starting AI_CAM (SHADOW)..."
Push-Location $Repo
try { & $Py "run.py" } finally { Pop-Location }
