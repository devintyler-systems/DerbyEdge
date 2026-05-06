# DerbyEdge — Windows / PowerShell bootstrap.
# Usage (from the repo root):
#   PowerShell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# Creates a venv at .venv, installs deps, ingests data\raw\*.xml into SQLite,
# builds the feature parquet, and runs the test suite.

param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> Creating virtual environment at .venv"
& $Python -m venv .venv

$venvPython = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "==> Upgrading pip"
& $venvPython -m pip install --upgrade pip wheel | Out-Null

Write-Host "==> Installing requirements"
& $venvPython -m pip install -r requirements.txt

Write-Host "==> Initializing data dirs"
New-Item -ItemType Directory -Force -Path "data\processed" | Out-Null

Write-Host "==> Ingesting Equibase XML files"
$env:PYTHONPATH = "src"
& $venvPython -c @"
from derbyedge.loader import load_directory
counts = load_directory('data/raw', 'data/processed/derbyedge.sqlite')
for k,v in counts.items(): print(f'{k:20s} {v:6d}')
"@

Write-Host "==> Building feature table"
& $venvPython -c @"
import sqlite3
from derbyedge.features import build_entry_features
conn = sqlite3.connect('data/processed/derbyedge.sqlite')
feats = build_entry_features(conn)
feats.to_parquet('data/processed/entry_features.parquet')
feats.to_csv('data/processed/entry_features.csv', index=False)
print(f'Wrote {len(feats)} feature rows -> data/processed/entry_features.parquet')
"@

Write-Host "==> Running tests"
& $venvPython -m pytest tests\ -v

Write-Host ""
Write-Host "DONE. To activate the venv in a new shell:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
