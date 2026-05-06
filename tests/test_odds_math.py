"""Unit tests for odds_math: conversions, devig, drift, edge, Kelly, tag."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'src'))

import pytest

from derbyedge.odds_math import (
    american_to_decimal,
    decimal_to_american,
    fractional_to_decimal,
    morningline_to_decimal,
    decimal_to_implied_prob,
    devig_proportional,
    devig_power,
    drift_pct,
    drift_direction,
    edge,
    expected_value,
    kelly_fraction,
    bet_tag,
)


# ---- Conversions ----------------------------------------------------------

def test_american_to_decimal_positive():
    assert american_to_decimal(400) == 5.0
    assert american_to_decimal(100) == 2.0


def test_american_to_decimal_negative():
    assert abs(american_to_decimal(-110) - 1.909091) < 1e-3
    assert american_to_decimal(-200) == 1.5


def test_decimal_to_american_roundtrip():
    for am in (-300, -110, 100, 250, 800):
        d = american_to_decimal(am)
        assert abs(decimal_to_american(d) - am) <= 1


def test_morningline_parsing():
    assert morningline_to_decimal('4-1') == 5.0
    assert morningline_to_decimal('5/2') == 3.5
    assert morningline_to_decimal('EVEN') == 2.0
    assert morningline_to_decimal('8') == 9.0
    assert morningline_to_decimal('') is None
    assert morningline_to_decimal(None) is None


# ---- Devig ----------------------------------------------------------------

def test_devig_proportional_sums_to_one():
    decs = [3.0, 4.0, 5.0, 8.0]   # implied 0.333+0.25+0.2+0.125 = 0.908 (no vig)
    out = devig_proportional(decs)
    assert all(p is not None for p in out)
    assert abs(sum(out) - 1.0) < 1e-5


def test_devig_proportional_with_overround():
    # 3-book overround case
    decs = [2.0, 3.0, 4.0]   # implied = 0.5 + 0.333 + 0.25 = 1.083 -> overround 8.3%
    out = devig_proportional(decs)
    assert abs(sum(out) - 1.0) < 1e-5
    # Ranks preserved
    assert out[0] > out[1] > out[2]


def test_devig_handles_nones():
    decs = [3.0, None, 5.0]
    out = devig_proportional(decs)
    assert out[1] is None
    assert abs((out[0] or 0) + (out[2] or 0) - 1.0) < 1e-5


def test_devig_power_sums_to_one():
    decs = [2.0, 3.0, 4.0, 6.0, 12.0]
    out = devig_power(decs)
    assert all(p is not None for p in out)
    assert abs(sum(out) - 1.0) < 1e-3


def test_devig_empty():
    assert devig_proportional([None, None]) == [None, None]


# ---- Drift ----------------------------------------------------------------

def test_drift_pct_drifting():
    assert drift_pct(5.0, 7.0) == 0.4   # +40% = drifting
    assert drift_direction(5.0, 7.0) == 'drifting'


def test_drift_pct_shortening():
    assert drift_pct(5.0, 3.0) == -0.4
    assert drift_direction(5.0, 3.0) == 'shortening'


def test_drift_flat():
    assert drift_direction(5.0, 5.2, flat_band=0.10) == 'flat'


def test_drift_unknown_inputs():
    assert drift_pct(None, 5.0) is None
    assert drift_direction(None, 5.0) == 'unknown'


# ---- Edge / EV / Kelly ----------------------------------------------------

def test_edge_positive():
    # Model says 25%, market says 20%
    assert edge(0.25, 0.20) == pytest.approx(0.25)


def test_edge_zero_market():
    assert edge(0.5, 0) == 0.0


def test_expected_value_positive():
    # 25% to win at 5.0 decimal = 25% * 4 - 75% = 0.25
    assert expected_value(0.25, 5.0) == pytest.approx(0.25)


def test_expected_value_breakeven():
    # 20% at 5.0 = 0.20*4 - 0.80 = 0
    assert expected_value(0.20, 5.0) == pytest.approx(0.0)


def test_kelly_fraction_capped():
    # Huge edge should still cap at 0.05
    assert kelly_fraction(0.5, 10.0, cap=0.05) == 0.05


def test_kelly_zero_when_negative_ev():
    assert kelly_fraction(0.10, 5.0) == 0.0


def test_kelly_handles_short_odds():
    assert kelly_fraction(0.5, 1.0) == 0.0


# ---- Bet tag --------------------------------------------------------------

def test_bet_tag_strong_play():
    # 30% model on 6.0 dec (mkt implied ~16.7%) = edge ~80%, EV positive
    tag = bet_tag(model_prob=0.30, market_prob=0.167, decimal_odds=6.0)
    assert tag == 'STRONG_PLAY'


def test_bet_tag_value_play():
    tag = bet_tag(model_prob=0.15, market_prob=0.10, decimal_odds=10.0)
    # edge = 50%, EV = 0.15*9 - 0.85 = 0.50  -> qualifies STRONG (>=40%) but model_prob only 0.15
    assert tag == 'STRONG_PLAY'


def test_bet_tag_watch():
    # Small edge
    tag = bet_tag(model_prob=0.22, market_prob=0.20, decimal_odds=5.0)
    assert tag == 'WATCH'


def test_bet_tag_fade():
    # Heavy favorite at 40% market, model says 20%
    tag = bet_tag(model_prob=0.20, market_prob=0.40, decimal_odds=2.5)
    assert tag == 'FADE'


def test_bet_tag_pass():
    # Slightly negative edge but not heavy enough to fade
    tag = bet_tag(model_prob=0.08, market_prob=0.10, decimal_odds=10.0)
    assert tag == 'PASS'
