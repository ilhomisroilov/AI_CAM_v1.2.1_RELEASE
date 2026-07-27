param(
    [Parameter(Mandatory = $true)]
    [string]$DestinationRoot,

    [switch]$IncludeVenv,
    [switch]$IncludeIdea
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath([string]$PathValue) {
    $expanded = [Environment]::ExpandEnvironmentVariables($PathValue)
    return [System.IO.Path]::GetFullPath($expanded)
}

$SourceRoot = Resolve-FullPath (Get-Location).Path
$DestinationRootFull = Resolve-FullPath $DestinationRoot

if (-not (Test-Path $DestinationRootFull)) {
    New-Item -ItemType Directory -Path $DestinationRootFull | Out-Null
}

$DestinationRootFull = (Resolve-Path $DestinationRootFull).Path

if ($DestinationRootFull.StartsWith($SourceRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "DestinationRoot must not be inside the AI_CAM source folder: $DestinationRootFull"
}

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupRoot = Join-Path $DestinationRootFull "AI_CAM_MIGRATION_$Stamp"
New-Item -ItemType Directory -Path $BackupRoot | Out-Null

$Items = @(
    ".git",
    "backend",
    "frontend",
    "config",
    "tools",
    "tests",
    "docs",
    "data",
    "dataset",
    "models",
    "runs",
    "logs",
    "README.md",
    "TRAINING_GUIDE.md",
    "requirements.txt",
    "requirements.freeze.current.txt",
    "backup_manifest.current.json",
    "run.py",
    "split_dataset.py",
    ".gitignore",
    "yolov8n.pt"
)

if ($IncludeVenv) {
    $Items += ".venv"
}

if ($IncludeIdea) {
    $Items += ".idea"
}

$Copied = @()
$Missing = @()

foreach ($item in $Items) {
    $src = Join-Path $SourceRoot $item
    $dst = Join-Path $BackupRoot $item

    if (-not (Test-Path $src)) {
        $Missing += $item
        continue
    }

    $srcItem = Get-Item -Force $src
    if ($srcItem.PSIsContainer) {
        New-Item -ItemType Directory -Path $dst -Force | Out-Null
        $excludeDirs = @("__pycache__", ".pytest_cache")
        $excludeFiles = @("*.pyc", "*.pyo")
        robocopy $src $dst /E /R:2 /W:1 /NFL /NDL /NP /XD $excludeDirs /XF $excludeFiles | Out-Null
        if ($LASTEXITCODE -ge 8) {
            throw "robocopy failed for $item with exit code $LASTEXITCODE"
        }
    } else {
        $parent = Split-Path $dst -Parent
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
    $Copied += $item
}

$Inventory = @()
foreach ($item in $Copied) {
    $p = Join-Path $BackupRoot $item
        $obj = Get-Item -Force $p
    if ($obj.PSIsContainer) {
        $files = @(Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue)
        $sum = ($files | Measure-Object Length -Sum).Sum
        if ($null -eq $sum) { $sum = 0 }
        $Inventory += [ordered]@{
            path = $item
            type = "dir"
            files = $files.Count
            bytes = [int64]$sum
            mb = [math]::Round($sum / 1MB, 2)
        }
    } else {
        $Inventory += [ordered]@{
            path = $item
            type = "file"
            files = 1
            bytes = [int64]$obj.Length
            mb = [math]::Round($obj.Length / 1MB, 2)
        }
    }
}

$KeyFiles = @(
    "data\ai_cam.db",
    "data\ai_cam.migration_backup.db",
    "config\settings.yaml",
    "requirements.txt",
    "requirements.freeze.current.txt",
    "models\vin_slot_recognizer_pilot\best.pt",
    "runs\detect\vin_plate\weights\best.pt",
    "yolov8n.pt"
)

$Hashes = @()
foreach ($file in $KeyFiles) {
    $p = Join-Path $BackupRoot $file
    if (Test-Path $p) {
        try {
            $h = Get-FileHash $p -Algorithm SHA256 -ErrorAction Stop
            $Hashes += [ordered]@{ path = $file; sha256 = $h.Hash }
        } catch {
            $Hashes += [ordered]@{ path = $file; sha256 = $null; error = $_.Exception.Message }
        }
    } else {
        $Hashes += [ordered]@{ path = $file; missing = $true }
    }
}

$Report = [ordered]@{
    created_at = (Get-Date).ToString("o")
    source_root = $SourceRoot
    backup_root = $BackupRoot
    include_venv = [bool]$IncludeVenv
    include_idea = [bool]$IncludeIdea
    copied = $Copied
    missing = $Missing
    inventory = $Inventory
    key_hashes = $Hashes
}

$ReportPath = Join-Path $BackupRoot "migration_backup_report.json"
$Report | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $ReportPath

$ReadmePath = Join-Path $BackupRoot "README_MIGRATION.txt"
@"
AI_CAM migration backup

Created: $((Get-Date).ToString("o"))
Source:  $SourceRoot

Restore:
1. Copy this folder to the new computer.
2. Install Python 3.11.
3. In the restored AI_CAM folder:
   python -m venv .venv
   .\.venv\Scripts\activate
   python -m pip install --upgrade pip
   pip install -r requirements.txt
4. Run:
   python -m py_compile backend\config.py backend\ai\ocr_worker.py backend\ai\vin_fusion.py run.py
   python tests\test_vin_fusion.py
   python run.py

Important:
- Git alone is not enough for this project.
- Runtime data/model folders are included in this backup.
- If data\ai_cam.db was locked on the old PC, use data\ai_cam.migration_backup.db as the safe SQLite backup copy.
"@ | Set-Content -Encoding UTF8 $ReadmePath

Write-Host "Backup created:"
Write-Host $BackupRoot
Write-Host "Report:"
Write-Host $ReportPath
