"""Odds math: conversions, devigging, drift detection.

All functions are pure and unit-testable. No DB, no I/O.

Conventions:
- decimal_odds: 1 + (stake_return_ratio). 4-1 = decimal 5.0. Even money = 2.0.
- american_odds: standard US format; +400 = decimal 5.0; -110 = decimal 1.909.
- implied_prob: 1 / decimal_odds. Sums to >1 over a book's runners (overround).
- devig: redistributes overround proportionally. Sum of devigged probs = 1.
"""
from __future__ import annotations

import math
from typing import Iterable


# ---- Conversions -----------------------------------------------------------

def american_to_decimal(american: int | None) -> float | None:
    if american is None:
        return None
    if american == 0:
        return None
    if american > 0:
        return round(1 + american / 100.0, 6)
    return round(1 + 100.0 / abs(american), 6)


def decimal_to_american(decimal: float | None) -> int | None:
    if decimal is None or decimal <= 1.0:
        return None
    if decimal >= 2.0:
        return int(round((decimal - 1.0) * 100))
    return int(round(-100.0 / (decimal - 1.0)))


def fractional_to_decimal(num: int, den: int) -> float | None:
    """e.g., 4-1 -> 5.0, 5-2 -> 3.5."""
    if den <= 0:
        return None
    return round(1 + num / den, 6)


def morningline_to_decimal(ml: str | None) -> float | None:
    """Parse '4-1', '5/2', '8' (=8-1), 'EVEN' to decimal odds."""
    if not ml:
        return None
    s = ml.strip().upper().replace(' ', '')
    if s in ('EVEN', 'EVENS', 'EV'):
        return 2.0
    for sep in ('-', '/', ':'):
        if sep in s:
            a, b = s.split(sep, 1)
            try:
                return fractional_to_decimal(int(a), int(b))
            except ValueError:
                return None
    try:
        return fractional_to_decimal(int(s), 1)
    except ValueError:
        return None


def decimal_to_implied_prob(decimal: float | None) -> float | None:
    if decimal is None or decimal <= 1.0:
        return None
    return 1.0 / decimal


# ---- Devig -----------------------------------------------------------------

def devig_proportional(decimals: list[float | None]) -> list[float | None]:
    """Multiplicative devig: each prob = raw_prob / overround_sum."""
    raws = [(i, decimal_to_implied_prob(d)) for i, d in enumerate(decimals)]
    valid = [(i, p) for i, p in raws if p is not None and p > 0]
    if not valid:
        return [None] * len(decimals)
    total = sum(p for _, p in valid)
    if total <= 0:
        return [None] * len(decimals)
    out: list[float | None] = [None] * len(decimals)
    for i, p in valid:
        out[i] = round(p / total, 6)
    return out


def devig_power(decimals: list[float | None], tol: float = 1e-6,
                max_iter: int = 60) -> list[float | None]:
    """Power devig: find k s.t. sum(prob_i^k) = 1."""
    raws = [(i, decimal_to_implied_prob(d)) for i, d in enumerate(decimals)]
    valid = [(i, p) for i, p in raws if p is not None and p > 0]
    if not valid:
        return [None] * len(decimals)
    probs = [p for _, p in valid]
    lo, hi = 0.5, 2.0
    for _ in range(max_iter):
        k = 0.5 * (lo + hi)
        s = sum(p ** k for p in probs)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    out: list[float | None] = [None] * len(decimals)
    for (i, p), _ in zip(valid, probs):
        out[i] = round(p ** k, 6)
    return out


# ---- Drift -----------------------------------------------------------------

def drift_pct(opening_dec: float | None, current_dec: float | None) -> float | None:
    """Positive = drifting (longer odds); negative = shortening."""
    if opening_dec is None or current_dec is None or opening_dec <= 1.0:
        return None
    return round((current_dec - opening_dec) / opening_dec, 4)


def drift_direction(opening_dec: float | None, current_dec: float | None,
                    flat_band: float = 0.10) -> str:
    pct = drift_pct(opening_dec, current_dec)
    if pct is None:
        return 'unknown'
    if pct > flat_band:
        return 'drifting'
    if pct < -flat_band:
        return 'shortening'
    return 'flat'


# ---- Edge calc / Kelly -----------------------------------------------------

def edge(model_prob: float, market_prob: float) -> float:
    """Multiplicative edge: (model - market) / market. >0 = positive EV."""
    if market_prob <= 0:
        return 0.0
    return (model_prob - market_prob) / market_prob


def expected_value(model_prob: float, decimal_odds: float) -> float:
    """EV per $1 stake. Positive = +EV."""
    if decimal_odds <= 1.0:
        return -1.0
    return model_prob * (decimal_odds - 1.0) - (1.0 - model_prob)


def kelly_fraction(model_prob: float, decimal_odds: float, cap: float = 0.05) -> float:
    """Fractional Kelly bankroll allocation, capped (default 5% ≈ quarter-Kelly for racing).

    f* = (b*p - q) / b  where b = decimal-1, p = model_prob, q = 1-p.
    """
    if decimal_odds <= 1.0:
        return 0.0
    b = decimal_odds - 1.0
    p = model_prob
    q = 1.0 - p
    f = (b * p - q) / b
    if f <= 0:
        return 0.0
    return round(min(f, cap), 4)


def kelly_fraction_full(model_prob: float, decimal_odds: float) -> float:
    """Full (uncapped) Kelly fraction f* = (b*p - q)/b, floored at 0.

    Use this when you want to apply your own fractional multiplier rather
    than a hard cap.  Returns 0.0 for -EV bets or invalid odds.
    """
    if decimal_odds <= 1.0:
        return 0.0
    b = decimal_odds - 1.0
    f = (b * model_prob - (1.0 - model_prob)) / b
    return round(max(f, 0.0), 6)


# ---- Bet denomination / playable stake rounding ----------------------------

# (min_bet, step) in USD. Conservative: always round DOWN to nearest step.
# WIN/PLACE/SHOW use $2 pari-mutuel minimum; exotics vary by track.
BET_DENOMINATION: dict[str, tuple[float, float]] = {
    "WIN":        (2.00, 2.00),
    "PLACE":      (2.00, 2.00),
    "SHOW":       (2.00, 2.00),
    "EXACTA":     (1.00, 1.00),
    "TRIFECTA":   (0.50, 0.50),
    "SUPERFECTA": (0.10, 0.10),
    "DEFAULT":    (1.00, 1.00),
}


def recommend_bet_size(
    raw_kelly_stake: float,
    bet_type: str = "WIN",
) -> dict:
    """Round a raw Kelly dollar stake to the nearest valid betting denomination.

    Conservative rounding: floors to the nearest allowed increment so the
    recommended amount is always achievable at the window.

    Returns a dict:
      raw_stake      — input stake (unmodified)
      rounded_stake  — floored to nearest step; 0.0 when below minimum
      min_bet        — track minimum for this bet type
      step           — valid increment for this bet type
      is_bettable    — True when rounded_stake >= min_bet
      recommendation — human-readable: "$2.00" or "PASS"
    """
    key = bet_type.upper()
    min_bet, step = BET_DENOMINATION.get(key, BET_DENOMINATION["DEFAULT"])

    if raw_kelly_stake < min_bet:
        return {
            "raw_stake":      round(raw_kelly_stake, 4),
            "rounded_stake":  0.0,
            "min_bet":        min_bet,
            "step":           step,
            "is_bettable":    False,
            "recommendation": "PASS",
        }

    # Floor to nearest step (avoid float drift with epsilon guard)
    steps_down = int(raw_kelly_stake / step + 1e-9)
    rounded = round(steps_down * step, 2)
    if rounded > raw_kelly_stake + 1e-9:
        rounded = round((steps_down - 1) * step, 2)

    return {
        "raw_stake":      round(raw_kelly_stake, 4),
        "rounded_stake":  rounded,
        "min_bet":        min_bet,
        "step":           step,
        "is_bettable":    rounded >= min_bet,
        "recommendation": f"${rounded:.2f}",
    }


def bet_tag(model_prob: float, market_prob: float, decimal_odds: float,
            min_edge: float = 0.20, strong_edge: float = 0.40) -> str:
    e = edge(model_prob, market_prob)
    ev = expected_value(model_prob, decimal_odds)
    if e >= strong_edge and ev > 0 and model_prob >= 0.06:
        return 'STRONG_PLAY'
    if e >= min_edge and ev > 0:
        return 'VALUE_PLAY'
    if e > 0:
        return 'WATCH'
    if market_prob >= 0.10 and model_prob < market_prob * 0.7:
        return 'FADE'
    return 'PASS'
