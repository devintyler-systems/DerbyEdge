"""
training/feature_audit.py

Compute per-feature null rates from the feature_store table and write
output/feature_null_audit.csv.  Called automatically at the end of
run_shadow_cycle.py.

Usage (standalone)
    python -m training.feature_audit
    python -m training.feature_audit --card-id 7
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO   = Path(__file__).resolve().parents[1]
_OUTPUT = _REPO / "output"

sys.path.insert(0, str(_REPO))

from src.models.trainer import FEATURE_TIERS

# Columns that identify a row but are not features.
_NON_FEATURE_COLS = frozenset({
    "feature_id", "card_id", "entry_id", "horse_id",
    "horse_name", "post_position", "build_ts",
    "run_style_bucket",   # categorical label, not numeric feature
})


def compute_null_audit(card_id: int | None = None) -> pd.DataFrame:
    """
    Load feature_store from DB and compute per-column null rates.

    Returns DataFrame with columns: feature, null_rate, tier
    Sorted by null_rate descending.
    """
    from src.utils.db import get_connection, get_derby_card_id

    conn = get_connection()
    if card_id is None:
        card_id = get_derby_card_id()

    if card_id is None:
        conn.close()
        return pd.DataFrame(columns=["feature", "null_rate", "tier"])

    try:
        feat_df = pd.read_sql(
            "SELECT * FROM feature_store WHERE card_id = ?",
            conn, params=(card_id,),
        )
    except Exception as exc:
        print(f"[feature_audit] Could not read feature_store: {exc}")
        conn.close()
        return pd.DataFrame(columns=["feature", "null_rate", "tier"])
    finally:
        conn.close()

    if feat_df.empty:
        return pd.DataFrame(columns=["feature", "null_rate", "tier"])

    feature_cols = [c for c in feat_df.columns if c not in _NON_FEATURE_COLS]
    n = len(feat_df)

    rows = []
    for col in feature_cols:
        null_rate = round(float(feat_df[col].isna().sum()) / n, 4)
        rows.append({
            "feature":   col,
            "null_rate": null_rate,
            "tier":      FEATURE_TIERS.get(col, "UNKNOWN"),
        })

    return pd.DataFrame(rows).sort_values("null_rate", ascending=False).reset_index(drop=True)


def run_null_audit(card_id: int | None = None) -> Path | None:
    """Compute null rates and write output/feature_null_audit.csv."""
    audit_df = compute_null_audit(card_id)
    if audit_df.empty:
        print("[feature_audit] No feature_store data — skipping null audit")
        return None

    _OUTPUT.mkdir(parents=True, exist_ok=True)
    out_path = _OUTPUT / "feature_null_audit.csv"
    audit_df.to_csv(out_path, index=False)

    n_gap    = int((audit_df["null_rate"] > 0.50).sum())
    n_total  = len(audit_df)
    print(f"[feature_audit] Null audit: {n_total} features, {n_gap} with >50% nulls → {out_path}")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Compute feature null-rate audit")
    ap.add_argument("--card-id", type=int, default=None)
    args = ap.parse_args()
    run_null_audit(args.card_id)


if __name__ == "__main__":
    main()
