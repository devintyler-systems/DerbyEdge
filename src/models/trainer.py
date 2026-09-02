"""
DerbyEdge V1  —  Model Trainer
src/models/trainer.py

Training paths:
  1. SEED-ONLY BASELINE (current state)
     horse_starts is empty -> training_rows < MIN_TRAINING_ROWS
     -> build a principled weighted composite over the 46-feature catalog
     -> calibrate spread via temperature-scaled softmax
     -> labeled "seed_only_baseline" in model_registry

  2. XGBOOST FAMILIES (future, once historical races are loaded)
     When training_rows >= MIN_TRAINING_ROWS for a given race_type_key,
     this module trains an XGBoost ranker with rolling time-based
     validation and isotonic post-hoc calibration.

Race-type families:   dirt_route | dirt_sprint | turf_route | turf_sprint
Derby uses: dirt_route
"""

import dataclasses
import datetime
import pickle
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT       = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "saved_models"

MIN_TRAINING_ROWS = 50

# ---------------------------------------------------------------------------
# Feature tier catalog — used for importance reports and null-rate audits.
# ---------------------------------------------------------------------------
FEATURE_TIERS: dict[str, str] = {
    # T1 — high-signal features added 2026-05
    "speed_fig_adj":               "T1",
    "layoff_bucket_encoded":       "T1",
    "class_delta_v2":              "T1",
    "horses_beaten_pct_actual":    "T1",
    "pace_pressure_tier":          "T1",
    "collapse_risk_v2":            "T1",
    "morning_line_delta":          "T1",
    # T2 — core implemented features
    "speed_best_3":                "T2",
    "speed_last":                  "T2",
    "speed_best":                  "T2",
    "beyer_last":                  "T2",
    "form_cycle_idx":              "T2",
    "class_delta":                 "T2",
    "horses_beaten_pct_last":      "T2",
    "career_win_pct":              "T2",
    "career_itm_pct":              "T2",
    "distance_fit":                "T2",
    "surface_fit":                 "T2",
    "pace_fit_score":              "T2",
    "traffic_resilience_proxy":    "T2",
    "work_readiness_score":        "T2",
    "trainer_intent_proxy":        "T2",
    "finish_energy_proxy":         "T2",
    "derby_override_score":        "T2",
    "market_implied_prob":         "T2",
    # T3 — secondary / supporting implemented features
    "speed_avg":                   "T3",
    "layoff_days":                 "T3",
    "morning_line_rank":           "T3",
    "lone_speed_edge":             "T3",
    "pace_pressure":               "T3",
    "collapse_risk":               "T3",
    "early_intent":                "T3",
    # DEGRADED — proxy constructions
    "field_size_exp":              "DEGRADED",
    "pedigree_route_proxy":        "DEGRADED",
    "publicness_score":            "DEGRADED",
    "public_underlay_penalty":     "DEGRADED",
    "route_progression":           "DEGRADED",
    "classic_distance_projection": "DEGRADED",
    "class_level":                 "DEGRADED",
    "gate_reliability":            "DEGRADED",
    "works_30d":                   "DEGRADED",
    # PLACEHOLDER — null for seed-only installs
    "pace_early_mean_3":           "PLACEHOLDER",
    "pace_mid_mean_3":             "PLACEHOLDER",
    "bullet_30d":                  "PLACEHOLDER",
    "days_since_last_work":        "PLACEHOLDER",
    "post_win_bias":               "PLACEHOLDER",
    "trouble_recovery_proxy":      "PLACEHOLDER",
    "field_strength_last":         "PLACEHOLDER",
    "trainer_jockey_itm_cond":     "PLACEHOLDER",
    "jockey_route_cond":           "PLACEHOLDER",
    "trainer_derby_cond":          "PLACEHOLDER",
    "churchill_readiness":         "PLACEHOLDER",
    "jan_apr_improvement_curve":   "PLACEHOLDER",
}

# ---------------------------------------------------------------------------
# Feature group definitions — keyed by race_type_key.
# Each group has a top-level weight (must sum to 1.0 across groups) and
# per-feature weights (must sum to 1.0 within the group).
#
# Features here are all IMPLEMENTED or DEGRADED; PLACEHOLDER features
# (pace_early_mean_3, bullet_30d, trainer_jockey_itm_cond, etc.) are
# deliberately excluded until source tables are populated.
# ---------------------------------------------------------------------------
FEATURE_GROUPS: dict[str, dict] = {
    "dirt_route": {
        "speed_quality": {
            "group_weight": 0.25,
            "features": {"speed_best_3": 0.40, "speed_last": 0.35, "beyer_last": 0.25},
        },
        "form_class": {
            "group_weight": 0.20,
            "features": {
                "form_cycle_idx":         0.35,
                "class_delta":            0.30,
                "horses_beaten_pct_last": 0.20,
                "career_win_pct":         0.15,
            },
        },
        "distance_surface": {
            "group_weight": 0.20,
            "features": {"distance_fit": 0.55, "surface_fit": 0.45},
        },
        "race_shape": {
            "group_weight": 0.17,
            "features": {
                "pace_fit_score":          0.65,
                "traffic_resilience_proxy": 0.35,
            },
        },
        "readiness": {
            "group_weight": 0.13,
            "features": {
                "work_readiness_score":  0.50,
                "trainer_intent_proxy":  0.30,
                "finish_energy_proxy":   0.20,
            },
        },
        "market_prior": {
            "group_weight": 0.05,
            "features": {"market_implied_prob": 1.00},
        },
    },
    "dirt_sprint": {
        "speed_quality": {
            "group_weight": 0.35,
            "features": {"speed_best_3": 0.40, "speed_last": 0.35, "beyer_last": 0.25},
        },
        "form_class": {
            "group_weight": 0.18,
            "features": {
                "form_cycle_idx":         0.35,
                "class_delta":            0.30,
                "horses_beaten_pct_last": 0.20,
                "career_win_pct":         0.15,
            },
        },
        "distance_surface": {
            "group_weight": 0.15,
            "features": {"distance_fit": 0.55, "surface_fit": 0.45},
        },
        "race_shape": {
            "group_weight": 0.15,
            "features": {
                "pace_fit_score":          0.65,
                "traffic_resilience_proxy": 0.35,
            },
        },
        "readiness": {
            "group_weight": 0.12,
            "features": {
                "work_readiness_score": 0.50,
                "trainer_intent_proxy": 0.30,
                "finish_energy_proxy":  0.20,
            },
        },
        "market_prior": {
            "group_weight": 0.05,
            "features": {"market_implied_prob": 1.00},
        },
    },
    "turf_route": {
        "speed_quality": {
            "group_weight": 0.22,
            "features": {"speed_best_3": 0.40, "speed_last": 0.35, "beyer_last": 0.25},
        },
        "form_class": {
            "group_weight": 0.18,
            "features": {
                "form_cycle_idx":         0.35,
                "class_delta":            0.30,
                "horses_beaten_pct_last": 0.20,
                "career_win_pct":         0.15,
            },
        },
        "distance_surface": {
            "group_weight": 0.22,
            "features": {"distance_fit": 0.55, "surface_fit": 0.45},
        },
        "race_shape": {
            "group_weight": 0.15,
            "features": {
                "pace_fit_score":          0.65,
                "traffic_resilience_proxy": 0.35,
            },
        },
        "readiness": {
            "group_weight": 0.13,
            "features": {
                "work_readiness_score": 0.50,
                "trainer_intent_proxy": 0.30,
                "finish_energy_proxy":  0.20,
            },
        },
        "market_prior": {
            "group_weight": 0.10,
            "features": {"market_implied_prob": 1.00},
        },
    },
    "turf_sprint": {
        "speed_quality": {
            "group_weight": 0.32,
            "features": {"speed_best_3": 0.40, "speed_last": 0.35, "beyer_last": 0.25},
        },
        "form_class": {
            "group_weight": 0.18,
            "features": {
                "form_cycle_idx":         0.35,
                "class_delta":            0.30,
                "horses_beaten_pct_last": 0.20,
                "career_win_pct":         0.15,
            },
        },
        "distance_surface": {
            "group_weight": 0.18,
            "features": {"distance_fit": 0.55, "surface_fit": 0.45},
        },
        "race_shape": {
            "group_weight": 0.14,
            "features": {
                "pace_fit_score":          0.65,
                "traffic_resilience_proxy": 0.35,
            },
        },
        "readiness": {
            "group_weight": 0.12,
            "features": {
                "work_readiness_score": 0.50,
                "trainer_intent_proxy": 0.30,
                "finish_energy_proxy":  0.20,
            },
        },
        "market_prior": {
            "group_weight": 0.06,
            "features": {"market_implied_prob": 1.00},
        },
    },
}

# ---------------------------------------------------------------------------
# Derby override feature groups — used when is_derby_context() returns True.
# Weights differ from dirt_route to emphasize classic distance fit, traffic
# resilience, and pedigree while reducing market anchoring.
# ---------------------------------------------------------------------------
DERBY_OVERRIDE_FEATURE_GROUPS: dict = {
    "speed_quality": {
        "group_weight": 0.22,   # dirt_route: 0.25 — speed universal but not sole determinant at 1.25mi
        "features": {"speed_best_3": 0.40, "speed_last": 0.35, "beyer_last": 0.25},
    },
    "form_class": {
        "group_weight": 0.15,   # dirt_route: 0.18 — recent form matters less vs. stamina fit
        "features": {
            "form_cycle_idx":         0.35,
            "class_delta":            0.30,
            "horses_beaten_pct_last": 0.20,
            "career_win_pct":         0.15,
        },
    },
    "distance_surface": {
        "group_weight": 0.22,   # dirt_route: 0.17 — classic distance projection is the key Derby ask
        "features": {"distance_fit": 0.60, "surface_fit": 0.40},
    },
    "race_shape": {
        "group_weight": 0.18,   # dirt_route: 0.15 — 20-horse field amplifies traffic risk
        "features": {
            "pace_fit_score":          0.60,
            "traffic_resilience_proxy": 0.40,  # elevated vs 0.35 base
        },
    },
    "readiness": {
        "group_weight": 0.12,   # dirt_route: 0.13
        "features": {
            "work_readiness_score": 0.50,
            "trainer_intent_proxy": 0.30,
            "finish_energy_proxy":  0.20,
        },
    },
    "derby_override": {
        "group_weight": 0.09,   # dirt_route: 0.07 — expanded; Derby sub-components carry more signal
        "features": {"derby_override_score": 1.00},
    },
    "market_prior": {
        "group_weight": 0.02,   # dirt_route: 0.05 — reduce anchoring; public_underlay_penalty
                                 # already embedded in derby_override_score
        "features": {"market_implied_prob": 1.00},
    },
}

TRAIN_CONFIGS: dict[str, dict] = {
    key: {
        "race_type_key":    key,
        "model_family":     key,
        "model_name":       f"{key}_v1",
        "version":          "1.0.0",
        "feature_groups":   FEATURE_GROUPS[key],
        "calibration_method": "temperature_softmax",
        "calibration_target": "morning_line_prob",
        "evaluation_metrics": [
            "sum_win_prob",
            "kendall_tau_vs_ml",
            "kl_div_vs_ml",
            "mean_edge_abs",
            "bet_count",
            "underlay_count",
        ],
        "bet_edge_threshold":      0.025,   # model > market by 2.5pp -> BET
        "underlay_edge_threshold": -0.015,  # model < market by 1.5pp -> UNDERLAY
    }
    for key in FEATURE_GROUPS
}

# The normal dirt-route model deliberately excludes Derby-only projections.
# Its explicit family name prevents a non-Derby card from being presented as
# a Kentucky Derby model in run metadata or downstream UI.
TRAIN_CONFIGS["dirt_route"] = {
    **TRAIN_CONFIGS["dirt_route"],
    "model_family": "dirt_route_stakes_v1",
    "model_name": "dirt_route_stakes_v1",
}

# Derby override config — shares all TRAIN_CONFIGS["dirt_route"] settings but
# substitutes DERBY_OVERRIDE_FEATURE_GROUPS and bumps the model name.
DERBY_TRAIN_CONFIG: dict = {
    **TRAIN_CONFIGS["dirt_route"],
    "feature_groups": DERBY_OVERRIDE_FEATURE_GROUPS,
    "model_name":     "derby_override_v1",
    "model_family":   "dirt_route",
    "race_type_key":  "dirt_route",
}


# ---------------------------------------------------------------------------
# Model artifact
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class ModelArtifact:
    model_type:          str
    race_type_key:       str
    model_name:          str
    version:             str
    training_rows:       int
    temperature:         float
    feature_importances: dict   # feature_name -> relative_weight (sums to 1.0)
    group_scores:        dict   # group_name -> np.ndarray (one score per entry)
    config:              dict


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------
def _norm_field(values: pd.Series) -> np.ndarray:
    """Min-max normalize a Series within its field; NaN -> median; all-NaN -> 0.5."""
    arr = values.astype(float).copy()
    if arr.isna().all():
        return np.full(len(arr), 0.5)   # all-null feature column: neutral prior
    median = arr.median()
    arr.fillna(median, inplace=True)
    lo, hi = arr.min(), arr.max()
    if hi == lo:
        return np.full(len(arr), 0.5)
    return ((arr - lo) / (hi - lo)).values


def _softmax(scores: np.ndarray, temperature: float = 8.0) -> np.ndarray:
    shifted = (scores - scores.max()) * temperature
    exp_s   = np.exp(shifted)
    return exp_s / exp_s.sum()


def _count_historical(conn) -> int:
    try:
        return conn.execute("SELECT COUNT(*) FROM horse_starts").fetchone()[0]
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Feature importance: effective weight of each feature in the composite
# ---------------------------------------------------------------------------
def compute_feature_importances(config: dict) -> dict[str, float]:
    """
    For a weighted composite model, importance(f) = within_group_weight(f) * group_weight.
    Normalized so all importances sum to 1.0.
    """
    raw: dict[str, float] = {}
    for gname, gdef in config["feature_groups"].items():
        gw = gdef["group_weight"]
        for fname, fw in gdef["features"].items():
            raw[fname] = raw.get(fname, 0.0) + gw * fw
    total = sum(raw.values())
    return {k: round(v / total, 4) for k, v in sorted(raw.items(), key=lambda x: -x[1])}


# ---------------------------------------------------------------------------
# Group score computation
# ---------------------------------------------------------------------------
def compute_group_scores(feat_df: pd.DataFrame, config: dict) -> dict[str, np.ndarray]:
    """
    For each feature group:
      1. Min-max normalize each feature within the field
      2. Weight and sum normalized features
    Returns dict: group_name -> np.ndarray of shape (n_entries,)
    """
    groups: dict[str, np.ndarray] = {}
    for gname, gdef in config["feature_groups"].items():
        acc = np.zeros(len(feat_df), dtype=float)
        for fname, fw in gdef["features"].items():
            if fname not in feat_df.columns:
                continue
            normed = _norm_field(feat_df[fname])
            acc   += normed * fw
        groups[gname] = acc
    return groups


def compute_composite(group_scores: dict[str, np.ndarray], config: dict) -> np.ndarray:
    """
    Weighted sum of group scores using group_weight.
    Result is a raw composite score per entry (not a probability).
    """
    composite = np.zeros(len(next(iter(group_scores.values()))), dtype=float)
    for gname, scores in group_scores.items():
        gw = config["feature_groups"][gname]["group_weight"]
        composite += scores * gw
    return composite


# ---------------------------------------------------------------------------
# Calibration: temperature-scaled softmax
# Finds temperature T that minimizes MSE between model and market
# as a soft prior; T is bounded to [1.0, 20.0].
# ---------------------------------------------------------------------------
def calibrate_temperature(
    raw_scores: np.ndarray,
    market_probs: np.ndarray,
) -> tuple[np.ndarray, float]:
    """
    Find temperature T such that softmax(raw_scores * T) best matches
    the market's normalized implied probabilities in mean-squared-error sense.

    This is a soft calibration that preserves model signal while anchoring
    probability spread to market norms.  NOT a substitute for out-of-fold
    calibration against actual race outcomes.
    """
    best_T, best_mse = 8.0, float("inf")
    for T in np.linspace(1.0, 20.0, 200):
        probs = _softmax(raw_scores, T)
        mse   = float(np.mean((probs - market_probs) ** 2))
        if mse < best_mse:
            best_mse = mse
            best_T   = float(T)
    return _softmax(raw_scores, best_T), round(best_T, 2)


# ---------------------------------------------------------------------------
# Seed-only baseline builder
# ---------------------------------------------------------------------------
def build_seed_baseline(
    feat_df:    pd.DataFrame,
    entries_df: pd.DataFrame,
    config:     dict,
) -> tuple["ModelArtifact", np.ndarray]:
    """
    Build a seed-only baseline model (no historical training data).

    Steps:
      1. Compute group scores (normalized features)
      2. Compute weighted composite
      3. Calibrate temperature to minimize MSE vs. market
      4. Return ModelArtifact + array of calibrated win probabilities

    Returns
    -------
    artifact : ModelArtifact
    win_probs : np.ndarray — calibrated win probabilities, sums to 1.0
    """
    group_scores = compute_group_scores(feat_df, config)
    composite    = compute_composite(group_scores, config)

    # Sanitize composite: NaN/inf can occur when every entry in a feature column
    # is null (sparse/screenshot race).  Replace with 0.0 so softmax doesn't
    # collapse to all-NaN win_probs.
    n_bad = int((~np.isfinite(composite)).sum())
    if n_bad:
        print(
            f"[trainer] composite has {n_bad} non-finite value(s) — "
            f"replacing with 0.0 (sparse race, all-null feature columns)"
        )
        composite = np.where(np.isfinite(composite), composite, 0.0)

    # Market calibration target: overround-adjusted morning line probs
    ml_implied = pd.to_numeric(
        feat_df["market_implied_prob"], errors="coerce"
    ).fillna(0.0).values
    ml_sum = ml_implied.sum()
    if ml_sum <= 0:
        market_probs = np.full(len(ml_implied), 1.0 / max(len(ml_implied), 1))
    else:
        market_probs = ml_implied / ml_sum

    win_probs, temperature = calibrate_temperature(composite, market_probs)

    importances = compute_feature_importances(config)

    artifact = ModelArtifact(
        model_type          = "seed_only_baseline",
        race_type_key       = config["race_type_key"],
        model_name          = config["model_name"],
        version             = config["version"] + "-seed-only",
        training_rows       = 0,
        temperature         = temperature,
        feature_importances = importances,
        group_scores        = {k: v.tolist() for k, v in group_scores.items()},
        config              = config,
    )
    return artifact, win_probs


# ---------------------------------------------------------------------------
# XGBoost training path
# ---------------------------------------------------------------------------
def build_xgboost_model(
    train_df: pd.DataFrame,
    config: dict,
    n_cv_folds: int = 5,
) -> tuple["ModelArtifact", dict]:
    """
    Train XGBoost binary classifier using time-ordered cross-validation.

    train_df must contain: race_date, race_id, post_position, won (0/1),
    plus all feature columns in config['feature_groups'].

    Returns (ModelArtifact, metrics_dict).
    """
    import xgboost as xgb
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import log_loss, brier_score_loss

    TIER1_EXTRA = [
        "speed_fig_adj", "layoff_bucket_encoded", "class_delta_v2",
        "horses_beaten_pct_actual", "pace_pressure_tier",
        "collapse_risk_v2", "morning_line_delta",
    ]

    # Build feature column list from config, then append Tier 1 extras
    feature_cols = [
        fname
        for gdef in config["feature_groups"].values()
        for fname in gdef["features"]
    ]
    feature_cols = list(dict.fromkeys(feature_cols + TIER1_EXTRA))
    feature_cols = [c for c in feature_cols if c in train_df.columns]

    train_df = train_df.sort_values("race_date").reset_index(drop=True)
    X = train_df[feature_cols].fillna(train_df[feature_cols].median())
    y = train_df["won"].astype(int)

    n = len(train_df)
    fold_size = n // n_cv_folds
    oof_preds = np.zeros(n)

    model = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        eval_metric="logloss",
        use_label_encoder=False,
        random_state=42,
    )

    for fold in range(1, n_cv_folds + 1):
        train_end = fold * fold_size
        val_start = train_end
        val_end   = min(val_start + fold_size, n)
        if val_end <= val_start:
            break
        X_tr, y_tr   = X.iloc[:train_end],       y.iloc[:train_end]
        X_val, y_val = X.iloc[val_start:val_end], y.iloc[val_start:val_end]
        model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
        oof_preds[val_start:val_end] = model.predict_proba(X_val)[:, 1]

    # Final fit on full data
    model.fit(X, y)

    # Isotonic calibration on OOF predictions
    iso: Optional[IsotonicRegression] = None
    valid_mask = oof_preds > 0
    if valid_mask.sum() > 20:
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(oof_preds[valid_mask], y.values[valid_mask])

    # Holdout metrics on last 20%
    metrics: dict = {}
    holdout_start = int(n * 0.8)
    if holdout_start < n and iso is not None:
        raw_h = model.predict_proba(X.iloc[holdout_start:])[:, 1]
        cal_h = iso.transform(raw_h)
        metrics["log_loss"]    = round(log_loss(y.iloc[holdout_start:], cal_h), 4)
        metrics["brier_score"] = round(brier_score_loss(y.iloc[holdout_start:], cal_h), 4)

    importances = dict(zip(feature_cols, model.feature_importances_.tolist()))

    artifact = ModelArtifact(
        model_type          = "xgboost",
        race_type_key       = config["race_type_key"],
        model_name          = config["model_name"] + "_xgb",
        version             = "2.0.0",
        training_rows       = n,
        temperature         = 1.0,
        feature_importances = importances,
        group_scores        = {},
        config              = config,
    )
    artifact.calibrator   = iso       # type: ignore[attr-defined]
    artifact.xgb_model    = model     # type: ignore[attr-defined]
    artifact.feature_cols = feature_cols  # type: ignore[attr-defined]

    return artifact, metrics


# ---------------------------------------------------------------------------
# Feature importance report
# ---------------------------------------------------------------------------
def save_feature_importance_report(
    artifact: ModelArtifact,
    output_dir: Path,
) -> Path:
    """
    Write feature_importance_report.csv alongside eval artifacts.

    Columns: feature_name, importance_score, importance_rank, group_name, tier
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build reverse mapping: feature_name -> group_name
    feat_to_group: dict[str, str] = {}
    for gname, gdef in artifact.config.get("feature_groups", {}).items():
        for fname in gdef.get("features", {}):
            feat_to_group[fname] = gname

    importances = artifact.feature_importances
    if not importances:
        return output_dir / "feature_importance_report.csv"

    sorted_feats = sorted(importances.items(), key=lambda x: -x[1])
    rows = []
    for rank, (fname, score) in enumerate(sorted_feats, start=1):
        rows.append({
            "feature_name":     fname,
            "importance_score": round(score, 6),
            "importance_rank":  rank,
            "group_name":       feat_to_group.get(fname, "tier1_extra"),
            "tier":             FEATURE_TIERS.get(fname, "UNKNOWN"),
        })

    import csv
    out_path = output_dir / "feature_importance_report.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "feature_name", "importance_score", "importance_rank", "group_name", "tier"
        ])
        w.writeheader()
        w.writerows(rows)

    return out_path


# ---------------------------------------------------------------------------
# Artifact persistence
# ---------------------------------------------------------------------------
def save_artifact(artifact: ModelArtifact) -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    path = MODELS_DIR / f"{artifact.model_name}.pkl"
    with open(path, "wb") as fh:
        pickle.dump(artifact, fh)
    return path


def register_model(
    artifact:      ModelArtifact,
    artifact_path: Path,
    metrics:       dict,
    conn,
) -> int:
    """
    Upsert into model_registry without delete/re-insert.

    INSERT OR REPLACE would delete the old row and bump AUTOINCREMENT,
    breaking any score_runs rows that FK-reference the old model_id.
    Instead: INSERT OR IGNORE to create if absent, then UPDATE metrics.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO model_registry (
            model_name, model_family, version, artifact_path, training_rows
        ) VALUES (?,?,?,?,?)
        """,
        (
            artifact.model_name,
            artifact.race_type_key,
            artifact.version,
            str(artifact_path),
            artifact.training_rows,
        ),
    )
    conn.execute(
        """
        UPDATE model_registry SET
            version=?, artifact_path=?, training_rows=?,
            log_loss=?, brier_score=?, calibration_error=?,
            top1_hit_rate=?, edge_roi=?
        WHERE model_name=?
        """,
        (
            artifact.version,
            str(artifact_path),
            artifact.training_rows,
            metrics.get("log_loss"),
            metrics.get("brier_score"),
            metrics.get("calibration_error"),
            metrics.get("top1_hit_rate"),
            metrics.get("edge_roi"),
            artifact.model_name,
        ),
    )
    row = conn.execute(
        "SELECT model_id FROM model_registry WHERE model_name=?",
        (artifact.model_name,),
    ).fetchone()
    return row["model_id"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def train_or_build(
    feat_df:    pd.DataFrame,
    entries_df: pd.DataFrame,
    race_type_key: str = "dirt_route",
    conn=None,
) -> tuple[ModelArtifact, np.ndarray]:
    """
    Dispatch to seed-only baseline or XGBoost based on available data.

    Parameters
    ----------
    feat_df       : feature store rows for this card
    entries_df    : v_entries_live rows for this card
    race_type_key : one of dirt_route | dirt_sprint | turf_route | turf_sprint
    conn          : open sqlite connection (used only for training data count)

    Returns (ModelArtifact, win_prob_array)
    """
    config = TRAIN_CONFIGS.get(race_type_key, TRAIN_CONFIGS["dirt_route"])

    n_historical = _count_historical(conn) if conn else 0

    if n_historical >= MIN_TRAINING_ROWS and conn is not None:
        try:
            train_df = pd.read_sql(
                """
                SELECT so.race_date, so.race_id, so.post AS post_position,
                       so.win_flag AS won, fs.*
                FROM   starter_observations so
                JOIN   feature_store fs
                       ON  fs.card_id      = so.race_id
                       AND fs.post_position = so.post
                WHERE  so.win_flag IS NOT NULL
                  AND  COALESCE(so.scratched, 0) = 0
                ORDER  BY so.race_date, so.race_id, so.post
                """,
                conn,
            )
            if len(train_df) >= MIN_TRAINING_ROWS:
                print(
                    f"[trainer] {n_historical} historical rows — training XGBoost "
                    f"on {len(train_df)} joined observations."
                )
                artifact, _metrics = build_xgboost_model(train_df, config)

                # Compute win probabilities for the current card using trained model
                try:
                    feat_cols = artifact.feature_cols  # type: ignore[attr-defined]
                    model     = artifact.xgb_model     # type: ignore[attr-defined]
                    iso       = artifact.calibrator    # type: ignore[attr-defined]
                    X_now = feat_df[[c for c in feat_cols if c in feat_df.columns]]
                    X_now = X_now.reindex(columns=feat_cols).fillna(
                        train_df[[c for c in feat_cols if c in train_df.columns]].median()
                    )
                    raw_probs = model.predict_proba(X_now)[:, 1]
                    if iso is not None:
                        raw_probs = iso.transform(raw_probs)
                    raw_probs = np.where(np.isfinite(raw_probs), raw_probs, 0.0)
                    psum = raw_probs.sum()
                    win_probs = raw_probs / psum if psum > 0 else np.full(len(raw_probs), 1.0 / len(raw_probs))
                except Exception as infer_exc:
                    print(f"[trainer] XGBoost inference failed ({infer_exc}) — using market prior.")
                    win_probs = (
                        pd.to_numeric(feat_df.get("market_implied_prob"), errors="coerce")
                        .fillna(0.0).values
                    )
                    psum = win_probs.sum()
                    win_probs = win_probs / psum if psum > 0 else np.full(len(win_probs), 1.0 / len(win_probs))

                # Save feature importance report to output/
                _output_dir = ROOT / "output"
                try:
                    save_feature_importance_report(artifact, _output_dir)
                except Exception:
                    pass

                return artifact, win_probs

            print(
                f"[trainer] {n_historical} horse_starts rows but only "
                f"{len(train_df)} joined feature rows — using seed_only_baseline."
            )
        except ImportError:
            print("[trainer] xgboost not installed — falling back to seed_only_baseline.")
        except Exception as exc:
            print(f"[trainer] XGBoost path failed ({exc}) — falling back to seed_only_baseline.")

    print(
        f"[trainer] {n_historical} historical rows (need {MIN_TRAINING_ROWS}). "
        "Building seed_only_baseline."
    )
    artifact, win_probs = build_seed_baseline(feat_df, entries_df, config)

    # Save feature importance report to output/
    _output_dir = ROOT / "output"
    try:
        save_feature_importance_report(artifact, _output_dir)
    except Exception:
        pass

    return artifact, win_probs
