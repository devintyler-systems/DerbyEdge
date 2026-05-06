"""Walk-forward backtest harness.

Time-forward splits only — never random across racing rows. The fold structure:
    For each split point t_i in a sorted list of dates:
        train = all rows with race_date <  t_i
        test  = all rows with t_i <= race_date < t_{i+1}

Default split: by quarter. Min train size enforced.

This is the framework that prevents future information leak. Calibration is
fit on the most recent slice of training data only (last 20%) to avoid the
entire training distribution dragging old calibrations forward.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator, Optional

import numpy as np
import pandas as pd


@dataclass
class Fold:
    fold_id: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_idx: np.ndarray
    test_idx: np.ndarray


def make_quarterly_folds(df: pd.DataFrame,
                         date_col: str = 'race_date',
                         min_train_rows: int = 200) -> list[Fold]:
    """Generate walk-forward quarterly folds.

    Each fold trains on everything strictly before its test quarter.
    Folds with insufficient training data are skipped.
    """
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col).reset_index(drop=True)
    df['_q'] = df[date_col].dt.to_period('Q')

    folds = []
    for fold_id, q in enumerate(sorted(df['_q'].unique())):
        test_mask = df['_q'] == q
        train_mask = df[date_col] < df.loc[test_mask, date_col].min()
        if train_mask.sum() < min_train_rows:
            continue
        if test_mask.sum() == 0:
            continue
        folds.append(Fold(
            fold_id=fold_id,
            train_start=df.loc[train_mask, date_col].min(),
            train_end=df.loc[train_mask, date_col].max(),
            test_start=df.loc[test_mask, date_col].min(),
            test_end=df.loc[test_mask, date_col].max(),
            train_idx=df.index[train_mask].to_numpy(),
            test_idx=df.index[test_mask].to_numpy(),
        ))
    return folds


def make_yearly_folds(df: pd.DataFrame,
                      date_col: str = 'race_date',
                      min_train_rows: int = 200) -> list[Fold]:
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col).reset_index(drop=True)
    df['_y'] = df[date_col].dt.year

    folds = []
    years = sorted(df['_y'].unique())
    for fold_id, y in enumerate(years):
        test_mask = df['_y'] == y
        train_mask = df['_y'] < y
        if train_mask.sum() < min_train_rows:
            continue
        if test_mask.sum() == 0:
            continue
        folds.append(Fold(
            fold_id=fold_id,
            train_start=df.loc[train_mask, date_col].min(),
            train_end=df.loc[train_mask, date_col].max(),
            test_start=df.loc[test_mask, date_col].min(),
            test_end=df.loc[test_mask, date_col].max(),
            train_idx=df.index[train_mask].to_numpy(),
            test_idx=df.index[test_mask].to_numpy(),
        ))
    return folds


def run_backtest(df: pd.DataFrame,
                 fit_predict: Callable[[pd.DataFrame, pd.DataFrame], np.ndarray],
                 folds: list[Fold],
                 evaluate: Callable[[np.ndarray, np.ndarray, pd.DataFrame], dict],
                 label_col: str = 'won') -> pd.DataFrame:
    """Run a backtest given a fit-predict callable and an evaluate callable.

    fit_predict: (train_df, test_df) -> np.ndarray of test predictions
    evaluate: (y_true, y_pred, test_df) -> dict of metrics
    """
    rows = []
    for f in folds:
        train_df = df.iloc[f.train_idx]
        test_df = df.iloc[f.test_idx]
        y_pred = fit_predict(train_df, test_df)
        y_true = test_df[label_col].to_numpy()
        m = evaluate(y_true, y_pred, test_df)
        m.update({
            'fold_id': f.fold_id,
            'train_start': f.train_start.date(),
            'train_end': f.train_end.date(),
            'test_start': f.test_start.date(),
            'test_end': f.test_end.date(),
            'n_train': len(train_df),
            'n_test': len(test_df),
        })
        rows.append(m)
    return pd.DataFrame(rows)
