[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$releaseRoot = (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $releaseRoot
$env:YOLO_AUTOINSTALL = "false"
$env:NO_ALBUMENTATIONS_UPDATE = "1"
$venvPython = Join-Path $releaseRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Local .venv is missing. Run .\setup.ps1 first."
}

foreach ($relative in @("build", "dist")) {
    $target = [System.IO.Path]::GetFullPath((Join-Path $releaseRoot $relative))
    if (-not $target.StartsWith(
        [System.IO.Path]::GetFullPath($releaseRoot) + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to clean path outside release root: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

Write-Host "[build] Building PyInstaller ONEDIR release..."
& $venvPython -m PyInstaller --noconfirm --clean (Join-Path $releaseRoot "AI_CAM_v1.2.1.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$distRoot = Join-Path $releaseRoot "dist\AI_CAM_v1.2.1"
$exe = Join-Path $distRoot "AI_CAM.exe"
$required = @(
    $exe,
    (Join-Path $distRoot "models\yolo\best.pt"),
    (Join-Path $distRoot "models\engraved_ocr_v1.2.1\model.onnx"),
    (Join-Path $distRoot "models\paddle\det\inference.pdmodel"),
    (Join-Path $distRoot "models\paddle\rec\inference.pdmodel"),
    (Join-Path $distRoot "models\paddle\cls\inference.pdmodel"),
    (Join-Path $distRoot "config\settings.yaml"),
    (Join-Path $distRoot "frontend\templates\dashboard.html"),
    (Join-Path $distRoot "migrations\v1_2_1_ocr_evidence.sql"),
    (Join-Path $distRoot "runtime\data\.gitkeep"),
    (Join-Path $distRoot "runtime\logs\.gitkeep"),
    (Join-Path $distRoot "runtime\crops\.gitkeep"),
    (Join-Path $distRoot "runtime\temp\.gitkeep"),
    (Join-Path $distRoot "runtime\engraved_ocr_collection\.gitkeep")
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Packaged release artifact missing: $path"
    }
}

Write-Host "[build] Executable self-check..."
& $exe --self-check
if ($LASTEXITCODE -ne 0) { throw "Executable --self-check failed ($LASTEXITCODE)" }

Write-Host "[build] Executable dry-run..."
& $exe --dry-run
if ($LASTEXITCODE -ne 0) { throw "Executable --dry-run failed ($LASTEXITCODE)" }

Write-Host "[build] Executable hardware-free startup smoke..."
& $venvPython (Join-Path $releaseRoot "tools\smoke_startup.py") $exe `
    --log (Join-Path $distRoot "runtime\logs\startup-smoke.log")
if ($LASTEXITCODE -ne 0) { throw "Executable startup smoke failed ($LASTEXITCODE)" }

# Verification creates a first-run DB, logs, and empty-cache evidence under
# dist/runtime. Scrub those generated files so the shipped artifact is pristine.
foreach ($relative in @("data", "logs", "crops", "temp", "engraved_ocr_collection")) {
    $runtimeDir = [System.IO.Path]::GetFullPath((Join-Path $distRoot "runtime\$relative"))
    if (-not $runtimeDir.StartsWith(
        [System.IO.Path]::GetFullPath($distRoot) + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to scrub runtime path outside dist root: $runtimeDir"
    }
    Get-ChildItem -LiteralPath $runtimeDir -Force |
        Where-Object { $_.Name -ne ".gitkeep" } |
        Remove-Item -Recurse -Force
}
$unexpectedRuntimeFiles = Get-ChildItem -LiteralPath (Join-Path $distRoot "runtime") -Recurse -File -Force |
    Where-Object { $_.Name -ne ".gitkeep" }
if ($unexpectedRuntimeFiles) {
    throw "Packaged runtime contains generated data after scrub: $($unexpectedRuntimeFiles.FullName -join ', ')"
}

Write-Host "[build] Verified: $exe"
