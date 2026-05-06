"""Train baseline model and run walk-forward backtest on PP data.

Outputs:
    data/processed/training_set.parquet
    data/processed/backtest_results.csv
    models/baseline_v0.2.pkl
    docs/baseline_metrics.md
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'src'))

from derbyedge.historical_features import build_historical_training_set
from derbyedge.model import (
    fit_by_surface, predict_by_surface, save_models,
    NUMERIC_FEATURES, CATEGORICAL_FEATURES, CORE_NUMERIC_FEATURES,
    CONNECTION_PRIOR_FEATURES, _USE_PRIORS,
)
from derbyedge.evaluation import summarize, odds_int_to_decimal
from derbyedge.backtest import make_quarterly_folds, run_backtest


def main():
    db = ROOT / 'data' / 'processed' / 'derbyedge.sqlite'
    out_dir = ROOT / 'data' / 'processed'
    out_dir.mkdir(parents=True, exist_ok=True)
    models_dir = ROOT / 'models'
    models_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = ROOT / 'docs'
    docs_dir.mkdir(parents=True, exist_ok=True)

    print(f"Connection priors active: {_USE_PRIORS} (set DERBYEDGE_USE_CONNECTION_PRIORS=1 to enable)")
    print("Building historical training set from PPs...")
    conn = sqlite3.connect(db)
    df = build_historical_training_set(conn, min_prior_starts=1)
    conn.close()
    print(f"  rows: {len(df)}")
    print(f"  wins: {df['won'].sum()} ({df['won'].mean():.1%} base rate)")
    print(f"  surfaces: {df['pp_surface'].value_counts().to_dict()}")
    print(f"  date range: {df['race_date'].min().date()} -> {df['race_date'].max().date()}")

    df['decimal_odds'] = df['odds_int'].apply(odds_int_to_decimal)
    df.to_parquet(out_dir / 'training_set.parquet', index=False)
    print(f"  saved -> {out_dir / 'training_set.parquet'}")

    # ---- Walk-forward backtest by quarter
    print("\nWalk-forward backtest (quarterly):")
    folds = make_quarterly_folds(df, min_train_rows=200)
    print(f"  {len(folds)} folds with sufficient training data")

    def fit_predict(train_df: pd.DataFrame, test_df: pd.DataFrame) -> np.ndarray:
        models = fit_by_surface(train_df, calibrated=False, min_per_surface=80)
        return predict_by_surface(models, test_df, fallback_prob=train_df['won'].mean())

    def evaluate(y_true, y_pred, test_df):
        return summarize(y_true, y_pred, test_df['decimal_odds'].to_numpy())

    bt = run_backtest(df, fit_predict, folds, evaluate, label_col='won')

    # ---- Dump per-row out-of-fold predictions for calibration plot in UI
    pred_rows = []
    for f in folds:
        train_df = df[(df['race_date'] >= pd.to_datetime(f.train_start)) &
                      (df['race_date'] <= pd.to_datetime(f.train_end))]
        test_df = df[(df['race_date'] >= pd.to_datetime(f.test_start)) &
                     (df['race_date'] <= pd.to_datetime(f.test_end))]
        if len(train_df) < 200 or len(test_df) == 0:
            continue
        y_pred = fit_predict(train_df, test_df)
        pred_rows.append(pd.DataFrame({
            'fold_id': f.fold_id,
            'race_date': test_df['race_date'].values,
            'pp_surface': test_df['pp_surface'].values,
            'y_true': test_df['won'].values,
            'y_pred': y_pred,
        }))
    if pred_rows:
        preds = pd.concat(pred_rows, ignore_index=True)
        preds.to_parquet(out_dir / 'oof_predictions.parquet', index=False)
        print(f"  saved -> {out_dir / 'oof_predictions.parquet'} ({len(preds)} rows)")
    print(bt[['fold_id','test_start','test_end','n_train','n_test',
              'log_loss','brier','auc','ece_10bin','base_rate']].to_string(index=False))

    # Drop folds with NaN AUC (no positives in test) for the summary average
    valid = bt.dropna(subset=['auc'])
    summary = {
        'n_folds': len(bt),
        'n_folds_valid_auc': len(valid),
        'mean_log_loss': float(valid['log_loss'].mean()) if len(valid) else None,
        'mean_brier': float(valid['brier'].mean()) if len(valid) else None,
        'mean_auc': float(valid['auc'].mean()) if len(valid) else None,
        'mean_ece': float(valid['ece_10bin'].mean()) if len(valid) else None,
    }
    print("\nFold averages:")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    bt.to_csv(out_dir / 'backtest_results.csv', index=False)
    print(f"\nSaved backtest -> {out_dir / 'backtest_results.csv'}")

    # ---- Train final model on ALL data, save artifact
    print("\nTraining final model on full dataset...")
    final_models = fit_by_surface(df, calibrated=True, min_per_surface=100)
    print(f"  surface models trained: {list(final_models.keys())}")
    metadata = {
        'features_numeric': NUMERIC_FEATURES,
        'features_categorical': CATEGORICAL_FEATURES,
        'train_rows': int(len(df)),
        'win_base_rate': float(df['won'].mean()),
        'date_range': [str(df['race_date'].min().date()), str(df['race_date'].max().date())],
        'backtest_summary': summary,
    }
    model_path = models_dir / 'baseline_v0.3.pkl'
    save_models(final_models, model_path, metadata=metadata)
    print(f"  saved -> {model_path}")

    # ---- Markdown report
    report = []
    report.append("# Baseline Model — v0.2\n")
    report.append(f"Trained: {len(df):,} rows, {df['won'].sum()} wins ({df['won'].mean():.1%}). "
                  f"Date range {df['race_date'].min().date()} → {df['race_date'].max().date()}.\n")
    report.append("## Walk-forward backtest (quarterly)\n")
    cols = ['fold_id','test_start','test_end','n_train','n_test',
            'log_loss','brier','auc','ece_10bin','base_rate']
    # Plain markdown table (no tabulate dep)
    report.append('| ' + ' | '.join(cols) + ' |')
    report.append('|' + '|'.join(['---'] * len(cols)) + '|')
    for _, r in bt[cols].iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, float):
                cells.append(f'{v:.4f}')
            else:
                cells.append(str(v))
        report.append('| ' + ' | '.join(cells) + ' |')
    report.append("\n## Fold averages\n")
    for k, v in summary.items():
        report.append(f"- **{k}**: {v}")
    report.append("\n## What the metrics mean\n")
    report.append("- **log_loss** lower is better. Baseline (predict base rate): "
                  f"{-(df['won'].mean()*np.log(df['won'].mean()) + (1-df['won'].mean())*np.log(1-df['won'].mean())):.4f}")
    report.append("- **brier** lower is better. Baseline (predict base rate): "
                  f"{(df['won'].mean()*(1-df['won'].mean())):.4f}")
    report.append("- **auc** discrimination. 0.50 = random; 0.70+ = useful for racing.")
    report.append("- **ece_10bin** expected calibration error across 10 quantile bins. <0.05 is well-calibrated.")
    report.append("\n## Honest caveats\n")
    report.append("- Training corpus is **per-runner PP rows**, not full race shells. "
                  "Within-race calibration only kicks in at scoring time via softmax over today's field.")
    report.append("- 2,459 rows is small for racing ML. Treat metrics as plumbing validation, not bankable signal.")
    report.append("- Surface-conditioned, but no class-distance interaction yet, no jockey/trainer effects.")
    report.append("- ROI columns are exploratory because the per-runner odds in PPs are post-race — "
                  "this is the one place we deliberately use closing odds for cost-of-betting estimation.")
    (docs_dir / 'baseline_metrics.md').write_text('\n'.join(report))
    print(f"  saved -> {docs_dir / 'baseline_metrics.md'}")


if __name__ == '__main__':
    main()
