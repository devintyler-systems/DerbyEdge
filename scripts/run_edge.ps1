# DerbyEdge — produce the edge sheet
# Outputs CSV at data/processed/edge_sheet.csv
# Without a trained model, this uses the equal-mass placeholder model — useful
# for plumbing/diff but NOT for betting. Plug in your own model_probs CSV
# (columns: entry_id, model_prob) via -ModelProbsCsv.
param(
    [string]$DbPath = ".\data\processed\derbyedge.sqlite",
    [string]$VenvPath = ".\.venv",
    [string]$ModelProbsCsv = "",
    [string]$OutPath = ".\data\processed\edge_sheet.csv"
)

$ErrorActionPreference = "Stop"

$modelArg = if ($ModelProbsCsv -ne "") { "r'$ModelProbsCsv'" } else { "None" }

& "$VenvPath\Scripts\python.exe" -c @"
import sqlite3, pandas as pd
from derbyedge.edge_calc import build_edge_table

conn = sqlite3.connect(r'$DbPath')
mp = $modelArg
if mp is not None:
    mp = pd.read_csv(mp)
df = build_edge_table(conn, model_probs=mp)
df.to_csv(r'$OutPath', index=False)
print(f'wrote {len(df)} rows to $OutPath')
print(df.sort_values('edge', ascending=False).head(10).to_string(index=False))
conn.close()
"@
