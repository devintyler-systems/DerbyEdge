"""Tests for evaluation metrics and backtest splits."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'src'))

import numpy as np
import pandas as pd
import pytest

from derbyedge.evaluation import (
    log_loss, brier_score, auc_roc, calibration_table,
    expected_calibration_error, odds_int_to_decimal, roi_flat_bets,
    summarize,
)
from derbyedge.backtest import (
    make_quarterly_folds, make_yearly_folds, run_backtest,
)


# ---- Metric correctness ----------------------------------------------------

def test_log_loss_perfect():
    y = np.array([1, 0, 1, 0])
    p = np.array([0.999, 0.001, 0.999, 0.001])
    assert log_loss(y, p) < 0.01


def test_log_loss_constant_base_rate():
    # All predictions 0.25; 25% positives -> matches base rate
    y = np.array([1, 0, 0, 0])
    p = np.full(4, 0.25)
    expected = -(0.25*np.log(0.25) + 0.75*np.log(0.75))
    assert abs(log_loss(y, p) - expected) < 1e-6


def test_brier_zero_perfect():
    y = np.array([1, 0, 1, 0])
    assert brier_score(y, y.astype(float)) == 0.0


def test_auc_perfect_separation():
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.2, 0.8, 0.9])
    assert auc_roc(y, p) == 1.0


def test_auc_random_predictions():
    np.random.seed(7)
    y = np.random.binomial(1, 0.5, size=500)
    p = np.random.uniform(0, 1, size=500)
    auc = auc_roc(y, p)
    assert 0.40 < auc < 0.60   # basically random


def test_auc_handles_no_positives():
    y = np.zeros(10)
    p = np.linspace(0, 1, 10)
    assert np.isnan(auc_roc(y, p))


def test_calibration_table_shape():
    np.random.seed(3)
    y = np.random.binomial(1, 0.3, size=200)
    p = np.random.uniform(0, 1, size=200)
    cal = calibration_table(y, p, n_bins=10)
    assert len(cal) == 10
    assert cal['n'].sum() == 200


def test_ece_well_calibrated_low():
    # Predictions equal true rate exactly within each bin -> ECE near 0
    y = np.array([0]*50 + [1]*50)
    p = np.array([0.0]*50 + [1.0]*50)
    assert expected_calibration_error(y, p, n_bins=2) < 0.01


# ---- Odds conversions ------------------------------------------------------

def test_odds_int_to_decimal_basic():
    # Equibase odds_int = decimal odds * 100
    assert odds_int_to_decimal(500) == 5.0
    assert odds_int_to_decimal(1000) == 10.0


def test_odds_int_to_decimal_invalid():
    assert odds_int_to_decimal(None) is None
    assert odds_int_to_decimal(0) is None
    assert odds_int_to_decimal(50) is None  # decimal would be 0.5, invalid


# ---- ROI -------------------------------------------------------------------

def test_roi_flat_bets_breakeven_at_fair():
    # Model perfectly matches market -> edge = 0 -> with edge_threshold=0 we bet
    y = np.array([1, 0, 0, 0])
    d = np.array([4.0, 4.0, 4.0, 4.0])
    p = np.full(4, 0.25)
    r = roi_flat_bets(y, p, d, edge_threshold=0.0)
    # Won 1 of 4 at 4.0 dec: profit = 1*(4-1) - 3*1 = 0; ROI = 0
    assert abs(r['roi']) < 1e-6


def test_roi_flat_bets_no_qualifying():
    y = np.array([1, 0])
    d = np.array([2.0, 2.0])
    p = np.array([0.4, 0.4])
    r = roi_flat_bets(y, p, d, edge_threshold=0.50)
    assert r['n_bets'] == 0
    assert r['roi'] == 0.0


# ---- Summarize -------------------------------------------------------------

def test_summarize_returns_expected_keys():
    y = np.array([1, 0, 1, 0])
    p = np.array([0.6, 0.3, 0.7, 0.2])
    s = summarize(y, p)
    for key in ('n', 'base_rate', 'log_loss', 'brier', 'auc', 'ece_10bin'):
        assert key in s


# ---- Walk-forward folds ----------------------------------------------------

def _toy_df():
    dates = pd.to_datetime([
        '2022-01-15', '2022-02-15', '2022-04-15', '2022-07-15',
        '2022-10-15', '2023-01-15', '2023-04-15', '2023-07-15',
    ])
    return pd.DataFrame({
        'race_date': dates,
        'won': [0, 1, 0, 1, 1, 0, 1, 0],
        'feature': range(8),
    })


def test_quarterly_folds_no_future_leak():
    df = _toy_df()
    folds = make_quarterly_folds(df, min_train_rows=1)
    for f in folds:
        train_max = pd.to_datetime(df.iloc[f.train_idx]['race_date']).max()
        test_min = pd.to_datetime(df.iloc[f.test_idx]['race_date']).min()
        assert train_max < test_min


def test_yearly_folds_no_future_leak():
    df = _toy_df()
    folds = make_yearly_folds(df, min_train_rows=1)
    for f in folds:
        train_max = pd.to_datetime(df.iloc[f.train_idx]['race_date']).max()
        test_min = pd.to_datetime(df.iloc[f.test_idx]['race_date']).min()
        assert train_max < test_min


def test_folds_skip_when_insufficient_train():
    df = _toy_df()
    folds = make_yearly_folds(df, min_train_rows=100)
    assert len(folds) == 0


def test_run_backtest_calls_callbacks():
    df = _toy_df()
    folds = make_quarterly_folds(df, min_train_rows=1)
    n_calls = {'fit': 0, 'eval': 0}

    def fit_predict(train, test):
        n_calls['fit'] += 1
        return np.full(len(test), 0.25)

    def evaluate(y, p, t):
        n_calls['eval'] += 1
        return {'mae': float(np.mean(np.abs(p - y)))}

    bt = run_backtest(df, fit_predict, folds, evaluate)
    assert n_calls['fit'] == len(folds)
    assert n_calls['eval'] == len(folds)
    assert 'mae' in bt.columns
    assert 'fold_id' in bt.columns
