<#
.SYNOPSIS
    Import a race-level eval CSV into the DerbyEdge race_eval_log table.

.DESCRIPTION
    Validates the file exists then delegates to
    python -m training.import_race_eval_log.

    The importer is idempotent: re-running with the same file updates
    existing rows in place.  Use -ReplaceSource to delete prior rows
    for that source file before importing.

.PARAMETER CsvPath
    Full path to the eval CSV file (required).

.PARAMETER StrictMatch
    Fail (exit 2) if any row cannot be matched to an internal race.

.PARAMETER ReplaceSource
    Delete all existing race_eval_log rows for this source file before
    importing.  Use when re-exporting a corrected version of the same file.

.EXAMPLE
    .\scripts\import_race_eval_log.ps1 -CsvPath "C:\path\38Races_Final_Results_5-12-26.csv" -ReplaceSource

.EXAMPLE
    .\scripts\import_race_eval_log.ps1 -CsvPath ".\2023 Racing Results\38Races_Final_Results_5-12-26.csv"
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$CsvPath,

    [switch]$StrictMatch,
    [switch]$ReplaceSource
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Ensure UTF-8 output so horse names with punctuation render correctly
$env:PYTHONUTF8 = "1"

if (-not (Test-Path $CsvPath)) {
    Write-Error "CSV file not found: $CsvPath"
    exit 1
}

$pyArgs = @("--csv", $CsvPath)
if ($StrictMatch)    { $pyArgs += "--strict-match" }
if ($ReplaceSource)  { $pyArgs += "--replace-source" }

python -m training.import_race_eval_log @pyArgs
exit $LASTEXITCODE
