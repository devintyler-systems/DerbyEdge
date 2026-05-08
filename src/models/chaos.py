"""
DerbyEdge — General-Race Chaos Module
src/models/chaos.py

PURPOSE
-------
Quantify the degree to which a race is "chaotic" — structurally prone
to a non-favorite winning — and surface longshot horses that have a realistic
upside case the seed baseline cannot resolve.

DESIGN PRINCIPLES
-----------------
1. Works entirely from pre-race, seed-available data (ML odds, field size,
   pace style, class, post position, layoff).
2. ADDITIVE to win_probs AFTER softmax calibration — does not replace the
   composite; redistributes residual probability mass toward qualified
   chaotic upsides.
3. Kill switch: chaos_boost is 0.0 when field_entropy_score < CHAOS_FLOOR.
4. Applies to ALL race types, not just the Derby.
5. Every output labeled with chaos_tier: NONE | LOW | MEDIUM | HIGH.

CHAOS COMPONENTS (race-level)
------------------------------
A. field_entropy_score   [0.0 – 1.0]
   Shannon entropy of market-implied probabilities, normalized by log2(n).
   High = wide-open field. Threshold >= CHAOS_FLOOR (0.72) activates chaos.

B. pace_chaos_flag       [0 or 1]
   1 when front_count >= 3 in sprint (<8.5f) OR >= 4 in route.
   Also 1 when front_count == 0 (no confirmed speed = unpredictable fractions).

C. class_compression_flag [0 or 1]
   1 when std(market_implied_prob) < 0.04 AND field_size >= 8.
   Indicates no standout — every horse looks similar to the market.

D. favorite_vulnerability  [0.0 – 1.0]
   1 - top_horse_implied_prob.

HORSE ELIGIBILITY
-----------------
All of:
  - ML odds >= LONGSHOT_ML_FLOOR (15)
  - career_win_pct > 0
  - bet_tag != 'underlay'
  - layoff_days <= LAYOFF_CAP (45)

CHAOS BOOST FORMULA
-------------------
  chaos_intensity    = entropy + 0.30*pace_chaos + 0.20*class_comp + 0.20*fav_vuln  (capped 1.0)
  horse_chaos_weight = softmax(1/ML_odds) over eligible horses
  raw_chaos_boost[i] = chaos_intensity * horse_chaos_weight[i] * BOOST_SCALE (0.06)

Cost absorbed proportionally from top-3 favorites. Renormalize to sum=1.

CT R9 RETROSPECTIVE
-------------------
Field entropy = 0.918 -> chaos ACTIVE at intensity 1.0
Rita The Redhead (31-1): base 4.63% -> adjusted 5.62% (fair 16.8-1 vs ML 31-1)
Value edge: +0.025 -> BET tier
"""

from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------
CHAOS_FLOOR       = 0.72   # min normalized entropy to activate chaos redistribution
LONGSHOT_ML_FLOOR = 15     # minimum ML odds to qualify as chaos candidate
LAYOFF_CAP        = 45     # max days since last race (None = treat as 0)
BOOST_SCALE       = 0.06   # max total probability mass redistributed
ENTROPY_PACE_W    = 0.30   # weight of pace_chaos in chaos_intensity
ENTROPY_CLASS_W   = 0.20   # weight of class_compression in chaos_intensity
ENTROPY_FAV_W     = 0.20   # weight of favorite_vulnerability in chaos_intensity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _shannon_entropy_normalized(probs: np.ndarray) -> float:
    """Normalized Shannon entropy H / log2(n). Returns [0.0, 1.0]."""
    n = len(probs)
    if n <= 1:
        return 0.0
    p = np.clip(probs, 1e-9, 1.0)
    p = p / p.sum()
    h = -np.sum(p * np.log2(p))
    return float(np.clip(h / np.log2(n), 0.0, 1.0))


def _chaos_tier(boost: float) -> str:
    if boost <= 0:    return "NONE"
    if boost < 0.005: return "LOW"
    if boost < 0.015: return "MEDIUM"
    return "HIGH"


# ---------------------------------------------------------------------------
# Race-level chaos context
# ---------------------------------------------------------------------------
def compute_race_chaos_context(
    market_implied_probs: np.ndarray,
    run_styles: list[Optional[str]],
    dist_furlongs: float,
) -> dict:
    """
    Compute race-level chaos context from pre-race data.

    Parameters
    ----------
    market_implied_probs : raw ML-implied probabilities (NOT normalized; overround OK)
    run_styles           : list of "front"|"presser"|"stalker"|"closer"|None per entry
    dist_furlongs        : race distance in furlongs

    Returns
    -------
    dict with keys:
        field_entropy_score, pace_chaos_flag, class_compression_flag,
        favorite_vulnerability, chaos_intensity, chaos_active
    """
    norm_probs = market_implied_probs / market_implied_probs.sum()

    field_entropy = _shannon_entropy_normalized(norm_probs)

    # Pace chaos
    front_count = sum(1 for s in run_styles if s == "front")
    is_sprint   = dist_furlongs < 8.5
    if is_sprint:
        pace_chaos = 1 if (front_count >= 3 or front_count == 0) else 0
    else:
        pace_chaos = 1 if (front_count >= 4 or front_count == 0) else 0

    # Class compression
    class_compression = 1 if (float(np.std(norm_probs)) < 0.04 and len(norm_probs) >= 8) else 0

    # Favorite vulnerability
    fav_vuln = float(1.0 - norm_probs.max())

    # Composite chaos intensity
    chaos_intensity = float(np.clip(
        field_entropy
        + ENTROPY_PACE_W  * pace_chaos
        + ENTROPY_CLASS_W * class_compression
        + ENTROPY_FAV_W   * fav_vuln,
        0.0, 1.0
    ))

    return {
        "field_entropy_score":    round(field_entropy,   4),
        "pace_chaos_flag":        pace_chaos,
        "class_compression_flag": class_compression,
        "favorite_vulnerability": round(fav_vuln,        4),
        "chaos_intensity":        round(chaos_intensity, 4),
        "chaos_active":           bool(field_entropy >= CHAOS_FLOOR),
    }


# ---------------------------------------------------------------------------
# Horse-level chaos scores
# ---------------------------------------------------------------------------
def compute_horse_chaos_scores(
    entries: pd.DataFrame,
    chaos_context: dict,
) -> pd.DataFrame:
    """
    Compute per-horse chaos eligibility, weight, boost, and tier.

    Parameters
    ----------
    entries : DataFrame with columns:
                morning_line_odds, career_win_pct, layoff_days, bet_tag
    chaos_context : output of compute_race_chaos_context()

    Returns
    -------
    entries copy with added columns:
        chaos_eligible (bool), chaos_score (float), chaos_boost (float), chaos_tier (str)
    """
    df = entries.copy()

    def _eligible(row) -> bool:
        if not chaos_context["chaos_active"]:
            return False
        ml  = float(row.get("morning_line_odds", 0) or 0)
        cwp = float(row.get("career_win_pct",    0) or 0)
        tag = str(row.get("bet_tag", "") or "")
        try:
            ld = int(row.get("layoff_days") or 0)
        except (TypeError, ValueError):
            ld = 0
        return (
            ml  >= LONGSHOT_ML_FLOOR
            and cwp  > 0.0
            and tag != "underlay"
            and ld   <= LAYOFF_CAP
        )

    df["chaos_eligible"] = df.apply(_eligible, axis=1)

    # Default: no chaos
    df["chaos_score"] = 0.0
    df["chaos_boost"] = 0.0
    df["chaos_tier"]  = "NONE"

    if not chaos_context["chaos_active"] or df["chaos_eligible"].sum() == 0:
        return df

    # Horse chaos weight: softmax of 1/ML_odds for eligible horses
    eligible_idx = df[df["chaos_eligible"]].index
    ml_eligible  = df.loc[eligible_idx, "morning_line_odds"].astype(float).values
    inv_ml       = 1.0 / np.clip(ml_eligible, 1.0, 1000.0)
    exp_inv      = np.exp(inv_ml - inv_ml.max())
    weights      = exp_inv / exp_inv.sum()

    df.loc[eligible_idx, "chaos_score"] = np.round(weights, 4)

    # Boost
    intensity = chaos_context["chaos_intensity"]
    df.loc[eligible_idx, "chaos_boost"] = np.round(
        intensity * df.loc[eligible_idx, "chaos_score"] * BOOST_SCALE, 5
    )

    # Tier
    df["chaos_tier"] = df["chaos_boost"].apply(_chaos_tier)

    return df


# ---------------------------------------------------------------------------
# Probability adjustment
# ---------------------------------------------------------------------------
def apply_chaos_adjustment(
    win_probs: np.ndarray,
    horse_chaos: pd.DataFrame,
    chaos_context: dict,
) -> np.ndarray:
    """
    Apply chaos boost additively post-calibration.
    Cost absorbed proportionally from top-3 favorites.
    Renormalize to sum=1.

    Parameters
    ----------
    win_probs    : calibrated win probabilities from build_seed_baseline()
    horse_chaos  : output of compute_horse_chaos_scores()
    chaos_context: output of compute_race_chaos_context()

    Returns
    -------
    np.ndarray of adjusted win probabilities, sum=1.0
    """
    if not chaos_context["chaos_active"]:
        return win_probs

    boosts      = horse_chaos["chaos_boost"].values
    total_boost = boosts.sum()

    if total_boost <= 0:
        return win_probs

    adjusted = win_probs.copy()

    # Cost from top-3 by win probability
    top3_idx  = np.argsort(adjusted)[::-1][:3]
    top3_mass = adjusted[top3_idx].sum()
    for i in top3_idx:
        cost_share = adjusted[i] / top3_mass if top3_mass > 0 else 1.0 / 3.0
        adjusted[i] = max(0.001, adjusted[i] - total_boost * cost_share)

    adjusted += boosts
    adjusted  = adjusted / adjusted.sum()

    return np.round(adjusted, 6)


# ---------------------------------------------------------------------------
# Convenience: full chaos pipeline for a scored race
# ---------------------------------------------------------------------------
def run_chaos_pipeline(
    win_probs: np.ndarray,
    entries_df: pd.DataFrame,
    dist_furlongs: float,
) -> tuple[np.ndarray, pd.DataFrame, dict]:
    """
    Full chaos pipeline: context -> horse scores -> adjusted probs.

    Caller supplies:
        win_probs    : calibrated probs from scorer.py
        entries_df   : v_entries_live rows with morning_line_odds, career_win_pct,
                       layoff_days (last_race_days), bet_tag, pace_style
        dist_furlongs: from race_cards.distance_furlongs

    Returns
    -------
    adjusted_win_probs : np.ndarray
    horse_chaos_df     : entries_df copy with chaos columns added
    chaos_context      : race-level chaos dict
    """
    market_implied = entries_df["market_implied_prob"].astype(float).values
    run_styles     = entries_df.get("run_style_bucket",
                     entries_df.get("pace_style", pd.Series([None]*len(entries_df)))
                     ).tolist()

    # Map layoff col name
    if "layoff_days" not in entries_df.columns and "last_race_days" in entries_df.columns:
        entries_df = entries_df.copy()
        entries_df["layoff_days"] = entries_df["last_race_days"]

    chaos_context = compute_race_chaos_context(market_implied, run_styles, dist_furlongs)
    horse_chaos   = compute_horse_chaos_scores(entries_df, chaos_context)
    adj_probs     = apply_chaos_adjustment(win_probs, horse_chaos, chaos_context)

    return adj_probs, horse_chaos, chaos_context
