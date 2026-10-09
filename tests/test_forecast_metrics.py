"""Scoring rules, checked against hand-computed values."""
from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from src.analysis.forecast_metrics import (
    EPS, Race, bootstrap_mean_ci, calibration_table, check_forecast, chronological_folds, devig, paired_comparison,
    race_brier, race_log_loss, running_log_loss, summarize, top1_hit, winner_rank,
)


def test_devig_removes_the_overround_proportionally():
    p = devig([0.5, 0.4, 0.3])
    assert p == pytest.approx([5 / 12, 4 / 12, 3 / 12]) and sum(p) == pytest.approx(1.0)
    for bad in ([], [0.5, 0.0], [0.5, -0.1], [0.5, float("nan")]):
        with pytest.raises(ValueError):
            devig(bad)


def test_log_loss_brier_rank_and_top1_by_hand():
    p = [0.5, 0.3, 0.2]
    assert race_log_loss(p, 1) == pytest.approx(-math.log(0.3)) == pytest.approx(1.2039728)
    assert race_log_loss(p, 0) == pytest.approx(0.6931472)
    assert race_brier(p, 0) == pytest.approx(0.25 + 0.09 + 0.04)
    assert race_brier(p, 2) == pytest.approx(0.25 + 0.09 + 0.64)
    assert (winner_rank(p, 0), winner_rank(p, 1), winner_rank(p, 2)) == (1.0, 2.0, 3.0)
    assert (top1_hit(p, 0), top1_hit(p, 1)) == (1.0, 0.0)
    tie = [0.4, 0.4, 0.2]
    assert winner_rank(tie, 0) == 1.5 and top1_hit(tie, 0) == 0.5 and top1_hit(tie, 2) == 0.0


def test_a_zero_probability_on_the_winner_is_floored_not_infinite():
    assert race_log_loss([1.0, 0.0, 0.0], 1) == pytest.approx(-math.log(EPS))


def test_uniform_forecast_scores_ln_n_whatever_wins():
    for n in (2, 5, 14):
        for w in (0, n - 1):
            assert race_log_loss([1.0 / n] * n, w) == pytest.approx(math.log(n))


@pytest.mark.parametrize("probs,w", [([0.5, 0.4], 0), ([0.5, 0.5], 2), ([1.2, -0.2], 0), ([float("nan"), 1.0], 1)])
def test_invalid_forecasts_are_refused(probs, w):
    with pytest.raises(ValueError):
        check_forecast(probs, w)


def test_calibration_table_and_ece_by_hand():
    # four races, [0.6, 0.4] each; the favourite wins 3 of 4
    races = [Race(f"r{i}", date(2026, 1, 1 + i), 0 if i < 3 else 1, {"m": [0.6, 0.4]}) for i in range(4)]
    cal = calibration_table(races, "m")
    assert (cal["n_starters"], cal["n_winners"]) == (8, 4)
    by = {(b["lo"], round(b["hi"], 3)): b for b in cal["bins"] if b["n"]}
    low, high = by[(0.40, 0.60)], by[(0.60, 1.0)]
    assert (low["n"], low["mean_predicted"], low["observed_rate"]) == (4, pytest.approx(0.4), 0.25)
    assert (high["n"], high["mean_predicted"], high["observed_rate"]) == (4, pytest.approx(0.6), 0.75)
    assert cal["ece"] == pytest.approx((4 * 0.15 + 4 * 0.15) / 8)
    assert sum(b["n"] for b in cal["bins"]) == 8


def test_bootstrap_is_deterministic_and_brackets_the_mean():
    v = [random.Random(3).gauss(0.1, 0.5) for _ in range(5)] + list(range(-5, 5))
    a, b = bootstrap_mean_ci(v, seed=7), bootstrap_mean_ci(v, seed=7)
    assert a == b and a["lo"] <= a["mean"] <= a["hi"] and a["mean"] == pytest.approx(sum(v) / len(v))
    assert bootstrap_mean_ci(v, seed=8) != a
    flat = bootstrap_mean_ci([0.25] * 20)
    assert flat["lo"] == flat["hi"] == flat["mean"] == 0.25
    assert bootstrap_mean_ci([])["mean"] is None


def _pair_races(n, cand_p, ref_p):
    out = []
    for i in range(n):
        out.append(Race(f"r{i}", date(2026, 3, 1) + timedelta(days=i), 0,
                        {"cand": [cand_p, 1 - cand_p], "ref": [ref_p, 1 - ref_p]}))
    return out


def test_paired_verdicts():
    better = paired_comparison(_pair_races(40, 0.6, 0.4), "cand", "ref")
    assert better["verdict"] == "BETTER_THAN_REFERENCE" and better["delta_log_loss"]["mean"] == pytest.approx(math.log(0.4 / 0.6))
    assert paired_comparison(_pair_races(40, 0.4, 0.6), "cand", "ref")["verdict"] == "WORSE_THAN_REFERENCE"
    assert paired_comparison(_pair_races(40, 0.5, 0.5), "cand", "ref")["verdict"] == "NOT_DISTINGUISHABLE"
    few = paired_comparison(_pair_races(10, 0.6, 0.4), "cand", "ref")
    assert few["verdict"].startswith("INSUFFICIENT_DATA") and few["n_races"] == 10


def test_paired_comparison_uses_only_races_that_have_both_forecasts():
    races = _pair_races(35, 0.6, 0.4) + [Race("lonely", date(2027, 1, 1), 0, {"cand": [0.9, 0.1]})]
    assert paired_comparison(races, "cand", "ref")["n_races"] == 35
    assert summarize(races, "cand")["n_races"] == 36


def test_sample_size_needed_for_a_one_percent_gain_is_reported_when_there_is_noise():
    rng = random.Random(1)
    races = []
    for i in range(60):
        pr = rng.uniform(0.15, 0.5)
        pc = min(0.9, max(0.05, pr + rng.uniform(-0.05, 0.05)))
        races.append(Race(f"r{i}", date(2026, 3, 1) + timedelta(days=i), 0 if rng.random() < pr else 1,
                          {"cand": [pc, 1 - pc], "ref": [pr, 1 - pr]}))
    need = paired_comparison(races, "cand", "ref")["races_needed_to_detect_1pct_log_loss_gain"]
    assert isinstance(need, int) and need > 60


def test_running_log_loss_is_cumulative_in_date_order_over_common_races():
    races = [Race("b", date(2026, 2, 2), 0, {"a": [0.5, 0.5], "c": [0.8, 0.2]}),
             Race("a", date(2026, 2, 1), 1, {"a": [0.5, 0.5], "c": [0.8, 0.2]}),
             Race("x", date(2026, 2, 3), 0, {"a": [0.5, 0.5]})]
    run = running_log_loss(races, ["a", "c"])
    assert [r["race"] for r in run] == ["a", "b"]
    assert run[0]["c"] == pytest.approx(-math.log(0.2)) and run[1]["c"] == pytest.approx((-math.log(0.2) - math.log(0.8)) / 2)
    assert run[1]["a"] == pytest.approx(math.log(2))


def test_chronological_folds_never_train_on_the_future_or_share_a_race():
    rng = random.Random(5)
    dates = [date(2026, 1, 1) + timedelta(days=rng.randrange(0, 120)) for _ in range(150)]
    folds = chronological_folds(dates, n_folds=4, min_train=20, embargo_days=2)
    assert len(folds) == 4
    seen_test: set[int] = set()
    for train, test in folds:
        assert len(train) >= 20 and test and not (set(train) & set(test))
        assert max(dates[i] for i in train) < min(dates[i] for i in test) - timedelta(days=2)
        assert not (seen_test & set(test))
        seen_test |= set(test)
    assert [min(dates[i] for i in t) for _tr, t in folds] == sorted(min(dates[i] for i in t) for _tr, t in folds)
    assert folds == chronological_folds(dates, n_folds=4, min_train=20, embargo_days=2)       # deterministic


def test_folds_skip_blocks_without_enough_history_and_handle_empty_input():
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(10)]
    assert chronological_folds(dates, n_folds=3, min_train=50) == []
    assert chronological_folds([], n_folds=3, min_train=1) == []
    with pytest.raises(ValueError):
        chronological_folds(dates, n_folds=0, min_train=1)
