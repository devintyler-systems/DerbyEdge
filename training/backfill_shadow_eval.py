"""
training/backfill_shadow_eval.py

Join output/shadow_log.csv with starter_observations (post-race outcomes) to
produce output/shadow_eval.csv — the input for evaluate_shadow_vs_baseline.

Matching key: race_id + horse (case-insensitive).

Usage
-----
    python -m training.backfill_shadow_eval
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT  = Path(__file__).resolve().parents[1]
_OUTPUT     = _REPO_ROOT / "output"
_SHADOW_LOG = _OUTPUT / "shadow_log.csv"
_SHADOW_EVAL = _OUTPUT / "shadow_eval.csv"

sys.path.insert(0, str(_REPO_ROOT))

from src.utils.db import get_connection
from training.build_training_data import load_observations

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

# Columns emitted to shadow_eval.csv (in order)
_OUT_COLS = [
    "race_id", "horse", "segment",
    "heuristic_win_prob", "ml_win_prob", "served_win_prob",
    "finish_pos", "win_flag", "off_odds",
    "model_version", "serving_mode", "scored_at",
    # context
    "race_date", "track", "race_no", "field_size",
    "distance_furlongs", "surface", "derby_override_flag", "ml_loaded_flag",
]


def run_backfill() -> pd.DataFrame:
    """Join shadow log with race outcomes and write shadow_eval.csv.

    Returns the joined DataFrame (may contain rows without outcomes where the
    race has not yet been run or observations haven't been loaded).
    """
    if not _SHADOW_LOG.exists():
        _log.error("shadow_log.csv not found at %s — score in shadow/live mode first", _SHADOW_LOG)
        return pd.DataFrame()

    shadow = pd.read_csv(_SHADOW_LOG)
    if shadow.empty:
        _log.warning("shadow_log.csv is empty")
        return pd.DataFrame()

    conn = get_connection()
    obs  = load_observations(conn)
    conn.close()

    if obs.empty:
        _log.warning("starter_observations is empty — shadow_eval will have no outcome columns")
        result = shadow.copy()
        for col in ("finish_pos", "win_flag", "off_odds"):
            result[col] = None
    else:
        # Normalize join keys
        shadow = shadow.copy()
        shadow["_key"] = (
            shadow["race_id"].astype(str).str.strip()
            + "|"
            + shadow["horse"].astype(str).str.strip().str.lower()
        )

        outcomes = obs[["race_id", "horse", "finish_pos", "win_flag", "off_odds"]].copy()
        outcomes["finish_pos"] = pd.to_numeric(outcomes["finish_pos"], errors="coerce")
        outcomes["win_flag"]   = pd.to_numeric(outcomes["win_flag"],   errors="coerce")
        outcomes["off_odds"]   = pd.to_numeric(outcomes["off_odds"],   errors="coerce")
        outcomes["_key"] = (
            outcomes["race_id"].astype(str).str.strip()
            + "|"
            + outcomes["horse"].astype(str).str.strip().str.lower()
        )
        outcomes = outcomes.drop_duplicates(subset="_key")
        outcomes = outcomes[["_key", "finish_pos", "win_flag", "off_odds"]]

        result = shadow.merge(outcomes, on="_key", how="left").drop(columns="_key")

    # Select and order output columns (keep any present)
    out_cols = [c for c in _OUT_COLS if c in result.columns]
    result   = result[out_cols].copy()

    _OUTPUT.mkdir(parents=True, exist_ok=True)
    result.to_csv(_SHADOW_EVAL, index=False)

    n_with = int(result["win_flag"].notna().sum()) if "win_flag" in result.columns else 0
    _log.info(
        "backfill complete: %d shadow rows, %d with outcomes -> %s",
        len(result), n_with, _SHADOW_EVAL,
    )
    return result


def main() -> None:
    df = run_backfill()
    if df.empty:
        print(
            "No data produced.\n"
            "  • Run score_race with DERBYEDGE_ML_MODE=shadow or live first.\n"
            "  • Load race outcomes via backfill_observations / results_intake."
        )
        sys.exit(1)

    n_out = int(df["win_flag"].notna().sum()) if "win_flag" in df.columns else 0
    print(f"shadow_eval.csv written: {len(df)} rows, {n_out} with outcomes")
    print(f"  -> {_SHADOW_EVAL}")


if __name__ == "__main__":
    main()
