"""Evaluation metrics for the win-probability model.

We treat each historical PP as a binary classification example (won 1/0)
because that's the granularity our training data supports. Within-race
softmax is applied at scoring time, but training/eval at the per-runner level
keeps things tractable.

Metrics:
    log_loss          - Bernoulli log-loss (lower = better)
    brier_score       - (p - y)^2 mean (lower = better)
    calibration_table - 10 equal-frequency bins: predicted vs observed win rate
    auc               - Discrimination (0.5 = random)
    top1_accuracy     - When grouped by race-shell (start_id race_id proxy):
                        does the highest-prob runner actually win? (Only valid
                        when test set has full field shells; PP data does not,
                        so this is reported but flagged.)
    roi_at_best       - If we bet $1 on every model_prob >= threshold,
                        ROI based on actual odds.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional


EPS = 1e-15


def log_loss(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    p = np.clip(y_pred, EPS, 1 - EPS)
    return float(-np.mean(y_true * np.log(p) + (1 - y_true) * np.log(1 - p)))


def brier_score(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean((np.asarray(y_pred, dtype=float) - np.asarray(y_true, dtype=float)) ** 2))


def auc_roc(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """AUC via Mann-Whitney U. No sklearn dependency."""
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    pos = y_pred[y_true == 1]
    neg = y_pred[y_true == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float('nan')
    # Tie-aware
    n = len(pos) * len(neg)
    wins = 0.0
    for p in pos:
        wins += np.sum(p > neg) + 0.5 * np.sum(p == neg)
    return float(wins / n)


def calibration_table(y_true: np.ndarray, y_pred: np.ndarray,
                      n_bins: int = 10) -> pd.DataFrame:
    df = pd.DataFrame({'y': np.asarray(y_true, dtype=float),
                       'p': np.asarray(y_pred, dtype=float)})
    df = df.sort_values('p').reset_index(drop=True)
    df['bin'] = pd.qcut(df['p'].rank(method='first'), n_bins, labels=False)
    grouped = df.groupby('bin').agg(
        n=('y', 'size'),
        mean_pred=('p', 'mean'),
        empirical=('y', 'mean'),
    ).reset_index()
    grouped['gap'] = grouped['mean_pred'] - grouped['empirical']
    return grouped


def expected_calibration_error(y_true: np.ndarray, y_pred: np.ndarray,
                               n_bins: int = 10) -> float:
    cal = calibration_table(y_true, y_pred, n_bins)
    weights = cal['n'] / cal['n'].sum()
    return float((weights * cal['gap'].abs()).sum())


def odds_int_to_decimal(odds_int: float | int | None) -> float | None:
    """Equibase odds_int convention: integer / 100 = $ payout per $1 (decimal-1).

    e.g., odds_int = 1000 -> $10 payout = 9-1 = decimal 11.0? No:
    Equibase odds_int = decimal odds * 100. So 1000 = decimal 10.0.

    We follow that interpretation.
    """
    if odds_int is None or pd.isna(odds_int) or odds_int <= 0:
        return None
    dec = float(odds_int) / 100.0
    if dec <= 1.0:
        return None
    return dec


def roi_flat_bets(y_true: np.ndarray, y_pred: np.ndarray,
                  decimal_odds: np.ndarray,
                  prob_threshold: float = 0.0,
                  edge_threshold: float = 0.0) -> dict:
    """Flat $1 bet on every runner where:
        model_prob >= prob_threshold AND
        edge = (model - market) / market >= edge_threshold.

    Returns dict with: n_bets, n_wins, total_staked, total_returned, roi.
    """
    y = np.asarray(y_true)
    p = np.asarray(y_pred, dtype=float)
    d = np.asarray(decimal_odds, dtype=float)
    market = np.where(d > 0, 1.0 / d, np.nan)
    edge = np.where(market > 0, (p - market) / market, -np.inf)

    mask = (p >= prob_threshold) & (edge >= edge_threshold) & np.isfinite(d)
    if not mask.any():
        return {'n_bets': 0, 'n_wins': 0, 'total_staked': 0.0,
                'total_returned': 0.0, 'roi': 0.0}
    bets_y = y[mask]
    bets_d = d[mask]
    staked = float(mask.sum())
    returned = float(np.sum(bets_y * (bets_d - 1.0))) - float(np.sum(1 - bets_y))
    # returned = profit/loss (excludes original stake)
    return {
        'n_bets': int(mask.sum()),
        'n_wins': int(bets_y.sum()),
        'total_staked': staked,
        'profit': returned,
        'roi': returned / staked if staked > 0 else 0.0,
    }


def summarize(y_true: np.ndarray, y_pred: np.ndarray,
              decimal_odds: Optional[np.ndarray] = None) -> dict:
    out = {
        'n': int(len(y_true)),
        'base_rate': float(np.mean(y_true)),
        'log_loss': log_loss(y_true, y_pred),
        'brier': brier_score(y_true, y_pred),
        'auc': auc_roc(y_true, y_pred),
        'ece_10bin': expected_calibration_error(y_true, y_pred, n_bins=10),
    }
    if decimal_odds is not None:
        # ROI for various edge thresholds
        for et in (0.0, 0.20, 0.40):
            r = roi_flat_bets(y_true, y_pred, decimal_odds, edge_threshold=et)
            out[f'roi_edge>={et:.2f}'] = r['roi']
            out[f'n_bets_edge>={et:.2f}'] = r['n_bets']
    return out
