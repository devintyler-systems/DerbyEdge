"""
training/win_model_loader.py

Load the latest trained win-probability model artifact for a given segment
and score a DataFrame of pre-race starters.

Used by:
  • training/score_race.py  (CLI inference)
  • src/models/scorer.py    (live scoring with DERBYEDGE_ML_WIN_MODEL=1)

Public API
----------
load_best_model(segment) -> (model, calibrator, feature_cols)
    Returns (None, None, None) when no artifact exists for the segment.
    Falls back to "pooled" when segment-specific artifact is absent.

score_dataframe(df, model, cal, feat_cols) -> pd.DataFrame
    Adds model_win_prob, fair_odds_fmt, model_rank columns in-place.
    Probabilities are race-normalized (sum to 1 per race).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd

from training.build_training_data import (
    NUM_FEATURES, _encode_cat,
    _TAG_MAP, _TIER_MAP, _BUCKET_MAP, _SURFACE_MAP,
)

_log = logging.getLogger(__name__)
_ARTIFACTS = Path(__file__).resolve().parents[1] / "models" / "artifacts"


def _latest_version(segment: str) -> Optional[str]:
    """Return the most recent version tag (YYYYMMDD) for a segment, or None."""
    pattern = f"win_model_{segment}_*.pkl"
    files   = sorted(_ARTIFACTS.glob(pattern))
    if not files:
        return None
    # Filename: win_model_{segment}_{version}.pkl — extract last part.
    return files[-1].stem.split("_")[-1]


def load_best_model(
    segment: str,
) -> tuple[Optional[object], Optional[object], Optional[list]]:
    """Return (model, calibrator, feature_cols) for the latest artifact.

    Tries segment-specific artifact first; falls back to 'pooled'.
    Returns (None, None, None) if nothing is found.
    """
    for seg in (segment, "pooled"):
        version = _latest_version(seg)
        if version is None:
            continue
        try:
            model  = joblib.load(_ARTIFACTS / f"win_model_{seg}_{version}.pkl")
            cal    = joblib.load(_ARTIFACTS / f"calibrator_{seg}_{version}.pkl")
            fcols  = json.loads(
                (_ARTIFACTS / f"feature_columns_{seg}_{version}.json").read_text()
            )
            if seg != segment:
                _log.warning(
                    "load_best_model: no %s artifact — using pooled v%s",
                    segment, version,
                )
            else:
                _log.info("load_best_model: %s v%s", segment, version)
            return model, cal, fcols
        except Exception as exc:
            _log.warning("load_best_model: failed to load %s v%s — %s", seg, version, exc)

    return None, None, None


def _build_inference_df(df: pd.DataFrame, feat_cols: list[str]) -> pd.DataFrame:
    """Align an arbitrary pre-race DataFrame to the training feature columns.

    Missing columns are filled with NaN; extra columns are dropped.
    Categorical encoding matches training (build_training_data.py).
    """
    out = pd.DataFrame(index=df.index)

    # Numeric — coerce, fill missing with NaN (HistGBM handles natively).
    for col in NUM_FEATURES:
        if col in df.columns:
            out[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            out[col] = np.nan

    # ml_odds fraction strings.
    if out["ml_odds"].dtype == object:
        def _parse(v):
            try:
                if isinstance(v, str) and "-" in v:
                    n, d = v.split("-", 1)
                    return float(n) / float(d)
                return float(v)
            except Exception:
                return np.nan
        out["ml_odds"] = out["ml_odds"].apply(_parse)

    # Categorical encodings.
    if "tag_enc" in feat_cols:
        out["tag_enc"] = _encode_cat(
            df.get("tag", pd.Series(["neutral"] * len(df), index=df.index)).astype(str),
            _TAG_MAP, default=-1,
        )
    if "tier_enc" in feat_cols:
        out["tier_enc"] = _encode_cat(
            df.get("tier", pd.Series(["none"] * len(df), index=df.index)).astype(str),
            _TIER_MAP, default=0,
        )
    if "bucket_enc" in feat_cols:
        out["bucket_enc"] = _encode_cat(
            df.get("distance_bucket", pd.Series(["route"] * len(df), index=df.index)).astype(str),
            _BUCKET_MAP, default=-1,
        )
    if "surface_enc" in feat_cols:
        out["surface_enc"] = _encode_cat(
            df.get("surface", pd.Series(["dirt"] * len(df), index=df.index)).astype(str),
            _SURFACE_MAP, default=-1,
        )

    # Return only the columns the model was trained on, in order.
    missing = [c for c in feat_cols if c not in out.columns]
    for c in missing:
        out[c] = np.nan

    return out[feat_cols]


def score_dataframe(
    df: pd.DataFrame,
    model,
    cal,
    feat_cols: list[str],
    race_id_col: Optional[str] = None,
) -> pd.DataFrame:
    """Score starters, calibrate, normalize per race, add output columns.

    Parameters
    ----------
    df
        Pre-race starter rows.  Columns should include at minimum: horse, post,
        ml_odds, field_size, distance_furlongs, pace_fit.
    model
        Fitted classifier from load_best_model.
    cal
        IsotonicRegression calibrator from load_best_model.
    feat_cols
        List of feature column names (must match training).
    race_id_col
        Column identifying races for normalization (default: treat all as one race).
    """
    result = df.copy()
    X_inf  = _build_inference_df(df, feat_cols)

    raw_probs = model.predict_proba(X_inf)[:, 1]
    cal_probs = cal.predict(raw_probs)

    # Race-level normalization.
    normed = cal_probs.copy()
    if race_id_col and race_id_col in df.columns:
        rids = df[race_id_col].to_numpy()
    else:
        rids = np.zeros(len(df), dtype=int)  # all one race

    for rid in np.unique(rids):
        mask = rids == rid
        s    = normed[mask].sum()
        if s > 0:
            normed[mask] /= s

    result["model_win_prob"] = np.round(normed, 6)

    # fair_odds: human-readable "N-1" or "N.N" format.
    fair = np.where(normed > 0, 1.0 / np.maximum(normed, 1e-9) - 1.0, np.nan)
    result["fair_odds_fmt"]  = [
        f"{v:.1f}-1" if np.isfinite(v) else "—" for v in fair
    ]

    # Rank within each race (1 = highest probability).
    ranks = np.zeros(len(df), dtype=int)
    for rid in np.unique(rids):
        mask  = rids == rid
        order = np.argsort(-normed[mask])
        r     = np.empty(order.shape, dtype=int)
        r[order] = np.arange(1, mask.sum() + 1)
        ranks[mask] = r
    result["model_rank"] = ranks

    return result
