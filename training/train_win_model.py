"""
training/train_win_model.py

Train per-segment win-probability models from starter_observations.

Model priority
--------------
    1. LightGBM  (if installed)
    2. XGBoost   (if installed)
    3. sklearn HistGradientBoostingClassifier  (always available)

Calibration
-----------
IsotonicRegression fitted on validation-fold raw probabilities.

Outputs (models/artifacts/)
----------------------------
    win_model_{segment}_{version}.pkl       model object
    calibrator_{segment}_{version}.pkl      IsotonicRegression object
    feature_columns_{segment}_{version}.json  list of feature column names
    metrics_{version}.json                  all segment metrics

Usage
-----
    python -m training.train_win_model
    python -m training.train_win_model --min-races 10   # lower threshold for small datasets
    python -m training.train_win_model --segment dirt_sprint  # single segment
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from datetime import date
from pathlib import Path
from typing import Any, Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_REPO_ROOT   = Path(__file__).resolve().parents[1]
_ARTIFACTS   = _REPO_ROOT / "models" / "artifacts"
_ARTIFACTS.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(_REPO_ROOT))

from src.utils.db import get_connection
from training.build_training_data import (
    ALL_FEATURES, MIN_RACES_WARN, NUM_FEATURES,
    build_feature_matrix, get_segment, load_observations, temporal_split,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

# Segment list in training order.
_SEGMENTS = ["dirt_sprint", "dirt_route", "turf_sprint", "turf_route", "other"]

# Minimum labeled starters (not races) to attempt training a segment model.
# If below this, fall back to the pooled model for that segment.
_MIN_STARTERS_PER_SEGMENT = 30


# ---------------------------------------------------------------------------
# Model factory
# ---------------------------------------------------------------------------

def _make_model(n_estimators: int = 300, max_depth: int = 4, random_state: int = 42):
    """Return the best available GBM classifier."""
    try:
        import lightgbm as lgb
        _log.info("using LightGBM")
        return lgb.LGBMClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            learning_rate=0.05,
            num_leaves=31,
            random_state=random_state,
            n_jobs=-1,
            verbose=-1,
        )
    except ImportError:
        pass

    try:
        import xgboost as xgb
        _log.info("using XGBoost")
        return xgb.XGBClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            learning_rate=0.05,
            random_state=random_state,
            n_jobs=-1,
            eval_metric="logloss",
            verbosity=0,
        )
    except ImportError:
        pass

    from sklearn.ensemble import HistGradientBoostingClassifier
    _log.info("using sklearn HistGradientBoostingClassifier (lightgbm/xgboost not found)")
    return HistGradientBoostingClassifier(
        max_iter=n_estimators,
        max_depth=max_depth,
        learning_rate=0.05,
        random_state=random_state,
        # HistGBM handles NaN natively — no imputation needed.
        early_stopping=False,
    )


# ---------------------------------------------------------------------------
# Per-segment training
# ---------------------------------------------------------------------------

def _race_level_normalize(probs: np.ndarray, race_ids: np.ndarray) -> np.ndarray:
    """Normalize predicted probabilities so they sum to 1 within each race."""
    out = probs.copy()
    for rid in np.unique(race_ids):
        mask = race_ids == rid
        s = out[mask].sum()
        if s > 0:
            out[mask] /= s
    return out


def _top1_hit_rate(probs: np.ndarray, labels: np.ndarray, race_ids: np.ndarray) -> float:
    """Fraction of races where the top-ranked horse actually won."""
    hits = 0
    total = 0
    for rid in np.unique(race_ids):
        mask = race_ids == rid
        if labels[mask].sum() == 0:
            continue
        top_idx = np.argmax(probs[mask])
        if labels[mask][top_idx] == 1:
            hits += 1
        total += 1
    return hits / total if total > 0 else float("nan")


def _winner_in_top3(probs: np.ndarray, labels: np.ndarray, race_ids: np.ndarray) -> float:
    """Fraction of races where the winner was in the top-3 ranked horses."""
    hits = 0
    total = 0
    for rid in np.unique(race_ids):
        mask = race_ids == rid
        if labels[mask].sum() == 0:
            continue
        ranks = np.argsort(-probs[mask])[:3]
        if labels[mask][ranks].sum() > 0:
            hits += 1
        total += 1
    return hits / total if total > 0 else float("nan")


def train_segment(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    meta_train: pd.DataFrame,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    meta_val: pd.DataFrame,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    meta_test: pd.DataFrame,
    segment: str,
    version: str,
    feature_cols: list[str],
) -> dict[str, Any]:
    """Train one segment model + calibrator.  Returns metrics dict."""

    # ── Train ──────────────────────────────────────────────────────────────
    model = _make_model()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(X_train[feature_cols], y_train)

    # ── Raw validation probabilities ───────────────────────────────────────
    val_raw  = model.predict_proba(X_val[feature_cols])[:, 1]
    test_raw = model.predict_proba(X_test[feature_cols])[:, 1]

    # ── Calibration (isotonic on validation) ──────────────────────────────
    cal = IsotonicRegression(out_of_bounds="clip")
    cal.fit(val_raw, y_val.values)

    val_cal  = cal.predict(val_raw)
    test_cal = cal.predict(test_raw)

    # ── Race-level normalize ───────────────────────────────────────────────
    test_cal_norm = _race_level_normalize(
        test_cal, meta_test["race_id"].to_numpy()
    )

    # ── Metrics ───────────────────────────────────────────────────────────
    _safe_ll = lambda p, t: log_loss(t, np.clip(p, 1e-9, 1 - 1e-9)) \
        if len(np.unique(t)) > 1 else float("nan")

    metrics: dict[str, Any] = {
        "segment":          segment,
        "version":          version,
        "n_train":          len(y_train),
        "n_val":            len(y_val),
        "n_test":           len(y_test),
        "n_train_races":    meta_train["race_id"].nunique(),
        "n_val_races":      meta_val["race_id"].nunique(),
        "n_test_races":     meta_test["race_id"].nunique(),
        # Validation (calibration fold)
        "val_log_loss":     round(_safe_ll(val_cal, y_val.values), 5),
        "val_brier":        round(brier_score_loss(y_val, val_cal), 5),
        # Test (held-out)
        "test_log_loss":    round(_safe_ll(test_cal_norm, y_test.values), 5),
        "test_brier":       round(brier_score_loss(y_test, test_cal_norm), 5),
        "test_top1_hit":    round(_top1_hit_rate(
            test_cal_norm, y_test.to_numpy(), meta_test["race_id"].to_numpy()), 4),
        "test_winner_top3": round(_winner_in_top3(
            test_cal_norm, y_test.to_numpy(), meta_test["race_id"].to_numpy()), 4),
    }

    # ── Save artifacts ────────────────────────────────────────────────────
    model_path = _ARTIFACTS / f"win_model_{segment}_{version}.pkl"
    cal_path   = _ARTIFACTS / f"calibrator_{segment}_{version}.pkl"
    feat_path  = _ARTIFACTS / f"feature_columns_{segment}_{version}.json"

    joblib.dump(model, model_path)
    joblib.dump(cal,   cal_path)
    feat_path.write_text(json.dumps(feature_cols, indent=2))

    metrics["model_path"] = str(model_path)
    metrics["cal_path"]   = str(cal_path)

    _log.info(
        "[%s] test log_loss=%.4f  brier=%.4f  top1=%.3f  top3=%.3f",
        segment,
        metrics["test_log_loss"], metrics["test_brier"],
        metrics["test_top1_hit"], metrics["test_winner_top3"],
    )
    return metrics


# ---------------------------------------------------------------------------
# Main training loop
# ---------------------------------------------------------------------------

def run_training(
    target_segment: Optional[str] = None,
    min_races: int = MIN_RACES_WARN,
) -> dict[str, Any]:
    """Load observations, train all (or one) segments, save all artifacts.

    Returns dict of {segment: metrics}.
    """
    version = date.today().strftime("%Y%m%d")
    conn    = get_connection()

    df = load_observations(conn)
    conn.close()

    if df.empty:
        _log.error("No observations found — run backfill_observations first.")
        return {}

    # Build X, y, meta for the full dataset.
    X_all, y_all, meta_all = build_feature_matrix(df)
    meta_all["race_id"] = df.loc[X_all.index, "race_id"].values
    meta_all["race_date"] = df.loc[X_all.index, "race_date"].values

    if len(y_all) == 0:
        _log.error("No non-scratched labeled rows after feature build.")
        return {}

    feature_cols = list(X_all.columns)
    all_metrics: dict[str, Any] = {}

    segments = [target_segment] if target_segment else _SEGMENTS

    for seg in segments:
        _log.info("═══ segment: %s ═══", seg)

        if seg == "other":
            # Pooled fallback: use everything not in a named segment.
            named_segs = set(_SEGMENTS) - {"other"}
            seg_mask = ~meta_all["segment"].isin(named_segs)
        else:
            seg_mask = meta_all["segment"] == seg

        seg_idx = meta_all.index[seg_mask].to_numpy()

        if len(seg_idx) == 0:
            _log.info("[%s] no rows — skipping", seg)
            continue

        # Race count check.
        n_races = meta_all.loc[seg_idx, "race_id"].nunique()
        if n_races < min_races:
            _log.warning(
                "[%s] only %d race(s) (need %d) — falling back to pooled model",
                seg, n_races, min_races,
            )
            # Record the intent but don't train a dedicated segment model.
            all_metrics[seg] = {
                "segment": seg, "status": "fallback_insufficient_data",
                "n_races": n_races, "n_starters": len(seg_idx),
            }
            continue

        X_seg    = X_all.loc[seg_idx]
        y_seg    = y_all.loc[seg_idx]
        meta_seg = meta_all.loc[seg_idx]

        if y_seg.sum() == 0:
            _log.warning("[%s] no winners in segment — skipping", seg)
            continue

        # Temporal split on this segment's races.
        tr_i, va_i, te_i = temporal_split(meta_seg)

        if len(tr_i) < _MIN_STARTERS_PER_SEGMENT:
            _log.warning(
                "[%s] only %d training starters — skipping (need %d)",
                seg, len(tr_i), _MIN_STARTERS_PER_SEGMENT,
            )
            all_metrics[seg] = {
                "segment": seg, "status": "fallback_insufficient_train",
                "n_train_starters": len(tr_i),
            }
            continue

        if len(va_i) == 0 or len(te_i) == 0:
            _log.warning("[%s] val or test split is empty — skipping", seg)
            continue

        metrics = train_segment(
            X_seg.loc[tr_i], y_seg.loc[tr_i], meta_seg.loc[tr_i],
            X_seg.loc[va_i], y_seg.loc[va_i], meta_seg.loc[va_i],
            X_seg.loc[te_i], y_seg.loc[te_i], meta_seg.loc[te_i],
            segment=seg, version=version, feature_cols=feature_cols,
        )
        all_metrics[seg] = metrics

    # ── Pooled fallback model ────────────────────────────────────────────
    # For any segment that fell back, train a pooled model over ALL data
    # (with surface as an extra feature) so inference still works.
    fallback_needed = any(
        v.get("status", "").startswith("fallback") for v in all_metrics.values()
    )
    if fallback_needed or "pooled" not in all_metrics:
        _log.info("═══ segment: pooled (fallback) ═══")
        X_pool, y_pool, meta_pool = build_feature_matrix(df, include_surface_feature=True)
        meta_pool["race_id"] = df.loc[X_pool.index, "race_id"].values
        meta_pool["race_date"] = df.loc[X_pool.index, "race_date"].values
        pool_feat_cols = list(X_pool.columns)

        tr_i, va_i, te_i = temporal_split(meta_pool)
        if len(tr_i) >= _MIN_STARTERS_PER_SEGMENT and len(va_i) > 0 and len(te_i) > 0:
            metrics = train_segment(
                X_pool.loc[tr_i], y_pool.loc[tr_i], meta_pool.loc[tr_i],
                X_pool.loc[va_i], y_pool.loc[va_i], meta_pool.loc[va_i],
                X_pool.loc[te_i], y_pool.loc[te_i], meta_pool.loc[te_i],
                segment="pooled", version=version, feature_cols=pool_feat_cols,
            )
            all_metrics["pooled"] = metrics
        else:
            _log.warning("Pooled model also has insufficient data — no artifacts saved.")

    # ── Write combined metrics ─────────────────────────────────────────────
    metrics_path = _ARTIFACTS / f"metrics_{version}.json"
    metrics_path.write_text(json.dumps(all_metrics, indent=2, default=str))
    _log.info("Metrics written to %s", metrics_path)

    return all_metrics


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Train DerbyEdge win-probability models")
    parser.add_argument("--segment", choices=_SEGMENTS + ["pooled"],
                        help="Train only this segment (default: all)")
    parser.add_argument("--min-races", type=int, default=MIN_RACES_WARN,
                        help=f"Min races before training a segment (default {MIN_RACES_WARN})")
    args = parser.parse_args()

    results = run_training(target_segment=args.segment, min_races=args.min_races)

    print("\n=== Training complete ===")
    for seg, m in results.items():
        status = m.get("status", "trained")
        if status == "trained":
            print(
                f"  {seg:<14}  log_loss={m.get('test_log_loss','—')}  "
                f"brier={m.get('test_brier','—')}  "
                f"top1={m.get('test_top1_hit','—')}  "
                f"top3={m.get('test_winner_top3','—')}"
            )
        else:
            print(f"  {seg:<14}  {status}  (n_races={m.get('n_races', '?')})")


if __name__ == "__main__":
    main()
