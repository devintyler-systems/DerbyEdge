"""
DerbyEdge confidence scoring system.

Replaces the single dist_starts gate (dist_starts <= 1 → LOW) with a
4-component scored system that reflects genuine evidence quality.

Components and weights
----------------------
A. Horse evidence quality          weight=0.35  (per-horse)
   - distance starts score  (50% of A)
   - career starts score    (25% of A)
   - null model-feature penalty (25% of A)

B. Race evidence quality           weight=0.25  (race-level, same for all horses)
   - field size score       (50% of B)
   - morning-line coverage  (50% of B)

C. Model certainty                 weight=0.30  (race-level, same for all horses)
   - Shannon entropy (lower = more concentrated)  (45% of C)
   - top-1 vs top-2 win-prob gap                  (35% of C)
   - model-market favorite alignment              (20% of C)

D. Calibration support             weight=0.10  (race-level)
   - defaults to 0.50 (neutral) when no outcomes history is available
   - degrades gracefully; never collapses the race to LOW by itself

Final score = 0.35*A + 0.25*B + 0.30*C + 0.10*D

Thresholds
----------
score < 0.45     → LOW
0.45 ≤ score < 0.70 → MEDIUM
score ≥ 0.70     → HIGH

Backward compatibility
----------------------
confidence_flag:  0 = LOW,  1 = MEDIUM or HIGH  (same as before)
confidence_score: new float column (0.0–1.0)
confidence_bucket: new TEXT column  (LOW / MEDIUM / HIGH)
confidence_reasons: new TEXT column (semicolon-separated, top 3 drivers)
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

# Thresholds — match spec exactly
LOW_THRESHOLD    = 0.45
HIGH_THRESHOLD   = 0.70

# Missing-data flags kept for backward compat (board CSV / markdown output)
CRITICAL_MISSING = [
    "no_race_splits",       # pace_early_mean_3 / pace_mid_mean_3
    "no_workout_detail",    # bullet_30d / days_since_last_work
    "no_connections_stats", # trainer_jockey_itm_cond / jockey_route_cond
    "no_track_form",        # churchill_readiness
    "no_post_bias",         # post_win_bias
]
DERBY_EXTRA_MISSING = [
    "no_jan_apr_curve",
    "no_churchill_readiness",
]


# ---------------------------------------------------------------------------
# Component A  — Horse evidence quality
# ---------------------------------------------------------------------------

# dist_starts → contribution within A_dist sub-score
_DIST_SCORE = {0: 0.00, 1: 0.40, 2: 0.70, 3: 0.85}


def _component_a(
    dist_starts: int,
    career_starts: int,
    has_null_model_feat: bool,
) -> tuple[float, list[str]]:
    """Return (score 0–1, list[reason_string])."""
    reasons: list[str] = []

    a_dist = _DIST_SCORE.get(dist_starts, 1.00)
    if dist_starts == 0:
        reasons.append("no dist history")
    elif dist_starts == 1:
        reasons.append("1 dist start")
    elif dist_starts >= 4:
        reasons.append(f"{dist_starts} dist starts")   # positive driver

    # Career saturation at 15 starts
    a_career = min(career_starts / 15.0, 1.0)
    if career_starts < 5:
        reasons.append(f"limited career ({career_starts} starts)")
    elif career_starts >= 15:
        reasons.append(f"veteran ({career_starts} starts)")   # positive

    # Null model-feature penalty
    a_null = 0.0 if has_null_model_feat else 1.0
    if has_null_model_feat:
        reasons.append("missing model features")

    score = 0.50 * a_dist + 0.25 * a_career + 0.25 * a_null
    return round(score, 4), reasons


# ---------------------------------------------------------------------------
# Component B  — Race evidence quality
# ---------------------------------------------------------------------------

def _component_b(entries_df: pd.DataFrame) -> tuple[float, list[str]]:
    """Return (score 0–1, list[reason_string])."""
    reasons: list[str] = []
    n = len(entries_df)
    if n == 0:
        return 0.0, ["empty field"]

    # Field size: 4 runners → 0.0, 12+ runners → 1.0
    b_field = max(0.0, min(1.0, (n - 4) / 8.0))
    if n < 6:
        reasons.append(f"small field ({n})")
    elif n >= 10:
        reasons.append(f"solid field ({n})")   # positive

    # Morning-line coverage
    n_with_ml = int(
        (entries_df["morning_line_odds"].notna()
         & (pd.to_numeric(entries_df["morning_line_odds"], errors="coerce") > 0)).sum()
    )
    b_ml = n_with_ml / n
    if b_ml < 0.80:
        reasons.append(f"incomplete ML odds ({n_with_ml}/{n})")

    score = 0.50 * b_field + 0.50 * b_ml
    return round(score, 4), reasons


# ---------------------------------------------------------------------------
# Component C  — Model certainty
# ---------------------------------------------------------------------------

def _component_c(
    win_probs: np.ndarray,
    market_probs: np.ndarray,
) -> tuple[float, list[str]]:
    """Return (score 0–1, list[reason_string])."""
    reasons: list[str] = []
    n = len(win_probs)
    if n == 0:
        return 0.0, ["no probability data"]

    # Shannon entropy (lower = more concentrated = more certain)
    entropy = float(-np.sum(win_probs * np.log(np.maximum(win_probs, 1e-9))))
    uniform_entropy = float(np.log(n)) if n > 1 else 1.0
    c_entropy = max(0.0, 1.0 - entropy / uniform_entropy)

    if c_entropy >= 0.40:
        reasons.append("clear top-pick separation")
    elif c_entropy < 0.15:
        reasons.append("diffuse probability spread")

    # Top-1 vs top-2 gap (saturates at 10 percentage-point gap)
    sorted_probs = np.sort(win_probs)[::-1]
    gap_12 = float(sorted_probs[0] - sorted_probs[1]) if n >= 2 else float(sorted_probs[0])
    c_gap = min(gap_12 / 0.10, 1.0)

    # Model-vs-market favorite alignment
    model_rank1  = int(np.argmax(win_probs))
    market_rank1 = int(np.argmax(market_probs))
    c_align = 1.0 if model_rank1 == market_rank1 else 0.0
    if c_align:
        reasons.append("model-market aligned")
    else:
        reasons.append("model diverges from market fav")

    score = 0.45 * c_entropy + 0.35 * c_gap + 0.20 * c_align
    return round(score, 4), reasons


# ---------------------------------------------------------------------------
# Component D  — Calibration support (graceful degradation)
# ---------------------------------------------------------------------------

def _component_d() -> tuple[float, list[str]]:
    """Return neutral (0.50) when no outcomes history is available.

    Future extension: accept a conn and surface/distance/field-size slice,
    look up historical calibration accuracy, and return a data-driven score.
    """
    return 0.50, []   # omit from displayed reasons — generic noise when always absent


# ---------------------------------------------------------------------------
# Missing-flags  — backward compat
# ---------------------------------------------------------------------------

def legacy_missing_flags(dist_starts: int, derby_override: bool = False) -> str:
    """Reconstruct the comma-separated missing-data flag string for board output."""
    flags = list(CRITICAL_MISSING)
    if derby_override:
        flags.extend(DERBY_EXTRA_MISSING)
    if dist_starts <= 1:
        flags.append("dist_fit_single_start")
    return ",".join(flags)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_horse_confidence(
    feat_df:        pd.DataFrame,
    entries_df:     pd.DataFrame,
    win_probs:      np.ndarray,
    market_probs:   np.ndarray,
    model_features: list[str],
    derby_override: bool = False,
) -> pd.DataFrame:
    """Compute per-horse confidence scores for a race.

    Returns DataFrame with one row per entry (matched by entry_id):
      entry_id           int
      confidence_score   float  0.0–1.0
      confidence_bucket  str    LOW / MEDIUM / HIGH
      confidence_reasons str    semicolon-separated, top 3 drivers
      model_confidence   str    "low" / "medium" / "high"  (backward compat)
      confidence_flag    int    0=LOW, 1=MEDIUM-or-HIGH     (backward compat)
      missing_data_flags str    comma-separated flags        (backward compat)

    Weights
    -------
    A (horse evidence):  0.35
    B (race evidence):   0.25
    C (model certainty): 0.30
    D (calibration):     0.10
    """
    W_A, W_B, W_C, W_D = 0.35, 0.25, 0.30, 0.10

    # Race-level components computed once
    b_score, b_reasons = _component_b(entries_df)
    c_score, c_reasons = _component_c(win_probs, market_probs)
    d_score, _          = _component_d()

    check_cols = [c for c in model_features if c in feat_df.columns]

    rows: list[dict] = []
    for _, erow in entries_df.iterrows():
        eid = int(erow["entry_id"])

        frow = feat_df[feat_df["entry_id"] == eid]
        if frow.empty:
            # No feature data at all — conservative LOW
            rows.append({
                "entry_id":           eid,
                "confidence_score":   0.25,
                "confidence_bucket":  "LOW",
                "confidence_reasons": "no feature data",
                "model_confidence":   "low",
                "confidence_flag":    0,
                "missing_data_flags": legacy_missing_flags(0, derby_override),
            })
            continue

        fr = frow.iloc[0]

        # Null-feature check
        has_null = any(
            fr.get(c) is None
            or (isinstance(fr.get(c), float) and np.isnan(fr.get(c)))
            for c in check_cols
        )

        def _int0(v):  # noqa: E306 — defined inline for clarity
            return 0 if (v is None or (isinstance(v, float) and np.isnan(v))) else int(v)

        dist_starts   = _int0(erow.get("dist_starts"))
        career_starts = _int0(erow.get("career_starts"))

        # Component A (per-horse)
        a_score, a_reasons = _component_a(dist_starts, career_starts, has_null)

        # Aggregate
        raw_score = W_A * a_score + W_B * b_score + W_C * c_score + W_D * d_score

        # Prioritise per-horse (A) reasons first; append race-level where space allows
        all_reasons = a_reasons + b_reasons + c_reasons
        top_reasons = all_reasons[:3]
        reasons_str = "; ".join(top_reasons) if top_reasons else "sufficient evidence"

        # Bucket
        if raw_score < LOW_THRESHOLD:
            bucket = "LOW"
        elif raw_score < HIGH_THRESHOLD:
            bucket = "MEDIUM"
        else:
            bucket = "HIGH"

        model_conf = bucket.lower()  # "low" / "medium" / "high"
        conf_flag  = 0 if bucket == "LOW" else 1

        rows.append({
            "entry_id":           eid,
            "confidence_score":   round(raw_score, 3),
            "confidence_bucket":  bucket,
            "confidence_reasons": reasons_str,
            "model_confidence":   model_conf,
            "confidence_flag":    conf_flag,
            "missing_data_flags": legacy_missing_flags(dist_starts, derby_override),
        })

    return pd.DataFrame(rows)
