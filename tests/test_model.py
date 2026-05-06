"""Tests for model pipeline + race_softmax."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'src'))

import numpy as np
import pandas as pd
import pytest

from derbyedge.model import (
    make_pipeline, fit_by_surface, predict_by_surface, race_softmax,
    NUMERIC_FEATURES, CATEGORICAL_FEATURES,
)


def _toy_train():
    rng = np.random.default_rng(42)
    n = 200
    df = pd.DataFrame({
        'pp_surface': rng.choice(['D', 'T'], size=n, p=[0.7, 0.3]),
        'pp_dist_bucket': rng.choice(['sprint', 'mid', 'route'], size=n),
        'pp_race_type': rng.choice(['MSW', 'CLM', 'ALW'], size=n),
        'run_style': rng.choice(['E', 'EP', 'P', 'S'], size=n),
        'n_starts': rng.integers(1, 30, size=n),
        'n_starts_today_surface': rng.integers(0, 20, size=n),
        'n_starts_today_distbucket': rng.integers(0, 15, size=n),
        'avg_speed_last3': rng.normal(80, 10, size=n),
        'best_speed_last3': rng.normal(85, 10, size=n),
        'speed_trend_slope': rng.normal(0, 0.5, size=n),
        'pace2_avg_last3': rng.normal(80, 10, size=n),
        'class_avg_last3': rng.normal(80, 10, size=n),
        'finish_energy_raw': rng.normal(0, 2, size=n),
        'days_since_last': rng.integers(7, 200, size=n),
        'layoff_flag': rng.integers(0, 2, size=n),
        'pp_purse_usa': rng.uniform(20000, 200000, size=n),
        'pp_n_starters': rng.integers(5, 14, size=n),
        # Connection priors (v0.3)
        'jockey_winrate_prior': rng.uniform(0.05, 0.25, size=n),
        'jockey_winrate_surface': rng.uniform(0.05, 0.25, size=n),
        'trainer_winrate_prior': rng.uniform(0.05, 0.25, size=n),
        'trainer_winrate_surface': rng.uniform(0.05, 0.25, size=n),
        'jt_combo_winrate_prior': rng.uniform(0.05, 0.25, size=n),
        'jockey_starts_prior': rng.integers(0, 500, size=n),
        'trainer_starts_prior': rng.integers(0, 500, size=n),
        'jt_combo_starts_prior': rng.integers(0, 200, size=n),
        # Label correlated with avg_speed_last3 so model has signal to learn
    })
    df['won'] = (df['avg_speed_last3'] + rng.normal(0, 5, size=n) > 85).astype(int)
    return df


def test_pipeline_builds_and_fits():
    df = _toy_train()
    clf = make_pipeline(calibrated=False)
    X = df[NUMERIC_FEATURES + CATEGORICAL_FEATURES]
    y = df['won'].astype(int)
    clf.fit(X, y)
    p = clf.predict_proba(X)[:, 1]
    assert len(p) == len(df)
    assert (p >= 0).all() and (p <= 1).all()


def test_fit_by_surface_returns_models_per_surface():
    df = _toy_train()
    models = fit_by_surface(df, calibrated=False, min_per_surface=20)
    assert 'D' in models
    assert 'T' in models


def test_predict_by_surface_routes_correctly():
    df = _toy_train()
    models = fit_by_surface(df, calibrated=False, min_per_surface=20)
    p = predict_by_surface(models, df, fallback_prob=0.10)
    assert len(p) == len(df)
    assert (p >= 0).all() and (p <= 1).all()


def test_predict_unknown_surface_uses_fallback():
    df = _toy_train()
    models = fit_by_surface(df, calibrated=False, min_per_surface=20)
    novel = df.iloc[:5].copy()
    novel['pp_surface'] = 'X'
    p = predict_by_surface(models, novel, fallback_prob=0.123)
    np.testing.assert_array_equal(p, np.full(5, 0.123))


def test_race_softmax_sums_to_one_per_race():
    p = np.array([0.10, 0.05, 0.20, 0.15, 0.25, 0.30])
    rid = np.array(['R1', 'R1', 'R1', 'R2', 'R2', 'R2'])
    out = race_softmax(p, rid)
    assert abs(out[:3].sum() - 1.0) < 1e-6
    assert abs(out[3:].sum() - 1.0) < 1e-6


def test_race_softmax_preserves_rank():
    p = np.array([0.10, 0.20, 0.05])
    rid = np.array(['R1', 'R1', 'R1'])
    out = race_softmax(p, rid)
    # Index 1 had highest raw -> should have highest softmax
    assert out.argmax() == 1


def test_race_softmax_temperature_flattens():
    p = np.array([0.10, 0.30, 0.60])
    rid = np.array(['R1', 'R1', 'R1'])
    sharp = race_softmax(p, rid, temperature=0.5)
    flat = race_softmax(p, rid, temperature=2.0)
    # Higher temp -> closer to uniform (1/3 each)
    assert flat.std() < sharp.std()


def test_model_determinism():
    """Same training data -> same predictions."""
    df = _toy_train()
    m1 = fit_by_surface(df, calibrated=False, min_per_surface=20)
    m2 = fit_by_surface(df, calibrated=False, min_per_surface=20)
    p1 = predict_by_surface(m1, df)
    p2 = predict_by_surface(m2, df)
    np.testing.assert_allclose(p1, p2, rtol=1e-6)
