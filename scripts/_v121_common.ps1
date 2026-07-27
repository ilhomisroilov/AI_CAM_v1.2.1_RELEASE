# Shared paths for v1.2.1 scripts. Dot-source this from the other scripts.
$Repo = Split-Path $PSScriptRoot -Parent
$Projects = Split-Path $Repo -Parent
$Py = Join-Path $Projects "AI_CAM\.venv\Scripts\python.exe"
$Db = Join-Path $Repo "data\ai_cam.db"
if (-not (Test-Path $Py)) { Write-Error "venv python not found: $Py"; exit 1 }
