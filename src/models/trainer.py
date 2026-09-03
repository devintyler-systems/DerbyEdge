"""
DerbyEdge V1  —  Model Trainer
src/models/trainer.py

Training paths:
  1. SEED-ONLY BASELINE (current state)
     remains production until completed-race and rolling-OOF gates pass
     -> build a principled weighted composite over the versioned feature catalog
     -> calibrate spread via temperature-scaled softmax
     -> labeled "seed_only_baseline" in model_registry

  2. XGBOOST FAMILIES (future, once historical races are loaded)
     Exact-family candidates require 500 completed races, 4,000 labeled
     starters, 12 rolling race-level folds, coverage, leakage, comparative
     performance, calibration, and artifact-registration checks.

Race-type families:   dirt_route | dirt_sprint | turf_route | turf_sprint
Derby uses: dirt_route
"""

import dataclasses
import datetime
import json
import pickle
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT       = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "saved_models"

FEATURE_SCHEMA_VERSION = "2.0.0-pre-race-reliability"


@dataclasses.dataclass(frozen=True)
class XGBoostPromotionConfig:
    minimum_completed_races: int = 500
    minimum_labeled_starters: int = 4_000
    minimum_rolling_validation_folds: int = 12
    minimum_core_non_market_feature_coverage: float = 0.80
    minimum_brier_improvement: float = 0.02
    minimum_log_loss_improvement: float = 0.01
    maximum_calibration_error: float = 0.05
    maximum_field_brier_regression: float = 0.02
    shadow_minimum_completed_races: int = 20
    shadow_minimum_labeled_starters: int = 100
    shadow_minimum_feature_coverage: float = 0.50


XGBOOST_PROMOTION_CONFIG = XGBoostPromotionConfig()


@dataclasses.dataclass(frozen=True)
class DispatcherDecision:
    mode: str
    production_model: str
    reason_codes: tuple[str, ...]
    completed_races: int
    labeled_starters: int
    rolling_validation_folds: int
    core_feature_coverage: float


def _relative_improvement(baseline: object, candidate: object) -> float:
    try:
        baseline_f, candidate_f = float(baseline), float(candidate)
    except (TypeError, ValueError):
        return float("-inf")
    if not np.isfinite(baseline_f) or not np.isfinite(candidate_f) or baseline_f <= 0:
        return float("-inf")
    return (baseline_f - candidate_f) / baseline_f


def _audit_flag(value: object) -> bool:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "pass", "passed"}
    return bool(value)


def _registered_value(value: object) -> bool:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return False
    return bool(str(value).strip())


def assess_xgboost_promotion(
    readiness: dict,
    oof_metrics: dict | None = None,
    gate: XGBoostPromotionConfig = XGBOOST_PROMOTION_CONFIG,
) -> DispatcherDecision:
    """Return an auditable production/shadow/baseline dispatch decision."""
    metrics = oof_metrics or {}
    completed = max(0, int(readiness.get("completed_races", 0) or 0))
    starters = max(0, int(readiness.get("labeled_starters", 0) or 0))
    folds = max(0, int(readiness.get("rolling_validation_folds", 0) or 0))
    coverage = float(np.clip(readiness.get("core_feature_coverage", 0.0) or 0.0, 0.0, 1.0))
    reasons: list[str] = []
    if completed < gate.minimum_completed_races:
        reasons.append("insufficient_completed_races")
    if starters < gate.minimum_labeled_starters:
        reasons.append("insufficient_labeled_starters")
    if coverage < gate.minimum_core_non_market_feature_coverage:
        reasons.append("insufficient_feature_coverage")
    if folds < gate.minimum_rolling_validation_folds:
        reasons.append("insufficient_rolling_folds")
    if not _audit_flag(readiness.get("race_group_membership_valid", False)):
        reasons.append("invalid_race_group_membership")
    if not _audit_flag(readiness.get("valid_outcome_labels", False)):
        reasons.append("invalid_outcome_labels")
    if not _audit_flag(readiness.get("no_target_race_leakage", False)):
        reasons.append("leakage_check_failed")

    ready = not reasons
    if ready:
        brier_gain = _relative_improvement(
            metrics.get("baseline_brier_score"), metrics.get("brier_score")
        )
        log_gain = _relative_improvement(
            metrics.get("baseline_log_loss"), metrics.get("log_loss")
        )
        if brier_gain < gate.minimum_brier_improvement or log_gain < gate.minimum_log_loss_improvement:
            reasons.append("oof_baseline_not_beaten")
        if not _audit_flag(metrics.get("calibration_acceptable", False)):
            reasons.append("calibration_failed")
        if not _audit_flag(metrics.get("field_size_regression_acceptable", False)):
            reasons.append("field_size_regression")
        required_registry = (
            "artifact_path", "feature_schema_version", "training_window_start",
            "training_window_end", "target_race_type_key", "calibration_artifact_path",
        )
        if any(not _registered_value(metrics.get(name)) for name in required_registry):
            reasons.append("artifact_registration_incomplete")
        expected_family = readiness.get("target_race_type_key")
        if expected_family and metrics.get("target_race_type_key") != expected_family:
            reasons.append("artifact_family_mismatch")

    if not reasons:
        reasons = ["promoted"]
        mode, production = "promoted", "xgboost"
    else:
        shadow_ready = (
            completed >= gate.shadow_minimum_completed_races
            and starters >= gate.shadow_minimum_labeled_starters
            and coverage >= gate.shadow_minimum_feature_coverage
            and _audit_flag(readiness.get("race_group_membership_valid", False))
            and _audit_flag(readiness.get("valid_outcome_labels", False))
            and _audit_flag(readiness.get("no_target_race_leakage", False))
        )
        mode, production = ("shadow", "seed_only_baseline") if shadow_ready else ("baseline", "seed_only_baseline")
        if shadow_ready:
            reasons.append("shadow_only")
    return DispatcherDecision(
        mode=mode,
        production_model=production,
        reason_codes=tuple(dict.fromkeys(reasons)),
        completed_races=completed,
        labeled_starters=starters,
        rolling_validation_folds=folds,
        core_feature_coverage=coverage,
    )

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
    "recent_finish_percentile_w":  "T2",
    "starts_last_90d":             "T2",
    "class_delta_last_to_today":   "T2",
    "distance_fit_eb":             "T2",
    "surface_fit_eb":              "T2",
    "surface_distance_finish_percentile_w": "T2",
    "workout_readiness_score_v2":  "T2",
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
            "group_weight": 0.18,
            "features": {
                "recent_finish_percentile_w": 0.40,
                "starts_last_90d":             0.15,
                "class_delta_last_to_today":   0.25,
                "form_cycle_idx":              0.20,
            },
            "coverage_feature": "form_class_coverage",
            "reliability_features": {
                "recent_finish_percentile_w", "starts_last_90d",
                "class_delta_last_to_today",
            },
        },
        "distance_surface": {
            "group_weight": 0.17,
            "features": {
                "distance_fit_eb": 0.30,
                "surface_fit_eb": 0.25,
                "surface_distance_finish_percentile_w": 0.30,
                "distance_fit": 0.08,
                "surface_fit": 0.07,
            },
            "coverage_feature": "distance_surface_coverage",
            "reliability_features": {
                "distance_fit_eb", "surface_fit_eb",
                "surface_distance_finish_percentile_w",
            },
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
                "workout_readiness_score_v2": 0.55,
                "work_readiness_score":       0.20,
                "trainer_intent_proxy":       0.05,
                "finish_energy_proxy":   0.20,
            },
            "coverage_feature": "readiness_coverage",
            "reliability_features": {"workout_readiness_score_v2"},
        },
        "derby_override": {
            "group_weight": 0.07,
            "features": {"derby_override_score": 1.00},
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
                "recent_finish_percentile_w": 0.15,
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
                "recent_finish_percentile_w": 0.15,
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
                "recent_finish_percentile_w": 0.15,
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
    name: {key: (value.copy() if isinstance(value, dict) else value)
           for key, value in definition.items()}
    for name, definition in FEATURE_GROUPS["dirt_route"].items()
}

BASELINE_TEMPERATURE_MIN = 0.25
BASELINE_TEMPERATURE_MAX = 4.00
BASELINE_TEMPERATURE_DEFAULT = 1.00


TRAIN_CONFIGS: dict[str, dict] = {
    key: {
        "race_type_key":    key,
        "model_family":     key,
        "model_name":       f"{key}_v1",
        "version":          "1.0.0",
        "feature_groups":   FEATURE_GROUPS[key],
        "calibration_method": "temperature_softmax",
        "calibration_target": "morning_line_prob",
        "temperature_lower_bound": BASELINE_TEMPERATURE_MIN,
        "temperature_upper_bound": BASELINE_TEMPERATURE_MAX,
        "temperature_default": BASELINE_TEMPERATURE_DEFAULT,
        "near_market_collapse_js_threshold": 0.001,
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
    calibration_audit:   dict = dataclasses.field(default_factory=dict)
    dispatcher_audit:    dict = dataclasses.field(default_factory=dict)


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


def _softmax(scores: np.ndarray, temperature: float = BASELINE_TEMPERATURE_DEFAULT) -> np.ndarray:
    shifted = (scores - scores.max()) * temperature
    exp_s   = np.exp(shifted)
    return exp_s / exp_s.sum()


def _entropy(probabilities: np.ndarray) -> float:
    p = np.asarray(probabilities, dtype=float)
    p = np.clip(p / max(float(p.sum()), 1e-12), 1e-12, 1.0)
    return float(-np.sum(p * np.log(p)))


def jensen_shannon_divergence(left: np.ndarray, right: np.ndarray) -> float:
    """Stable Jensen-Shannon divergence in natural-log units."""
    p = np.clip(np.asarray(left, dtype=float), 1e-12, None)
    q = np.clip(np.asarray(right, dtype=float), 1e-12, None)
    p, q = p / p.sum(), q / q.sum()
    midpoint = 0.5 * (p + q)
    return float(0.5 * np.sum(p * np.log(p / midpoint)) + 0.5 * np.sum(q * np.log(q / midpoint)))


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
        coverage_col = gdef.get("coverage_feature")
        reliable_features = set(gdef.get("reliability_features", ()))
        if coverage_col and coverage_col in feat_df.columns:
            coverage = (
                pd.to_numeric(feat_df[coverage_col], errors="coerce")
                .fillna(0.0).clip(0.0, 1.0).to_numpy(dtype=float)
            )
        else:
            coverage = np.ones(len(feat_df), dtype=float)
        for fname, fw in gdef["features"].items():
            normed = (
                _norm_field(feat_df[fname])
                if fname in feat_df.columns else np.full(len(feat_df), 0.5)
            )
            # Reliability changes feature values, not group totals: sparse
            # evidence moves toward the existing 0.50 neutral convention.
            if fname in reliable_features:
                normed = coverage * normed + (1.0 - coverage) * 0.50
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
# as a soft prior; T is bounded to [0.25, 4.00].
# ---------------------------------------------------------------------------
def calibrate_temperature(
    raw_scores: np.ndarray,
    market_probs: np.ndarray,
    config: dict | None = None,
) -> tuple[np.ndarray, float]:
    """
    Find temperature T such that softmax(raw_scores * T) best matches
    the market's normalized implied probabilities in mean-squared-error sense.

    This is a soft calibration that preserves model signal while anchoring
    probability spread to market norms.  NOT a substitute for out-of-fold
    calibration against actual race outcomes.
    """
    config = config or {}
    lower = float(config.get("temperature_lower_bound", BASELINE_TEMPERATURE_MIN))
    upper = float(config.get("temperature_upper_bound", BASELINE_TEMPERATURE_MAX))
    default = float(config.get("temperature_default", BASELINE_TEMPERATURE_DEFAULT))
    threshold = float(config.get("near_market_collapse_js_threshold", 0.001))
    lower, upper = min(lower, upper), max(lower, upper)
    best_T, best_mse = float(np.clip(default, lower, upper)), float("inf")
    eligible_found = False
    for T in np.linspace(lower, upper, 200):
        probs = _softmax(raw_scores, T)
        if jensen_shannon_divergence(probs, market_probs) < threshold:
            continue
        mse   = float(np.mean((probs - market_probs) ** 2))
        if mse < best_mse:
            best_mse = mse
            best_T   = float(T)
            eligible_found = True
    if not eligible_found:
        best_T = float(np.clip(default, lower, upper))
    return _softmax(raw_scores, best_T), round(best_T, 2)


def temperature_adjustment_status(temperature: float) -> str:
    """Describe whether softmax temperature softens, preserves, or sharpens spread."""
    if np.isclose(float(temperature), 1.0):
        return "unchanged"
    return "softened" if temperature < 1.0 else "sharpened"


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

    uncalibrated = _softmax(composite, 1.0)
    win_probs, temperature = calibrate_temperature(composite, market_probs, config)
    divergence = jensen_shannon_divergence(win_probs, market_probs)
    sources = (
        feat_df.get("market_implied_prob_source", pd.Series(dtype=object))
        .dropna().astype(str).str.lower().unique().tolist()
    )
    morning_line_available = bool(ml_sum > 0 and "morning_line" in sources) if sources else bool(ml_sum > 0)
    threshold = float(config.get("near_market_collapse_js_threshold", 0.001))
    calibration_status = (
        "near_market_collapse_warning" if morning_line_available and divergence < threshold
        else "soft_market_anchor" if morning_line_available
        else "uniform_prior_no_morning_line"
    )

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
        calibration_audit   = {
            "uncalibrated_entropy": round(_entropy(uncalibrated), 8),
            "calibrated_entropy": round(_entropy(win_probs), 8),
            "selected_temperature": temperature,
            "temperature_adjustment_status": temperature_adjustment_status(temperature),
            "morning_line_available": morning_line_available,
            "market_prior_source": "morning_line" if morning_line_available else "uniform",
            "divergence_from_morning_line": round(divergence, 8),
            "calibration_status": calibration_status,
        },
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
    Train an XGBoost classifier using rolling chronological race-level folds.

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
        "collapse_risk_v2",
    ]
    prohibited = {
        "career_win_pct", "career_earnings", "market_implied_prob",
        "morning_line_delta", "prior_publicness", "historical_odds_raw",
        "off_odds", "trainer", "jockey", "trainer_intent_proxy",
        "derby_override_score",
        "hot_trainer", "hot_jockey", "top_pick", "key_trainer",
        "clocker_special", "angles", "annotations",
    }

    # Build feature column list from config, then append Tier 1 extras
    feature_cols = [
        fname
        for gdef in config["feature_groups"].values()
        for fname in gdef["features"]
    ]
    feature_cols = list(dict.fromkeys(feature_cols + TIER1_EXTRA))
    feature_cols = [c for c in feature_cols if c in train_df.columns and c not in prohibited]

    train_df = train_df.sort_values("race_date").reset_index(drop=True)
    X = train_df[feature_cols].fillna(train_df[feature_cols].median())
    y = train_df["won"].astype(int)

    n = len(train_df)
    oof_preds = np.zeros(n)

    def _new_model():
        return xgb.XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=5,
            eval_metric="logloss", random_state=42,
        )

    race_order = (
        train_df[["race_date", "race_id"]].drop_duplicates()
        .sort_values(["race_date", "race_id"])["race_id"].tolist()
    )
    blocks = [list(block) for block in np.array_split(race_order, n_cv_folds + 1) if len(block)]
    folds_completed = 0
    for fold in range(1, len(blocks)):
        train_races = {race for block in blocks[:fold] for race in block}
        val_races = set(blocks[fold])
        train_mask = train_df["race_id"].isin(train_races).to_numpy()
        val_mask = train_df["race_id"].isin(val_races).to_numpy()
        if not train_mask.any() or not val_mask.any() or y.iloc[train_mask].nunique() < 2:
            continue
        fold_model = _new_model()
        fold_model.fit(X.iloc[train_mask], y.iloc[train_mask], verbose=False)
        raw = fold_model.predict_proba(X.iloc[val_mask])[:, 1]
        val_race_ids = train_df.loc[val_mask, "race_id"].to_numpy()
        for race_id in np.unique(val_race_ids):
            local = val_race_ids == race_id
            total = float(raw[local].sum())
            raw[local] = raw[local] / total if total > 0 else 1.0 / local.sum()
        oof_preds[val_mask] = raw
        folds_completed += 1

    # Final fit on full data
    model = _new_model()
    model.fit(X, y)

    # Isotonic calibration on OOF predictions
    iso: Optional[IsotonicRegression] = None
    valid_mask = oof_preds > 0
    if valid_mask.sum() > 20:
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(oof_preds[valid_mask], y.values[valid_mask])

    # Metrics are exclusively rolling OOF; the final in-sample fit is never
    # used to decide promotion or to train the calibrator.
    metrics: dict = {}
    if valid_mask.sum() > 20:
        cal_oof = iso.transform(oof_preds[valid_mask]) if iso is not None else oof_preds[valid_mask]
        valid_races = train_df.loc[valid_mask, "race_id"].to_numpy()
        for race_id in np.unique(valid_races):
            local = valid_races == race_id
            total = float(cal_oof[local].sum())
            cal_oof[local] = cal_oof[local] / total if total > 0 else 1.0 / local.sum()
        y_oof = y.values[valid_mask]
        metrics["log_loss"] = round(log_loss(y_oof, np.clip(cal_oof, 1e-9, 1 - 1e-9)), 6)
        metrics["brier_score"] = round(brier_score_loss(y_oof, cal_oof), 6)
        if "baseline_win_probability" in train_df.columns:
            base = pd.to_numeric(
                train_df.loc[valid_mask, "baseline_win_probability"], errors="coerce"
            ).fillna(0.0).to_numpy()
            for race_id in np.unique(valid_races):
                local = valid_races == race_id
                total = float(base[local].sum())
                base[local] = base[local] / total if total > 0 else 1.0 / local.sum()
            metrics["baseline_log_loss"] = round(log_loss(y_oof, np.clip(base, 1e-9, 1 - 1e-9)), 6)
            metrics["baseline_brier_score"] = round(brier_score_loss(y_oof, base), 6)
        buckets = pd.qcut(cal_oof, q=min(10, len(np.unique(cal_oof))), duplicates="drop")
        calibration_error = max(
            abs(float(cal_oof[buckets == bucket].mean()) - float(y_oof[buckets == bucket].mean()))
            for bucket in buckets.categories
        ) if len(buckets.categories) else 1.0
        metrics["calibration_error"] = round(calibration_error, 6)
        metrics["calibration_acceptable"] = (
            calibration_error <= XGBOOST_PROMOTION_CONFIG.maximum_calibration_error
        )
        metrics["field_size_regression_acceptable"] = False
        if "field_size" in train_df.columns and "baseline_win_probability" in train_df.columns:
            field = pd.to_numeric(train_df.loc[valid_mask, "field_size"], errors="coerce")
            labels = pd.cut(field, bins=[0, 7, 11, float("inf")], labels=["small", "medium", "large"])
            comparisons = []
            for label in labels.dropna().unique():
                local = (labels == label).to_numpy()
                if local.sum() >= 20:
                    comparisons.append(
                        brier_score_loss(y_oof[local], cal_oof[local])
                        <= brier_score_loss(y_oof[local], base[local])
                        * (1.0 + XGBOOST_PROMOTION_CONFIG.maximum_field_brier_regression)
                    )
            metrics["field_size_regression_acceptable"] = bool(comparisons and all(comparisons))
    metrics["rolling_validation_folds"] = folds_completed
    metrics["training_window_start"] = str(train_df["race_date"].min())
    metrics["training_window_end"] = str(train_df["race_date"].max())
    metrics["target_race_type_key"] = config["race_type_key"]
    metrics["feature_schema_version"] = FEATURE_SCHEMA_VERSION

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
    from src.utils.db import ensure_model_registry_columns
    ensure_model_registry_columns(conn)
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
            top1_hit_rate=?, edge_roi=?, dispatcher_mode=?,
            dispatcher_reason_codes=?, completed_races=?, labeled_starters=?,
            rolling_validation_folds=?, core_feature_coverage=?,
            feature_schema_version=?, baseline_log_loss=?, baseline_brier_score=?,
            calibration_acceptable=?, field_size_regression_acceptable=?,
            target_race_type_key=?, training_window_start=?, training_window_end=?,
            calibration_artifact_path=?
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
            artifact.dispatcher_audit.get("mode"),
            json.dumps(artifact.dispatcher_audit.get("reason_codes", [])),
            metrics.get("completed_races", artifact.dispatcher_audit.get("completed_races")),
            metrics.get("labeled_starters", artifact.dispatcher_audit.get("labeled_starters")),
            metrics.get("rolling_validation_folds", artifact.dispatcher_audit.get("rolling_validation_folds")),
            metrics.get("core_feature_coverage", artifact.dispatcher_audit.get("core_feature_coverage")),
            FEATURE_SCHEMA_VERSION,
            metrics.get("baseline_log_loss"), metrics.get("baseline_brier_score"),
            int(bool(metrics.get("calibration_acceptable"))) if metrics.get("calibration_acceptable") is not None else None,
            int(bool(metrics.get("field_size_regression_acceptable"))) if metrics.get("field_size_regression_acceptable") is not None else None,
            artifact.race_type_key,
            metrics.get("training_window_start"), metrics.get("training_window_end"),
            metrics.get("calibration_artifact_path") or (
                str(artifact_path) if getattr(artifact, "calibrator", None) is not None else None
            ),
            artifact.model_name,
        ),
    )
    row = conn.execute(
        "SELECT model_id FROM model_registry WHERE model_name=?",
        (artifact.model_name,),
    ).fetchone()
    return row["model_id"]


def _dispatcher_inputs(conn, race_type_key: str, config: dict) -> tuple[pd.DataFrame, dict, dict]:
    """Load exact-family completed races and registered candidate OOF metrics."""
    empty = {
        "completed_races": 0, "labeled_starters": 0,
        "rolling_validation_folds": 0, "core_feature_coverage": 0.0,
        "race_group_membership_valid": False, "valid_outcome_labels": False,
        "no_target_race_leakage": False,
    }
    try:
        obs = pd.read_sql("SELECT * FROM starter_observations", conn)
    except Exception:
        return pd.DataFrame(), empty, {}
    if obs.empty:
        return pd.DataFrame(), empty, {}
    expected_surface, expected_distance = race_type_key.split("_", 1)
    surface = obs["surface"].fillna("").astype(str).str.lower()
    distance = obs["distance_bucket"].fillna("").astype(str).str.lower()
    family = obs[(surface == expected_surface) & (distance == expected_distance)].copy()
    if family.empty:
        return pd.DataFrame(), empty, {}
    non_scratched = family[
        pd.to_numeric(family["scratched"], errors="coerce").fillna(0) == 0
    ]
    completed_ids: list[int] = []
    for race_id, group in non_scratched.groupby("race_id"):
        labels = pd.to_numeric(group["win_flag"], errors="coerce")
        if labels.notna().all() and int(labels.sum()) == 1:
            completed_ids.append(int(race_id))
    completed = non_scratched[non_scratched["race_id"].isin(completed_ids)].copy()
    if completed.empty:
        return pd.DataFrame(), empty, {}
    try:
        features = pd.read_sql("SELECT * FROM feature_store", conn)
        joined = completed.merge(
            features, left_on=["race_id", "post"],
            right_on=["card_id", "post_position"], how="inner",
            suffixes=("", "_feature"),
        )
    except Exception:
        joined = pd.DataFrame()
    core = [
        name for group_name, group in config["feature_groups"].items()
        if group_name not in {"market_prior", "derby_override"}
        for name in group["features"]
        if name not in {"trainer_intent_proxy"}
    ]
    if joined.empty or not core:
        coverage = 0.0
        leakage_ok = False
    else:
        present = sum(
            int(pd.to_numeric(joined[name], errors="coerce").notna().sum())
            if name in joined.columns else 0
            for name in core
        )
        coverage = present / (len(joined) * len(core))
        build_dates = pd.to_datetime(joined.get("build_ts"), errors="coerce").dt.date
        race_dates = pd.to_datetime(joined["race_date"], errors="coerce").dt.date
        leakage_ok = bool(build_dates.notna().all() and race_dates.notna().all() and (build_dates <= race_dates).all())
        joined["won"] = pd.to_numeric(joined["win_flag"], errors="coerce").astype(int)
        joined["post_position"] = joined["post"]
        joined["baseline_win_probability"] = pd.to_numeric(joined["pred_win_prob"], errors="coerce")

    candidate: dict = {}
    try:
        candidate_df = pd.read_sql(
            """SELECT * FROM model_registry
               WHERE target_race_type_key=? AND artifact_path IS NOT NULL
               ORDER BY created_at DESC LIMIT 1""",
            conn, params=(race_type_key,),
        )
        if not candidate_df.empty:
            candidate = candidate_df.iloc[0].to_dict()
    except Exception:
        candidate = {}
    summary = {
        "completed_races": len(completed_ids),
        "labeled_starters": len(completed),
        "rolling_validation_folds": int(candidate.get("rolling_validation_folds") or 0),
        "core_feature_coverage": round(float(np.clip(coverage, 0.0, 1.0)), 6),
        "race_group_membership_valid": bool(completed_ids),
        "valid_outcome_labels": bool(completed_ids),
        "no_target_race_leakage": leakage_ok,
        "target_race_type_key": race_type_key,
    }
    return joined, summary, candidate


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
    Keep baseline in production unless exact-family readiness and registered
    rolling-OOF evidence satisfy every promotion criterion.

    Parameters
    ----------
    feat_df       : feature store rows for this card
    entries_df    : v_entries_live rows for this card
    race_type_key : one of dirt_route | dirt_sprint | turf_route | turf_sprint
    conn          : open sqlite connection (used only for training data count)

    Returns (ModelArtifact, win_prob_array)
    """
    config = TRAIN_CONFIGS.get(race_type_key, TRAIN_CONFIGS["dirt_route"])

    train_df, readiness, candidate = (
        _dispatcher_inputs(conn, race_type_key, config) if conn is not None
        else (pd.DataFrame(), {
            "completed_races": 0, "labeled_starters": 0,
            "rolling_validation_folds": 0, "core_feature_coverage": 0.0,
            "race_group_membership_valid": False, "valid_outcome_labels": False,
            "no_target_race_leakage": False,
        }, {})
    )
    decision = assess_xgboost_promotion(readiness, candidate)
    artifact, win_probs = build_seed_baseline(feat_df, entries_df, config)
    artifact.dispatcher_audit = dataclasses.asdict(decision)
    print(
        f"[trainer] production={decision.production_model} mode={decision.mode} "
        f"reasons={','.join(decision.reason_codes)}"
    )

    if decision.mode == "promoted" and not train_df.empty:
        try:
            candidate_path = Path(str(candidate["artifact_path"]))
            if not candidate_path.is_file():
                raise FileNotFoundError(candidate_path)
            with candidate_path.open("rb") as handle:
                promoted = pickle.load(handle)
            if not isinstance(promoted, ModelArtifact) or promoted.race_type_key != race_type_key:
                raise ValueError("registered artifact family mismatch")
            feat_cols = promoted.feature_cols  # type: ignore[attr-defined]
            model = promoted.xgb_model  # type: ignore[attr-defined]
            calibrator = promoted.calibrator  # type: ignore[attr-defined]
            medians = train_df.reindex(columns=feat_cols).median(numeric_only=True)
            X_now = feat_df.reindex(columns=feat_cols).apply(pd.to_numeric, errors="coerce").fillna(medians).fillna(0.5)
            raw_probs = model.predict_proba(X_now)[:, 1]
            if calibrator is not None:
                raw_probs = calibrator.transform(raw_probs)
            total = float(np.sum(raw_probs))
            if total <= 0 or not np.isfinite(raw_probs).all():
                raise ValueError("invalid promoted probability vector")
            promoted.dispatcher_audit = dataclasses.asdict(decision)
            artifact, win_probs = promoted, raw_probs / total
        except Exception as exc:
            artifact.dispatcher_audit["mode"] = "baseline"
            artifact.dispatcher_audit["production_model"] = "seed_only_baseline"
            artifact.dispatcher_audit["reason_codes"] = list(decision.reason_codes) + ["promotion_runtime_failed"]
            print(f"[trainer] promoted model unavailable ({exc}); baseline remains production")

    # Save feature importance report to output/
    _output_dir = ROOT / "output"
    try:
        save_feature_importance_report(artifact, _output_dir)
    except Exception:
        pass

    return artifact, win_probs
