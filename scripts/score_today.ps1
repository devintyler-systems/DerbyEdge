# DerbyEdge — score today's entries with the trained model and produce edge sheet
# Usage:
#   .\score_today.ps1
#   .\score_today.ps1 -ModelPath .\models\baseline_v0.2.pkl
param(
    [string]$DbPath = ".\data\processed\derbyedge.sqlite",
    [string]$ModelPath = ".\models\baseline_v0.2.pkl",
    [string]$VenvPath = ".\.venv",
    [string]$OutPath = ".\data\processed\edge_sheet.csv"
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path $ModelPath)) {
    Write-Error "Model not found: $ModelPath. Run train_baseline.py first."
    exit 1
}

& "$VenvPath\Scripts\python.exe" -c @"
import sqlite3
from derbyedge.edge_calc import build_edge_table
conn = sqlite3.connect(r'$DbPath')
df = build_edge_table(conn, model_path=r'$ModelPath')
df.to_csv(r'$OutPath', index=False)
print(f'wrote {len(df)} rows to $OutPath')
print()
print('Top edges:')
print(df.sort_values('edge', ascending=False, na_position='last').head(15).to_string(index=False))
print()
print('Bet tag counts:')
print(df['bet_tag'].value_counts().to_string())
conn.close()
"@
