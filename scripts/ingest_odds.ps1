# DerbyEdge — odds ingest from CSV
# Usage:
#   .\ingest_odds.ps1 -CsvPath .\samples\odds_template.csv
#   .\ingest_odds.ps1 -CsvPath .\my_odds.csv -DbPath .\data\processed\derbyedge.sqlite
param(
    [Parameter(Mandatory=$true)][string]$CsvPath,
    [string]$DbPath = ".\data\processed\derbyedge.sqlite",
    [string]$VenvPath = ".\.venv"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $CsvPath)) {
    Write-Error "CSV not found: $CsvPath"
    exit 1
}

if (-not (Test-Path $DbPath)) {
    Write-Host "DB not found, initializing schema first..." -ForegroundColor Yellow
    & "$VenvPath\Scripts\python.exe" -c @"
import sqlite3
from derbyedge.schema import init_db
from derbyedge.odds_schema import init_odds_schema
conn = sqlite3.connect(r'$DbPath')
init_db(conn); init_odds_schema(conn); conn.close()
print('schema initialized')
"@
}

# Ensure odds schema exists (idempotent)
& "$VenvPath\Scripts\python.exe" -c @"
import sqlite3
from derbyedge.odds_schema import init_odds_schema
conn = sqlite3.connect(r'$DbPath'); init_odds_schema(conn); conn.close()
"@

Write-Host "Ingesting odds from $CsvPath -> $DbPath" -ForegroundColor Cyan
& "$VenvPath\Scripts\python.exe" -m derbyedge.odds_ingest $DbPath $CsvPath

Write-Host "Recomputing odds_features..." -ForegroundColor Cyan
& "$VenvPath\Scripts\python.exe" -c @"
import sqlite3
from derbyedge.odds_features import build_odds_features, write_odds_features
conn = sqlite3.connect(r'$DbPath')
df = build_odds_features(conn)
n = write_odds_features(conn, df)
print(f'wrote {n} odds_feature rows')
conn.close()
"@

Write-Host "Done." -ForegroundColor Green
