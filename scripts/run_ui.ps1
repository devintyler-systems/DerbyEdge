# DerbyEdge UI launcher (Windows PowerShell)
# Run from repo root: .\scripts\run_ui.ps1

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

# Activate venv if present
$Venv = Join-Path $Repo ".venv\Scripts\Activate.ps1"
if (Test-Path $Venv) {
    . $Venv
    Write-Host "Activated venv: $Venv"
}

# Ensure deps
$Need = @("streamlit", "altair", "scikit-learn", "pandas", "pyarrow")
foreach ($pkg in $Need) {
    $check = python -c "import importlib, sys; sys.exit(0 if importlib.util.find_spec('$pkg') else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing $pkg..."
        pip install $pkg | Out-Null
    }
}

# Make sure src is on PYTHONPATH for the package
$env:PYTHONPATH = "$Repo\src;" + $env:PYTHONPATH

# Optional: enable connection priors (off by default)
# $env:DERBYEDGE_USE_CONNECTION_PRIORS = "1"

# Confirm db + model exist
$Db = Join-Path $Repo "data\processed\derbyedge.sqlite"
$Model = Join-Path $Repo "models\baseline_v0.3.pkl"
$ModelFallback = Join-Path $Repo "models\baseline_v0.2.pkl"

if (-not (Test-Path $Db)) {
    Write-Warning "DB missing: $Db"
    Write-Host "Run: python scripts\run_pipeline.py"
    exit 1
}
if (-not (Test-Path $Model) -and -not (Test-Path $ModelFallback)) {
    Write-Warning "No trained model. Run: python scripts\train_baseline.py"
    exit 1
}

Write-Host "Starting DerbyEdge UI on http://localhost:8501 ..." -ForegroundColor Cyan
streamlit run "$Repo\app\streamlit_app.py" `
    --server.port=8501 `
    --browser.gatherUsageStats=false
