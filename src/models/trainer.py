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
                "form_cycle_idx":         0.35,
                "class_delta":            0.30,
                "horses_beaten_pct_last": 0.20,
                "career_win_pct":         0.15,
            },
        },
        "distance_surface": {
            "group_weight": 0.17,
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
                "work_readiness_score":  0.50,
                "trainer_intent_proxy":  0.30,
                "finish_energy_proxy":   0.20,
            },
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
    """Min-max normalize a Series within its field; NaN -> median."""
    arr = values.astype(float).copy()
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

    # Market calibration target: overround-adjusted morning line probs
    ml_implied   = feat_df["market_implied_prob"].astype(float).values
    market_probs = ml_implied / ml_implied.sum()

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

    if n_historical >= MIN_TRAINING_ROWS:
        # Future path: XGBoost with rolling CV
        # Not yet implemented; fall through to seed baseline.
        print(
            f"[trainer] {n_historical} historical rows found but XGBoost trainer "
            "not yet implemented — using seed_only_baseline."
        )

    print(
        f"[trainer] No historical data ({n_historical} rows, need {MIN_TRAINING_ROWS}). "
        "Building seed_only_baseline."
    )
    return build_seed_baseline(feat_df, entries_df, config)
