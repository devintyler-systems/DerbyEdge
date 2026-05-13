"""
training/backfill_shadow_eval.py

Join output/shadow_log.csv with starter_observations (post-race outcomes) to
produce output/shadow_eval.csv — the input for evaluate_shadow_vs_baseline.

Join strategy
-------------
Primary key:  race_id + post + horse_norm
Fallback key: race_id + horse_norm          (used when post is unavailable)

horse_norm rules: lowercase, trim, collapse whitespace, strip apostrophes /
periods / commas / hyphens / parentheses, replace '&' with 'and'.

Side-car outputs (written alongside shadow_eval.csv)
----------------------------------------------------
  output/join_diagnostics.json    — match rate + sample unmatched keys
  output/unmatched_shadow_rows.csv — rows with no outcome found

Usage
-----
    python -m training.backfill_shadow_eval
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT   = Path(__file__).resolve().parents[1]
_OUTPUT      = _REPO_ROOT / "output"
_SHADOW_LOG  = _OUTPUT / "shadow_log.csv"
_SHADOW_EVAL = _OUTPUT / "shadow_eval.csv"

sys.path.insert(0, str(_REPO_ROOT))

from src.utils.db import get_connection
from src.utils.horse_norm import normalize_horse_name
from training.build_training_data import load_observations

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

# Columns emitted to shadow_eval.csv (in order)
_OUT_COLS = [
    "race_id", "horse", "horse_norm", "segment",
    "heuristic_win_prob", "ml_win_prob", "served_win_prob",
    "finish_pos", "win_flag", "off_odds",
    "match_type",
    "model_version", "serving_mode", "scored_at",
    # context
    "race_date", "track", "race_no", "post", "field_size",
    "distance_furlongs", "surface", "derby_override_flag", "ml_loaded_flag",
]

_UNMATCHED_SAMPLE_SIZE = 10   # how many unmatched keys to include in diagnostics


def _build_norm_keys(df: pd.DataFrame, id_col: str, post_col: str | None) -> pd.DataFrame:
    """Add horse_norm, _key_full, _key_partial columns to a copy of df."""
    df = df.copy()
    if "horse_norm" not in df.columns:
        df["horse_norm"] = df["horse"].apply(normalize_horse_name)

    id_str   = df[id_col].astype(str).str.strip()
    post_str = df[post_col].fillna(-1).astype(int).astype(str) if post_col and post_col in df.columns else pd.Series(["NOPOST"] * len(df), index=df.index)

    df["_key_full"]    = id_str + "|" + post_str + "|" + df["horse_norm"]
    df["_key_partial"] = id_str + "|" + df["horse_norm"]
    return df


def _join_outcomes(
    shadow: pd.DataFrame,
    outcomes: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    """
    Join shadow rows with outcome rows.  Returns (result_df, diagnostics_dict).

    Match order:
      1. race_id + post + horse_norm  (full key)
      2. race_id + horse_norm         (fallback — only for rows unmatched in step 1)
    """
    shadow   = _build_norm_keys(shadow,   "race_id", "post")
    outcomes = _build_norm_keys(outcomes, "race_id", "post")

    # Build lookup indexes from outcomes (deduplicate — keep first)
    out_full = (
        outcomes[["_key_full", "finish_pos", "win_flag", "off_odds"]]
        .drop_duplicates(subset="_key_full")
        .set_index("_key_full")
    )
    out_partial = (
        outcomes[["_key_partial", "finish_pos", "win_flag", "off_odds"]]
        .drop_duplicates(subset="_key_partial")
        .set_index("_key_partial")
    )

    result = shadow.copy()
    # Initialise outcome columns to NaN
    for col in ("finish_pos", "win_flag", "off_odds"):
        result[col] = float("nan")
    result["match_type"] = "none"

    # Step 1 — full key
    full_mask = result["_key_full"].isin(out_full.index)
    if full_mask.any():
        for col in ("finish_pos", "win_flag", "off_odds"):
            result.loc[full_mask, col] = result.loc[full_mask, "_key_full"].map(out_full[col])
        result.loc[full_mask, "match_type"] = "full"

    # Step 2 — partial key fallback (only for still-unmatched rows)
    partial_candidates = result["match_type"] == "none"
    partial_mask = partial_candidates & result["_key_partial"].isin(out_partial.index)
    if partial_mask.any():
        for col in ("finish_pos", "win_flag", "off_odds"):
            result.loc[partial_mask, col] = result.loc[partial_mask, "_key_partial"].map(out_partial[col])
        result.loc[partial_mask, "match_type"] = "partial"

    # Diagnostics
    n_total    = len(result)
    n_matched  = int((result["match_type"] != "none").sum())
    n_full     = int((result["match_type"] == "full").sum())
    n_partial  = int((result["match_type"] == "partial").sum())
    n_unmatched = n_total - n_matched
    match_rate = round(n_matched / n_total, 4) if n_total > 0 else 0.0

    unmatched_keys = (
        result.loc[result["match_type"] == "none", "_key_full"]
        .drop_duplicates()
        .head(_UNMATCHED_SAMPLE_SIZE)
        .tolist()
    )

    diagnostics = {
        "total_shadow_rows":    n_total,
        "matched_rows":         n_matched,
        "matched_full_key":     n_full,
        "matched_partial_key":  n_partial,
        "unmatched_rows":       n_unmatched,
        "match_rate":           match_rate,
        "sample_unmatched_keys": unmatched_keys,
    }

    # Drop internal key columns before returning
    result = result.drop(columns=["_key_full", "_key_partial"])
    return result, diagnostics


def run_backfill() -> tuple[pd.DataFrame, dict]:
    """Join shadow log with race outcomes and write shadow_eval.csv.

    Returns (result_df, diagnostics_dict).
    result_df may contain rows without outcomes where no matching observation
    exists (race not yet run, or key mismatch).
    """
    if not _SHADOW_LOG.exists():
        _log.error(
            "shadow_log.csv not found at %s — score in shadow/live mode first",
            _SHADOW_LOG,
        )
        return pd.DataFrame(), {}

    shadow = pd.read_csv(_SHADOW_LOG)
    if shadow.empty:
        _log.warning("shadow_log.csv is empty")
        return pd.DataFrame(), {}

    conn = get_connection()
    obs  = load_observations(conn)
    conn.close()

    if obs.empty:
        _log.warning("starter_observations is empty — shadow_eval will have no outcome columns")
        result = shadow.copy()
        result["horse_norm"] = result["horse"].apply(normalize_horse_name)
        for col in ("finish_pos", "win_flag", "off_odds"):
            result[col] = None
        result["match_type"] = "none"
        diagnostics = {
            "total_shadow_rows": len(result),
            "matched_rows": 0,
            "matched_full_key": 0,
            "matched_partial_key": 0,
            "unmatched_rows": len(result),
            "match_rate": 0.0,
            "sample_unmatched_keys": [],
        }
    else:
        outcomes = obs[["race_id", "horse", "post", "finish_pos", "win_flag", "off_odds"]].copy()
        for col in ("finish_pos", "win_flag", "off_odds"):
            outcomes[col] = pd.to_numeric(outcomes[col], errors="coerce")

        result, diagnostics = _join_outcomes(shadow, outcomes)

    # Select and order output columns (keep any present)
    out_cols = [c for c in _OUT_COLS if c in result.columns]
    result   = result[out_cols].copy()

    _OUTPUT.mkdir(parents=True, exist_ok=True)
    result.to_csv(_SHADOW_EVAL, index=False)

    # Write side-car files
    diag_path = _OUTPUT / "join_diagnostics.json"
    diag_path.write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")

    unmatched = result[result["match_type"] == "none"] if "match_type" in result.columns else pd.DataFrame()
    unmatched.to_csv(_OUTPUT / "unmatched_shadow_rows.csv", index=False)

    n_with = int(result["win_flag"].notna().sum()) if "win_flag" in result.columns else 0
    _log.info(
        "backfill complete: %d shadow rows, %d matched (%.1f%%), %d with outcomes -> %s",
        diagnostics["total_shadow_rows"],
        diagnostics["matched_rows"],
        diagnostics["match_rate"] * 100,
        n_with,
        _SHADOW_EVAL,
    )
    return result, diagnostics


def main() -> None:
    df, diag = run_backfill()
    if df.empty:
        print(
            "No data produced.\n"
            "  • Run score_race with DERBYEDGE_ML_MODE=shadow or live first.\n"
            "  • Load race outcomes via backfill_observations / results_intake."
        )
        sys.exit(1)

    n_out = int(df["win_flag"].notna().sum()) if "win_flag" in df.columns else 0
    mr    = diag.get("match_rate", 0.0)
    print(f"shadow_eval.csv written: {len(df)} shadow rows, {n_out} with outcomes")
    print(f"  match rate: {mr:.1%}  ({diag.get('matched_full_key',0)} full / "
          f"{diag.get('matched_partial_key',0)} partial / "
          f"{diag.get('unmatched_rows',0)} unmatched)")
    print(f"  -> {_SHADOW_EVAL}")
    print(f"  -> {_OUTPUT / 'join_diagnostics.json'}")
    print(f"  -> {_OUTPUT / 'unmatched_shadow_rows.csv'}")

    if diag.get("match_rate", 1.0) < 0.80:
        print()
        print("WARNING: match rate below 80% — check unmatched_shadow_rows.csv")
        print("  Common causes:")
        print("  • Horse name formatting differs between shadow log and race results")
        print("  • Missing post position in one of the sources")
        print("  • Race results not yet loaded (run backfill_observations)")


if __name__ == "__main__":
    main()
