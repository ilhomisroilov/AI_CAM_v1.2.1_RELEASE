# Rollback the v1.2.1 OCR release. Production master is never modified by the release, so
# rollback is immediate: the release lives only on release/v1.2.1-ocr-engine, and SHADOW
# never alters production decisions. This script reports state and (with -Hard) checks out
# the production baseline in THIS worktree.
param([switch]$Hard)
. "$PSScriptRoot\_v121_common.ps1"
Push-Location $Repo
try {
    Write-Host "current branch: $(git branch --show-current)"
    Write-Host "master (production, untouched): $(git rev-parse --short master)"
    Write-Host "rollback tag: $(git rev-parse --short 'rollback/pre-v1.2.1-ocr-release^{commit}')"
    Write-Host "To disable the shadow engine without any git change: set"
    Write-Host "  config/settings.yaml -> ocr_release.release_mode: DISABLED  (or engraved_enabled: false)"
    if ($Hard) {
        Write-Warning "Hard rollback: checking out production baseline (rollback/pre-v1.2.1-ocr-release)."
        git switch --detach "rollback/pre-v1.2.1-ocr-release"
    }
} finally { Pop-Location }
exit 0
