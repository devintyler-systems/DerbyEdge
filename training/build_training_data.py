"""
training/build_training_data.py

Load starter_observations from the DB and build a clean feature / label
DataFrame suitable for ML training.

Leakage policy
--------------
POST-RACE columns (finish_pos, win_flag, off_odds, source_result_file,
created_at) are never included in X.  win_flag is the only label.

Segment mapping
---------------
    dirt_sprint   dirt + distance_furlongs <  8.0
    dirt_route    dirt + distance_furlongs >= 8.0
    turf_sprint   turf + distance_furlongs <  8.0
    turf_route    turf + distance_furlongs >= 8.0
    other         anything else (synthetic, all_weather, unknown)

Public API
----------
get_segment(surface, distance_furlongs) -> str
load_observations(conn) -> pd.DataFrame
build_feature_matrix(df) -> (X: pd.DataFrame, y: pd.Series, meta: pd.DataFrame)
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

# Post-race leakage: never appear in X.
_LEAKAGE_COLS = frozenset({
    "finish_pos", "win_flag", "off_odds",
    "source_result_file", "source_result_file", "created_at",
})

# Identity / metadata: not features, kept for race-aware splits and evaluation.
_META_COLS = [
    "obs_id", "race_id", "race_date", "track", "race_no",
    "horse", "trainer", "jockey", "model_version",
]

# Numeric feature columns.
NUM_FEATURES = [
    "post",
    "ml_odds",
    "pred_win_prob",
    "pred_rank",
    "edge",
    "pace_fit",
    "form_score",
    "sudist_fit",
    "chaos_pct",
    "field_size",
    "distance_furlongs",
]

# Categorical feature columns — label-encoded to integers.
CAT_FEATURES = [
    "tag",              # bet | neutral | underlay | no_data
    "tier",             # chaos tier
    "distance_bucket",  # sprint | route
]

# surface is used for segmentation, not as a feature inside segment models.
# The pooled ("other") fallback model includes surface as a feature.
_POOL_EXTRA_CAT = ["surface"]

ALL_FEATURES = NUM_FEATURES + CAT_FEATURES

# Minimum race-count threshold to warn about small segments.
MIN_RACES_WARN = 20


# ---------------------------------------------------------------------------
# Segment logic
# ---------------------------------------------------------------------------

def get_segment(surface: Optional[str], distance_furlongs: Optional[float]) -> str:
    """Map surface + distance to a training segment.

    Returns one of: dirt_sprint | dirt_route | turf_sprint | turf_route | other
    """
    surf = (surface or "").lower().strip()
    try:
        dist = float(distance_furlongs or 0)
    except (TypeError, ValueError):
        dist = 0.0

    if surf == "dirt":
        return "dirt_sprint" if dist < 8.0 else "dirt_route"
    if surf in ("turf", "grass"):
        return "turf_sprint" if dist < 8.0 else "turf_route"
    return "other"


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_observations(conn: sqlite3.Connection) -> pd.DataFrame:
    """Return starter_observations as a DataFrame.

    Returns an empty DataFrame (with correct columns) when the table is
    empty or does not exist yet — callers must check df.empty.
    """
    try:
        df = pd.read_sql(
            "SELECT * FROM starter_observations ORDER BY race_date, race_id, post",
            conn,
        )
    except Exception as exc:
        _log.warning("load_observations: could not read table — %s", exc)
        return pd.DataFrame()

    if df.empty:
        _log.warning("load_observations: starter_observations is empty — run backfill first")
        return df

    _log.info("load_observations: %d rows, %d races", len(df),
              df["race_id"].nunique() if "race_id" in df.columns else 0)
    return df


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

# Canonical encoding maps for known categoricals.
_TAG_MAP  = {"bet": 2, "neutral": 1, "underlay": 0, "no_data": -1}
_TIER_MAP = {
    "none": 0,
    "dark_horse_candidate": 1,
    "dark_horse_tier_1": 2,
    "dark_horse_tier_2": 3,
    "tier_1": 2,
    "tier_2": 3,
}
_BUCKET_MAP = {"sprint": 0, "route": 1}
_SURFACE_MAP = {"dirt": 0, "turf": 1, "synthetic": 2, "all_weather": 3}


def _encode_cat(series: pd.Series, mapping: dict, default: int = -1) -> pd.Series:
    return series.str.lower().str.strip().map(mapping).fillna(default).astype(float)


def build_feature_matrix(
    df: pd.DataFrame,
    include_surface_feature: bool = False,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Build (X, y, meta) from a starter_observations DataFrame.

    X   : feature matrix (all numeric, NaN where data is missing)
    y   : win_flag series (0 or 1)
    meta: race_id, race_date, horse, etc. for race-aware splits

    Parameters
    ----------
    include_surface_feature
        Set True for the pooled/fallback model to add a 'surface_enc' column.
    """
    df = df.copy()

    # Drop scratched runners — they never won; including them inflates class
    # imbalance artificially and the model doesn't need to score them.
    n_before = len(df)
    df = df[df["scratched"].fillna(0).astype(int) == 0].reset_index(drop=True)
    _log.debug("build_feature_matrix: dropped %d scratched rows", n_before - len(df))

    # Require a valid label.
    df = df[df["win_flag"].notna()].reset_index(drop=True)

    # ---------- numeric features ----------
    for col in NUM_FEATURES:
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # ml_odds: convert "3-1" fraction strings if they slipped through.
    if df["ml_odds"].dtype == object:
        def _parse_odds(v):
            try:
                if isinstance(v, str) and "-" in v:
                    n, d = v.split("-", 1)
                    return float(n) / float(d)
                return float(v)
            except Exception:
                return np.nan
        df["ml_odds"] = df["ml_odds"].apply(_parse_odds)

    # ---------- categorical features ----------
    df["tag_enc"]    = _encode_cat(df["tag"].astype(str),            _TAG_MAP,     default=-1)
    df["tier_enc"]   = _encode_cat(df["tier"].fillna("none").astype(str), _TIER_MAP, default=0)
    df["bucket_enc"] = _encode_cat(df["distance_bucket"].astype(str), _BUCKET_MAP, default=-1)

    feat_cols = NUM_FEATURES + ["tag_enc", "tier_enc", "bucket_enc"]

    if include_surface_feature:
        df["surface_enc"] = _encode_cat(df["surface"].astype(str), _SURFACE_MAP, default=-1)
        feat_cols = feat_cols + ["surface_enc"]

    X    = df[feat_cols].copy()
    y    = df["win_flag"].astype(int)
    meta = df[[c for c in _META_COLS if c in df.columns]].copy()
    meta["segment"]   = df.apply(
        lambda r: get_segment(r.get("surface"), r.get("distance_furlongs")), axis=1
    )
    meta["race_date"] = df["race_date"]

    return X, y, meta


# ---------------------------------------------------------------------------
# Temporal split
# ---------------------------------------------------------------------------

def temporal_split(
    meta: pd.DataFrame,
    train_frac: float = 0.70,
    val_frac:   float = 0.15,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Race-date-aware split — never splits starters from the same race.

    Returns (train_idx, val_idx, test_idx) as integer index arrays into *meta*.
    Sorted by race_date; strictly non-overlapping.

    No random shuffling: earlier dates → train, middle → val, later → test.
    """
    dates = sorted(meta["race_date"].unique())
    n = len(dates)
    if n == 0:
        empty = np.array([], dtype=int)
        return empty, empty, empty

    train_cut_i = max(1, int(n * train_frac))
    val_cut_i   = max(train_cut_i + 1, int(n * (train_frac + val_frac)))
    val_cut_i   = min(val_cut_i, n - 1)

    train_dates = set(dates[:train_cut_i])
    val_dates   = set(dates[train_cut_i:val_cut_i])
    test_dates  = set(dates[val_cut_i:])

    idx = meta.index.to_numpy()
    train_idx = idx[meta["race_date"].isin(train_dates).to_numpy()]
    val_idx   = idx[meta["race_date"].isin(val_dates).to_numpy()]
    test_idx  = idx[meta["race_date"].isin(test_dates).to_numpy()]

    _log.info(
        "temporal_split: %d train races / %d val races / %d test races "
        "| %d / %d / %d starters",
        len(train_dates), len(val_dates), len(test_dates),
        len(train_idx), len(val_idx), len(test_idx),
    )
    return train_idx, val_idx, test_idx
