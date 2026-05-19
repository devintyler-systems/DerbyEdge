# Run race-level TP segment export then generate markdown summary.
[OutputType([void])]
param()

$ErrorActionPreference = 'Stop'
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

Write-Host "=== Step 1: Export segment / tier CSVs ===" -ForegroundColor Cyan
python -m training.race_eval_by_segment
if ($LASTEXITCODE -ne 0) {
    Write-Error "race_eval_by_segment failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=== Step 2: Generate markdown summary ===" -ForegroundColor Cyan
python -m training.generate_race_eval_summary
if ($LASTEXITCODE -ne 0) {
    Write-Error "generate_race_eval_summary failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=== Outputs ===" -ForegroundColor Green
Write-Host "  output\race_eval_by_segment.csv"
Write-Host "  output\race_eval_by_tier.csv"
Write-Host "  output\race_eval_summary.md"
