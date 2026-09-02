"""
training/score_race.py

Score a pre-race starter CSV using the trained win-probability model.
Returns model_win_prob, fair_odds (formatted), and within-race rank.

The CSV must contain at minimum:
    horse, post, ml_odds, pace_fit, form_score, field_size, distance_furlongs
Optional (passed as NaN if absent):
    pred_win_prob, pred_rank, edge, sudist_fit, chaos_pct, tag, tier,
    distance_bucket, surface

Usage
-----
    python -m training.score_race --input path/to/prerace.csv
    python -m training.score_race --input prerace.csv --segment dirt_sprint
    python -m training.score_race --input prerace.csv --output scored.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from training.win_model_loader import load_best_model, score_dataframe


def main() -> None:
    parser = argparse.ArgumentParser(description="Score a pre-race CSV with the ML win model")
    parser.add_argument("--input",   required=True, help="Path to pre-race CSV")
    parser.add_argument("--output",  default=None,  help="Write scored CSV here")
    parser.add_argument("--segment", default=None,  help="Force a specific segment")
    args = parser.parse_args()

    df = pd.read_csv(args.input)
    if df.empty:
        print("Input CSV is empty.")
        sys.exit(1)

    # Infer segment from data if not forced.
    segment = args.segment
    if segment is None:
        surf = str(df.get("surface", pd.Series(["dirt"])).iloc[0]).lower()
        dist = float(df.get("distance_furlongs", pd.Series([8.0])).iloc[0] or 0)
        from training.build_training_data import get_segment
        segment = get_segment(surf, dist)

    model, cal, feat_cols = load_best_model(segment)
    if model is None:
        print(f"No trained model found for segment '{segment}'. Run train_win_model first.")
        sys.exit(1)

    scored = score_dataframe(df, model, cal, feat_cols)

    cols = ["horse", "post", "model_win_prob", "fair_odds_fmt", "model_rank"]
    print(scored[[c for c in cols if c in scored.columns]].to_string(index=False))

    if args.output:
        scored.to_csv(args.output, index=False)
        print(f"\nScored CSV written to {args.output}")


if __name__ == "__main__":
    main()
