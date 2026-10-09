"""Scoring rules for win-probability forecasts of one-winner races.  Pure functions, no database.

A forecast for a race is a list of win probabilities, one per starter, summing to 1.  The race has exactly
one winner.  Everything is computed per race first (so a 14-horse race does not outweigh a 5-horse race) and then
averaged over races.

* log loss     ``-ln p(winner)`` (the multinomial log loss; probabilities are floored at ``EPS``)
* Brier        ``sum_i (p_i - y_i)^2`` over the starters of the race
* top-1 / rank whether the winner had the highest probability, and its (tie-averaged) rank
* calibration  starter-level: predicted vs observed win rate in fixed probability bins, and the weighted
               mean absolute gap (ECE)
* paired CI    a deterministic bootstrap over races of the per-race difference between two forecasts
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Sequence

import numpy as np

EPS = 1e-6
CALIBRATION_EDGES = (0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.60, 1.0000001)


def devig(raw: Sequence[float]) -> list[float]:
    """Proportional removal of the overround: ``p_i = raw_i / sum(raw)``."""
    values = [float(x) for x in raw]
    if not values or any((not math.isfinite(x)) or x <= 0 for x in values):
        raise ValueError("every raw probability must be finite and positive")
    total = sum(values)
    return [x / total for x in values]


def check_forecast(probs: Sequence[float], winner_idx: int) -> None:
    if not 0 <= winner_idx < len(probs):
        raise ValueError("winner index out of range")
    if any((not math.isfinite(p)) or p < 0 for p in probs):
        raise ValueError("probabilities must be finite and non-negative")
    if abs(sum(probs) - 1.0) > 1e-6:
        raise ValueError(f"probabilities must sum to 1, got {sum(probs):.6f}")


def race_log_loss(probs: Sequence[float], winner_idx: int) -> float:
    check_forecast(probs, winner_idx)
    return -math.log(max(probs[winner_idx], EPS))


def race_brier(probs: Sequence[float], winner_idx: int) -> float:
    check_forecast(probs, winner_idx)
    return sum((p - (1.0 if i == winner_idx else 0.0)) ** 2 for i, p in enumerate(probs))


def winner_rank(probs: Sequence[float], winner_idx: int) -> float:
    """1 = highest probability; ties share the average of the ranks they occupy."""
    check_forecast(probs, winner_idx)
    w = probs[winner_idx]
    above = sum(1 for p in probs if p > w + 1e-12)
    tied = sum(1 for p in probs if abs(p - w) <= 1e-12)
    return above + (tied + 1) / 2.0


def top1_hit(probs: Sequence[float], winner_idx: int) -> float:
    """1 if the winner had the strictly highest probability, 1/k if it tied for highest with k horses, else 0."""
    check_forecast(probs, winner_idx)
    top = max(probs)
    if probs[winner_idx] < top - 1e-12:
        return 0.0
    return 1.0 / sum(1 for p in probs if abs(p - top) <= 1e-12)


@dataclass(frozen=True)
class Race:
    """One gradable race: per-forecast starter probabilities (same starter order) and the winner's index."""
    key: str
    when: date
    winner_idx: int
    forecasts: dict[str, list[float]]


def per_race_scores(races: Sequence[Race], name: str) -> dict[str, np.ndarray]:
    rows = [r for r in races if name in r.forecasts]
    return {
        "log_loss": np.array([race_log_loss(r.forecasts[name], r.winner_idx) for r in rows]),
        "brier": np.array([race_brier(r.forecasts[name], r.winner_idx) for r in rows]),
        "top1": np.array([top1_hit(r.forecasts[name], r.winner_idx) for r in rows]),
        "rank": np.array([winner_rank(r.forecasts[name], r.winner_idx) for r in rows]),
    }


def summarize(races: Sequence[Race], name: str) -> dict[str, float | int | None]:
    s = per_race_scores(races, name)
    n = len(s["log_loss"])
    if n == 0:
        return {"n_races": 0, "log_loss": None, "brier": None, "top1": None, "mean_winner_rank": None}
    return {"n_races": n, "log_loss": float(s["log_loss"].mean()), "brier": float(s["brier"].mean()),
            "top1": float(s["top1"].mean()), "mean_winner_rank": float(s["rank"].mean())}


def calibration_table(races: Sequence[Race], name: str, edges: Sequence[float] = CALIBRATION_EDGES) -> dict:
    p, y = [], []
    for r in races:
        if name in r.forecasts:
            for i, pi in enumerate(r.forecasts[name]):
                p.append(pi)
                y.append(1.0 if i == r.winner_idx else 0.0)
    p_arr, y_arr = np.array(p), np.array(y)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (p_arr >= lo) & (p_arr < hi)
        n = int(mask.sum())
        rows.append({"lo": lo, "hi": min(hi, 1.0), "n": n,
                     "mean_predicted": float(p_arr[mask].mean()) if n else None,
                     "observed_rate": float(y_arr[mask].mean()) if n else None})
    total = len(p_arr)
    ece = (sum(r["n"] * abs(r["mean_predicted"] - r["observed_rate"]) for r in rows if r["n"]) / total) if total else None
    return {"n_starters": total, "n_winners": int(y_arr.sum()), "ece": ece, "bins": rows}


def bootstrap_mean_ci(values: Sequence[float], *, n_boot: int = 2000, seed: int = 0, alpha: float = 0.05) -> dict:
    """Mean and a percentile bootstrap interval, resampling races.  Deterministic for a given seed."""
    v = np.asarray(values, dtype=float)
    n = len(v)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None, "sd": None}
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, n, size=(n_boot, n))].mean(axis=1)
    return {"n": n, "mean": float(v.mean()), "sd": float(v.std(ddof=1)) if n > 1 else 0.0,
            "lo": float(np.quantile(means, alpha / 2)), "hi": float(np.quantile(means, 1 - alpha / 2))}


MIN_RACES_FOR_VERDICT = 30


def paired_comparison(races: Sequence[Race], candidate: str, reference: str, *, n_boot: int = 2000, seed: int = 0) -> dict:
    """Candidate minus reference on the races where both forecasts exist.  Negative = candidate is better."""
    both = [r for r in races if candidate in r.forecasts and reference in r.forecasts]
    cand, ref = per_race_scores(both, candidate), per_race_scores(both, reference)
    d_ll = cand["log_loss"] - ref["log_loss"]
    d_br = cand["brier"] - ref["brier"]
    ll = bootstrap_mean_ci(d_ll, n_boot=n_boot, seed=seed)
    br = bootstrap_mean_ci(d_br, n_boot=n_boot, seed=seed + 1)
    if len(both) < MIN_RACES_FOR_VERDICT:
        verdict = f"INSUFFICIENT_DATA (n={len(both)} < {MIN_RACES_FOR_VERDICT})"
    elif ll["hi"] < 0:
        verdict = "BETTER_THAN_REFERENCE"
    elif ll["lo"] > 0:
        verdict = "WORSE_THAN_REFERENCE"
    else:
        verdict = "NOT_DISTINGUISHABLE"
    needed = None
    if len(both) > 2 and ll["sd"] and ref["log_loss"].mean() > 0:
        target = 0.01 * float(ref["log_loss"].mean())          # a 1% log-loss improvement
        needed = int(math.ceil((1.96 * ll["sd"] / target) ** 2))
    return {"candidate": candidate, "reference": reference, "n_races": len(both),
            "candidate_summary": summarize(both, candidate), "reference_summary": summarize(both, reference),
            "delta_log_loss": ll, "delta_brier": br, "verdict": verdict,
            "races_needed_to_detect_1pct_log_loss_gain": needed}


def running_log_loss(races: Sequence[Race], names: Sequence[str]) -> list[dict]:
    """Cumulative mean log loss in time order over the races where every named forecast exists."""
    ordered = sorted((r for r in races if all(n in r.forecasts for n in names)), key=lambda r: (r.when, r.key))
    sums = {n: 0.0 for n in names}
    out = []
    for i, r in enumerate(ordered, start=1):
        row = {"n": i, "race": r.key, "date": r.when.isoformat()}
        for n in names:
            sums[n] += race_log_loss(r.forecasts[n], r.winner_idx)
            row[n] = sums[n] / i
        out.append(row)
    return out


def chronological_folds(
    dates: Sequence[date], *, n_folds: int, min_train: int, embargo_days: int = 1,
) -> list[tuple[list[int], list[int]]]:
    """Expanding-window folds over race indexes.  The test block is a contiguous run of dates; the training set is
    every race strictly earlier than ``first_test_date - embargo_days`` (same-week track and form effects must
    not leak), and always has at least ``min_train`` races.  No random splitting."""
    if n_folds < 1:
        raise ValueError("n_folds must be >= 1")
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    uniq = sorted({dates[i] for i in order})
    folds: list[tuple[list[int], list[int]]] = []
    if not uniq:
        return folds
    per = max(1, math.ceil(len(uniq) / (n_folds + 1)))           # the first block is only ever training data
    for k in range(1, n_folds + 1):
        block = set(uniq[k * per:(k + 1) * per]) if k < n_folds else set(uniq[k * per:])
        if not block:
            continue
        test_start = min(block)
        cutoff = test_start.toordinal() - embargo_days
        train = [i for i in order if dates[i].toordinal() < cutoff]
        test = [i for i in order if dates[i] in block]
        if len(train) >= min_train and test:
            folds.append((train, test))
    return folds
