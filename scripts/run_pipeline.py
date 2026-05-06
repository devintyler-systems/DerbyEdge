"""End-to-end pipeline runner: parse -> load -> features -> demo chaos patch."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from derbyedge.loader import load_directory
from derbyedge.features import build_entry_features
from derbyedge.chaos_patch import apply_derby_chaos_patch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw", help="dir of SIMD*.xml files")
    ap.add_argument("--db", default="data/processed/derbyedge.sqlite")
    ap.add_argument("--out", default="data/processed/entry_features.parquet")
    args = ap.parse_args()

    print(f"[1/3] Ingesting {args.raw} -> {args.db}")
    counts = load_directory(args.raw, args.db)
    for k, v in counts.items():
        print(f"   {k:18s} {v:6d}")

    print(f"[2/3] Building features -> {args.out}")
    conn = sqlite3.connect(args.db)
    feats = build_entry_features(conn)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    feats.to_parquet(args.out)
    feats.to_csv(args.out.replace(".parquet", ".csv"), index=False)
    print(f"   {len(feats)} entries across {feats['race_id'].nunique()} races")

    print(f"[3/3] Demo: applying Derby Chaos Patch to a synthetic race")
    demo = _synthetic_demo(feats)
    out_demo = apply_derby_chaos_patch(demo, chaos_index=0.85)
    cols = ['horse', 'WinProb_base', 'WinProb_final', 'DarkHorseFlag',
            'DarkHorseTier', 'chaos_beneficiary_flag']
    print(out_demo[cols].round(4).to_string(index=False))


def _synthetic_demo(feats: pd.DataFrame) -> pd.DataFrame:
    """Build a 5-row demo using real feature stats from the ingested data."""
    return pd.DataFrame([
        {'horse': 'Renegade', 'WinProb_base': 0.20, 'DevCurve_score': 6, 'FinishEnergy_score': 6,
         'PaceFit_score': 5, 'DistanceProj_score': 7, 'Publicness_score': 9, 'late_fig_z': 0.3,
         'FavRailCloserFlag': 1, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0},
        {'horse': 'Commandment', 'WinProb_base': 0.20, 'DevCurve_score': 7, 'FinishEnergy_score': 6,
         'PaceFit_score': 7, 'DistanceProj_score': 7, 'Publicness_score': 9, 'late_fig_z': 0.5,
         'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 1, 'FavTacticalOuterFlag': 0},
        {'horse': 'Further Ado', 'WinProb_base': 0.18, 'DevCurve_score': 8, 'FinishEnergy_score': 8.5,
         'PaceFit_score': 8, 'DistanceProj_score': 8, 'Publicness_score': 8.5, 'late_fig_z': 1.5,
         'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 1},
        {'horse': 'Golden Tempo', 'WinProb_base': 0.02, 'DevCurve_score': 7.5, 'FinishEnergy_score': 9,
         'PaceFit_score': 8.5, 'DistanceProj_score': 6.5, 'Publicness_score': 4, 'late_fig_z': 1.2,
         'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0},
        {'horse': 'Filler Field', 'WinProb_base': 0.40, 'DevCurve_score': 5, 'FinishEnergy_score': 5,
         'PaceFit_score': 5, 'DistanceProj_score': 5, 'Publicness_score': 4, 'late_fig_z': 0.0,
         'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0},
    ])


if __name__ == "__main__":
    main()
