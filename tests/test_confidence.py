"""Tests for the 4-component confidence scoring system.

Covers the spec requirements:
  1. Sparse distance history alone no longer forces LOW when other signals are strong.
  2. Genuinely sparse race still returns LOW.
  3. Strong separation + strong evidence returns MEDIUM or HIGH.
  4. Missing calibration history does not auto-fail confidence.
  5. Explanation strings reflect actual drivers.

Also covers:
  - Component-level unit tests for A, B, C, D.
  - Threshold boundary conditions.
  - Empty-field / missing-data edge cases.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.confidence import (
    LOW_THRESHOLD,
    HIGH_THRESHOLD,
    _component_a,
    _component_b,
    _component_c,
    _component_d,
    compute_horse_confidence,
    legacy_missing_flags,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_win_probs(n: int, top_prob: float | None = None) -> np.ndarray:
    if top_prob is None or n == 0:
        return np.full(n, 1.0 / n) if n else np.array([])
    remaining = max((1.0 - top_prob) / max(n - 1, 1), 0.0)
    probs = np.full(n, remaining)
    probs[0] = top_prob
    return probs / probs.sum()


def _entries(
    n: int,
    dist_starts_list: list[int],
    career_starts_list: list[int],
    ml_odds_list: list[float | None] | None = None,
) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append({
            "entry_id":       i + 1,
            "horse_name":     f"Horse{i + 1}",
            "post_position":  i + 1,
            "dist_starts":    dist_starts_list[i] if i < len(dist_starts_list) else 0,
            "career_starts":  career_starts_list[i] if i < len(career_starts_list) else 0,
            "morning_line_odds": (
                ml_odds_list[i] if ml_odds_list and i < len(ml_odds_list) else 5.0
            ),
        })
    return pd.DataFrame(rows)


def _feat(entry_ids: list[int], model_features: list[str], has_null: bool = False) -> pd.DataFrame:
    rows = []
    for eid in entry_ids:
        row = {"entry_id": eid}
        for feat in model_features:
            row[feat] = None if has_null else 0.5
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Component A unit tests
# ---------------------------------------------------------------------------

class TestComponentA:
    def test_zero_dist_starts_yields_low_A_dist(self):
        score, _ = _component_a(0, 20, False)
        # a_dist = 0.0 → A = 0.0*0.5 + 1.0*0.25 + 1.0*0.25 = 0.50
        assert score == pytest.approx(0.50, abs=1e-3)

    def test_four_dist_starts_yields_high_A(self):
        score, _ = _component_a(4, 20, False)
        # a_dist = 1.0 → A = 1.0*0.5 + 1.0*0.25 + 1.0*0.25 = 1.0
        assert score == pytest.approx(1.0, abs=1e-3)

    def test_null_features_penalises_A(self):
        score_no_null, _ = _component_a(3, 15, False)
        score_null, _    = _component_a(3, 15, True)
        assert score_null < score_no_null

    def test_limited_career_reduces_A(self):
        score_vet,  _ = _component_a(2, 20, False)
        score_green,_ = _component_a(2,  2, False)
        assert score_green < score_vet

    def test_reasons_mention_dist_when_zero(self):
        _, reasons = _component_a(0, 15, False)
        assert any("dist" in r.lower() for r in reasons)

    def test_reasons_mention_dist_when_one(self):
        _, reasons = _component_a(1, 15, False)
        assert any("1 dist" in r.lower() for r in reasons)

    def test_reasons_mention_veteran_when_career_high(self):
        _, reasons = _component_a(3, 20, False)
        assert any("veteran" in r.lower() for r in reasons)

    def test_reasons_mention_limited_career_when_few_starts(self):
        _, reasons = _component_a(2, 3, False)
        assert any("limited" in r.lower() or "career" in r.lower() for r in reasons)

    def test_reasons_mention_null_features(self):
        _, reasons = _component_a(2, 10, True)
        assert any("missing" in r.lower() or "feature" in r.lower() for r in reasons)


# ---------------------------------------------------------------------------
# Component B unit tests
# ---------------------------------------------------------------------------

class TestComponentB:
    def test_full_field_full_ml_yields_near_one(self):
        e = _entries(12, [2]*12, [10]*12)
        score, _ = _component_b(e)
        assert score >= 0.9

    def test_small_field_reduces_B(self):
        e_big   = _entries(10, [2]*10, [10]*10)
        e_small = _entries(4,  [2]*4,  [10]*4)
        s_big, _ = _component_b(e_big)
        s_sml, _ = _component_b(e_small)
        assert s_sml < s_big

    def test_missing_ml_reduces_B(self):
        e_full    = _entries(8, [2]*8, [10]*8, ml_odds_list=[5.0]*8)
        e_partial = _entries(8, [2]*8, [10]*8, ml_odds_list=[5.0, None, None, None, 5.0, 5.0, None, None])
        s_full, _ = _component_b(e_full)
        s_part, _ = _component_b(e_partial)
        assert s_part < s_full

    def test_small_field_reason_included(self):
        e = _entries(4, [2]*4, [10]*4)
        _, reasons = _component_b(e)
        assert any("small" in r.lower() for r in reasons)

    def test_solid_field_reason_included(self):
        e = _entries(12, [2]*12, [10]*12)
        _, reasons = _component_b(e)
        assert any("solid" in r.lower() for r in reasons)


# ---------------------------------------------------------------------------
# Component C unit tests
# ---------------------------------------------------------------------------

class TestComponentC:
    def test_uniform_probs_yield_low_C(self):
        n = 8
        wp = np.full(n, 1.0 / n)
        mp = np.full(n, 1.0 / n)
        score, _ = _component_c(wp, mp)
        assert score < 0.30

    def test_concentrated_probs_yield_higher_C(self):
        wp = _make_win_probs(8, top_prob=0.40)
        mp = _make_win_probs(8, top_prob=0.40)
        score_conc, _ = _component_c(wp, mp)
        wp_flat = np.full(8, 0.125)
        score_flat, _ = _component_c(wp_flat, mp)
        assert score_conc > score_flat

    def test_aligned_top_pick_boosts_C(self):
        wp = _make_win_probs(6, top_prob=0.25)
        mp = _make_win_probs(6, top_prob=0.25)   # same top → aligned
        wp_div = wp.copy()
        wp_div[0], wp_div[-1] = wp_div[-1], wp_div[0]   # flip top → diverge
        s_aligned, _ = _component_c(wp, mp)
        s_diverge, _ = _component_c(wp_div, mp)
        assert s_aligned > s_diverge

    def test_model_market_aligned_reason_present(self):
        wp = _make_win_probs(6, top_prob=0.30)
        mp = _make_win_probs(6, top_prob=0.30)
        _, reasons = _component_c(wp, mp)
        assert any("align" in r.lower() for r in reasons)

    def test_model_market_diverge_reason_present(self):
        wp = _make_win_probs(6, top_prob=0.30)
        mp = wp.copy()
        mp[0], mp[-1] = mp[-1], mp[0]
        _, reasons = _component_c(wp, mp)
        assert any("diverge" in r.lower() or "market" in r.lower() for r in reasons)


# ---------------------------------------------------------------------------
# Component D unit tests
# ---------------------------------------------------------------------------

class TestComponentD:
    def test_default_returns_half(self):
        score, reasons = _component_d()
        assert score == pytest.approx(0.50)
        assert isinstance(reasons, list)

    def test_does_not_crash(self):
        score, _ = _component_d()
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Integration: compute_horse_confidence
# ---------------------------------------------------------------------------

FEATS = ["distance_fit"]


class TestSparseDistNoLongerForcesLow:
    """Spec requirement 1: sparse distance alone must not force LOW when other signals are strong."""

    def test_zero_dist_starts_veteran_horse_large_field_is_not_low(self):
        n = 10
        e  = _entries(n, [0]*n, [20]*n)   # dist_starts=0, veteran 20+ starts
        f  = _feat(list(range(1, n+1)), FEATS, has_null=False)
        wp = _make_win_probs(n, top_prob=0.22)
        mp = _make_win_probs(n, top_prob=0.22)   # aligned

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        buckets = result["confidence_bucket"].tolist()
        assert all(b != "LOW" for b in buckets), (
            f"Expected no LOW despite dist_starts=0, but got: {buckets}"
        )

    def test_one_dist_start_strong_race_evidence_is_not_low(self):
        n = 12
        e  = _entries(n, [1]*n, [15]*n)
        f  = _feat(list(range(1, n+1)), FEATS, has_null=False)
        wp = _make_win_probs(n, top_prob=0.25)
        mp = _make_win_probs(n, top_prob=0.25)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        buckets = result["confidence_bucket"].tolist()
        assert all(b != "LOW" for b in buckets), (
            f"1 dist start + strong other signals must not yield LOW; got {buckets}"
        )


class TestGenuinelySparseRaceIsLow:
    """Spec requirement 2: genuinely sparse races still return LOW."""

    def test_sparse_everything_returns_low(self):
        # 4 runners, 2 with ML, null features, 0 dist starts, 2 career starts, uniform probs
        n = 4
        e = _entries(
            n,
            dist_starts_list=[0, 0, 1, 0],
            career_starts_list=[2, 3, 1, 2],
            ml_odds_list=[5.0, None, None, 8.0],
        )
        f  = _feat(list(range(1, n+1)), FEATS, has_null=True)
        wp = np.full(n, 1.0 / n)
        mp = np.full(n, 1.0 / n)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        buckets = result["confidence_bucket"].tolist()
        assert all(b == "LOW" for b in buckets), (
            f"Sparse race must return all LOW; got {buckets}"
        )

    def test_small_field_no_ml_uniform_probs_is_low(self):
        n = 3
        e = _entries(n, [0]*n, [3]*n, ml_odds_list=[None]*n)
        f = _feat(list(range(1, n+1)), FEATS, has_null=True)
        wp = np.full(n, 1.0 / n)
        mp = np.full(n, 1.0 / n)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        assert all(b == "LOW" for b in result["confidence_bucket"].tolist())


class TestStrongSeparationReturnsMediumOrHigh:
    """Spec requirement 3: strong separation + strong evidence → MEDIUM or HIGH."""

    def test_veteran_field_clear_top_pick_aligned(self):
        n = 8
        e  = _entries(n, [3]*n, [15]*n)
        f  = _feat(list(range(1, n+1)), FEATS, has_null=False)
        wp = _make_win_probs(n, top_prob=0.30)
        mp = _make_win_probs(n, top_prob=0.30)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        buckets = result["confidence_bucket"].tolist()
        assert all(b in ("MEDIUM", "HIGH") for b in buckets), (
            f"Expected MEDIUM/HIGH for strong separation + evidence; got {buckets}"
        )

    def test_high_dist_starts_aligned_large_field_returns_high(self):
        n = 10
        e  = _entries(n, [5]*n, [20]*n)
        f  = _feat(list(range(1, n+1)), FEATS, has_null=False)
        wp = _make_win_probs(n, top_prob=0.35)
        mp = _make_win_probs(n, top_prob=0.35)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        buckets = result["confidence_bucket"].tolist()
        # At least some should reach HIGH
        assert any(b == "HIGH" for b in buckets), (
            f"Expected at least one HIGH with very strong evidence; got {buckets}"
        )


class TestMissingCalibrationDoesNotFail:
    """Spec requirement 4: no calibration history → graceful, no crash, no collapse to LOW."""

    def test_no_crash_no_low_collapse(self):
        n = 6
        e  = _entries(n, [2]*n, [10]*n)
        f  = _feat(list(range(1, n+1)), FEATS, has_null=False)
        wp = _make_win_probs(n, top_prob=0.25)
        mp = _make_win_probs(n, top_prob=0.25)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        assert not result.empty
        assert result["confidence_score"].between(0.0, 1.0).all()
        # With moderate evidence the race must NOT collapse to LOW due to absent calibration
        assert not result["confidence_bucket"].eq("LOW").all(), (
            "Missing calibration history must not force all entries to LOW"
        )

    def test_scores_are_finite(self):
        n = 5
        e  = _entries(n, [1]*n, [8]*n)
        f  = _feat(list(range(1, n+1)), FEATS)
        wp = _make_win_probs(n, top_prob=0.20)
        mp = _make_win_probs(n)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        assert result["confidence_score"].apply(np.isfinite).all()


class TestReasonStrings:
    """Spec requirement 5: explanation strings reflect actual drivers."""

    def test_reasons_mention_dist_when_zero(self):
        n = 4
        e  = _entries(n, [0]*n, [15]*n)
        f  = _feat(list(range(1, n+1)), FEATS, has_null=False)
        wp = np.full(n, 0.25)
        mp = np.full(n, 0.25)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        reasons = result.iloc[0]["confidence_reasons"]
        assert "dist" in reasons.lower(), f"Expected dist reason; got: {reasons!r}"

    def test_reasons_mention_alignment_when_model_agrees(self):
        n = 8
        e  = _entries(n, [3]*n, [15]*n)
        f  = _feat(list(range(1, n+1)), FEATS, has_null=False)
        wp = _make_win_probs(n, top_prob=0.28)
        mp = _make_win_probs(n, top_prob=0.28)   # same top → aligned

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        reasons = result.iloc[0]["confidence_reasons"]
        assert "align" in reasons.lower() or "market" in reasons.lower(), (
            f"Expected alignment driver in reasons; got: {reasons!r}"
        )

    def test_reasons_mention_small_field(self):
        n = 4
        e  = _entries(n, [2]*n, [10]*n)
        f  = _feat(list(range(1, n+1)), FEATS)
        wp = _make_win_probs(n)
        mp = _make_win_probs(n)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        reasons = result.iloc[0]["confidence_reasons"]
        assert "small" in reasons.lower() or "field" in reasons.lower(), (
            f"Expected small-field reason; got: {reasons!r}"
        )

    def test_reasons_non_empty(self):
        n = 6
        e  = _entries(n, [2]*n, [10]*n)
        f  = _feat(list(range(1, n+1)), FEATS)
        wp = _make_win_probs(n, top_prob=0.20)
        mp = _make_win_probs(n, top_prob=0.20)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        for _, row in result.iterrows():
            assert row["confidence_reasons"].strip(), "confidence_reasons must not be empty"


# ---------------------------------------------------------------------------
# Threshold boundary conditions
# ---------------------------------------------------------------------------

class TestThresholds:
    def test_score_below_low_threshold_is_low(self):
        # Construct scenario guaranteed to score below LOW_THRESHOLD
        n = 3
        e = _entries(n, [0]*n, [1]*n, ml_odds_list=[None]*n)
        f = _feat(list(range(1, n+1)), FEATS, has_null=True)
        wp = np.full(n, 1.0 / n)
        mp = np.full(n, 1.0 / n)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        assert result["confidence_score"].iloc[0] < LOW_THRESHOLD
        assert result["confidence_bucket"].iloc[0] == "LOW"

    def test_high_threshold_consistent(self):
        assert HIGH_THRESHOLD > LOW_THRESHOLD

    def test_confidence_flag_zero_iff_low(self):
        n = 10
        e  = _entries(n, [3]*n, [15]*n)
        f  = _feat(list(range(1, n+1)), FEATS)
        wp = _make_win_probs(n, top_prob=0.28)
        mp = _make_win_probs(n, top_prob=0.28)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        for _, row in result.iterrows():
            if row["confidence_bucket"] == "LOW":
                assert row["confidence_flag"] == 0
            else:
                assert row["confidence_flag"] == 1

    def test_returns_required_columns(self):
        n = 5
        e  = _entries(n, [2]*n, [10]*n)
        f  = _feat(list(range(1, n+1)), FEATS)
        wp = _make_win_probs(n)
        mp = _make_win_probs(n)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        for col in (
            "entry_id", "confidence_score", "confidence_bucket",
            "confidence_reasons", "model_confidence", "confidence_flag",
            "missing_data_flags",
        ):
            assert col in result.columns, f"Missing column: {col}"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_feature_row_returns_low(self):
        n = 3
        e = _entries(n, [2]*n, [10]*n)
        f = pd.DataFrame({"entry_id": [], "distance_fit": []})  # empty feat df
        wp = _make_win_probs(n, top_prob=0.40)
        mp = _make_win_probs(n, top_prob=0.40)

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        assert (result["confidence_bucket"] == "LOW").all()

    def test_single_horse_field(self):
        n = 1
        e = _entries(n, [2], [10])
        f = _feat([1], FEATS)
        wp = np.array([1.0])
        mp = np.array([1.0])

        result = compute_horse_confidence(f, e, wp, mp, FEATS)
        assert len(result) == 1
        assert result["confidence_score"].iloc[0] == pytest.approx(
            result["confidence_score"].iloc[0], abs=1.0
        )   # just check it's a number

    def test_legacy_missing_flags_backward_compat(self):
        flags_low  = legacy_missing_flags(0)
        flags_high = legacy_missing_flags(5)
        assert "dist_fit_single_start" in flags_low
        assert "dist_fit_single_start" not in flags_high

    def test_legacy_missing_flags_derby(self):
        flags = legacy_missing_flags(0, derby_override=True)
        assert "no_jan_apr_curve" in flags
        assert "no_churchill_readiness" in flags
