[CmdletBinding()]
param(
    [string]$Python
)

$ErrorActionPreference = "Stop"
$releaseRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $releaseRoot
$env:YOLO_AUTOINSTALL = "false"
$env:NO_ALBUMENTATIONS_UPDATE = "1"

if (-not $Python) {
    $Python = (& py -3.11 -c "import sys; print(sys.executable)").Trim()
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Supported CPython 3.11 interpreter not found. Install Python 3.11 x64 or pass -Python <path>."
}

$version = (& $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($version -ne "3.11") {
    throw "AI_CAM v1.2.1 release requires CPython 3.11; found $version at $Python."
}

$venvPython = Join-Path $releaseRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "[setup] Creating independent release venv: $releaseRoot\.venv"
    & $Python -m venv (Join-Path $releaseRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed ($LASTEXITCODE)" }
}

Write-Host "[setup] Upgrading packaging tools safely..."
& $venvPython -m pip install --upgrade "pip<27" setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "pip/setuptools/wheel upgrade failed ($LASTEXITCODE)" }

Write-Host "[setup] Installing requirements-lock.txt..."
& $venvPython -m pip install --requirement (Join-Path $releaseRoot "requirements-lock.txt")
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed ($LASTEXITCODE)" }

Write-Host "[setup] Verifying dependency consistency..."
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed ($LASTEXITCODE)" }

& $venvPython (Join-Path $releaseRoot "tools\verify_release_imports.py")
if ($LASTEXITCODE -ne 0) { throw "required import verification failed ($LASTEXITCODE)" }

Write-Host "[setup] Running canonical source self-check..."
& $venvPython run.py --self-check
if ($LASTEXITCODE -ne 0) { throw "run.py --self-check failed ($LASTEXITCODE)" }

Write-Host "[setup] Running hardware-free source dry-run..."
& $venvPython run.py --dry-run
if ($LASTEXITCODE -ne 0) { throw "run.py --dry-run failed ($LASTEXITCODE)" }

Write-Host "[setup] AI_CAM v1.2.1 independent environment is ready."
