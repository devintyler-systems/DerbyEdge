<#
.SYNOPSIS
    Run the full DerbyEdge ML shadow evaluation cycle.

.DESCRIPTION
    Sets DERBYEDGE_ML_MODE=shadow and delegates to
    python -m training.run_shadow_cycle, forwarding all arguments.

    This script will NOT run in live mode.  Passing an existing live-mode
    environment variable will cause the Python runner to abort.

.PARAMETER SkipScore
    Skip the scoring step (use existing shadow_log.csv).

.PARAMETER SkipMigration
    Skip the horse_norm migration check.

.PARAMETER AutoMigrate
    Run migrate_horse_norm automatically if the column is missing.

.PARAMETER ReportOnly
    Only regenerate the markdown report from the last eval run.

.PARAMETER CardId
    Card ID to pass to score.py (default: Derby card).

.EXAMPLE
    .\scripts\run_shadow_cycle.ps1

.EXAMPLE
    .\scripts\run_shadow_cycle.ps1 --skip-score

.EXAMPLE
    .\scripts\run_shadow_cycle.ps1 --auto-migrate --card-id 7
#>

param(
    [switch]$SkipScore,
    [switch]$SkipMigration,
    [switch]$AutoMigrate,
    [switch]$ReportOnly,
    [int]$CardId = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Safety: refuse to run if already in live mode
if ($env:DERBYEDGE_ML_MODE -eq "live") {
    Write-Error (
        "DERBYEDGE_ML_MODE=live is set.`n" +
        "This script only runs in shadow mode.`n" +
        "Unset the variable or pass -SkipScore to evaluate without re-scoring."
    )
    exit 1
}

# Force shadow mode for this session
$env:DERBYEDGE_ML_MODE = "shadow"
# Ensure Python outputs UTF-8 so dashes and box chars render correctly
$env:PYTHONUTF8 = "1"

# Build argument list for the Python runner
$pyArgs = @()

if ($SkipScore)     { $pyArgs += "--skip-score" }
if ($SkipMigration) { $pyArgs += "--skip-migration" }
if ($AutoMigrate)   { $pyArgs += "--auto-migrate" }
if ($ReportOnly)    { $pyArgs += "--report-only" }
if ($CardId -gt 0)  { $pyArgs += "--card-id"; $pyArgs += "$CardId" }

python -m training.run_shadow_cycle @pyArgs
exit $LASTEXITCODE
