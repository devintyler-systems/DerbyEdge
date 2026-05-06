"""Win-probability model.

Baseline: logistic regression with numeric impute + standardize, categorical
one-hot, plus isotonic calibration on a held-out tail of training data.

We train ONE model per surface family (D, T, E). Same feature set, different
weights — the system prompt explicitly forbids a single global model across
surfaces.

To convert per-runner win probabilities to within-race probabilities at
scoring time, apply softmax over today's field. We provide a helper for that.
"""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.calibration import CalibratedClassifierCV


# Core features (v0.2 baseline, AUC ~0.643 at 2.1K rows)
CORE_NUMERIC_FEATURES = [
    'n_starts',
    'n_starts_today_surface',
    'n_starts_today_distbucket',
    'avg_speed_last3',
    'best_speed_last3',
    'speed_trend_slope',
    'pace2_avg_last3',
    'class_avg_last3',
    'finish_energy_raw',
    'days_since_last',
    'layoff_flag',
    'pp_purse_usa',
    'pp_n_starters',
]

# Connection priors (v0.3) — AS-OF, Beta(2,12) smoothed.
# Off by default at 2.1K rows because surface/JT cells are too thin to add
# signal beyond the league prior. Activate when corpus ≥ ~10K rows.
CONNECTION_PRIOR_FEATURES = [
    'jockey_winrate_prior',
    'jockey_winrate_surface',
    'trainer_winrate_prior',
    'trainer_winrate_surface',
    'jt_combo_winrate_prior',
    'jockey_starts_prior',
    'trainer_starts_prior',
    'jt_combo_starts_prior',
]

# Active feature list: env var DERBYEDGE_USE_CONNECTION_PRIORS=1 to enable.
import os
_USE_PRIORS = os.environ.get('DERBYEDGE_USE_CONNECTION_PRIORS', '0') == '1'
NUMERIC_FEATURES = (
    CORE_NUMERIC_FEATURES + CONNECTION_PRIOR_FEATURES
    if _USE_PRIORS
    else CORE_NUMERIC_FEATURES
)

CATEGORICAL_FEATURES = [
    'run_style',
    'pp_dist_bucket',
    'pp_race_type',
]


def make_pipeline(C: float = 1.0, calibrated: bool = True) -> Pipeline:
    """Build the model pipeline. C = inverse regularization strength."""
    pre = ColumnTransformer([
        ('num', Pipeline([
            ('impute', SimpleImputer(strategy='median')),
            ('scale', StandardScaler()),
        ]), NUMERIC_FEATURES),
        ('cat', Pipeline([
            ('impute', SimpleImputer(strategy='constant', fill_value='unknown')),
            ('ohe', OneHotEncoder(handle_unknown='ignore')),
        ]), CATEGORICAL_FEATURES),
    ])
    base = LogisticRegression(
        C=C, max_iter=2000, solver='lbfgs', class_weight=None,
    )
    if calibrated:
        # Wrap with sigmoid calibration via cv splits inside training set
        clf = CalibratedClassifierCV(
            estimator=Pipeline([('pre', pre), ('lr', base)]),
            method='sigmoid',
            cv=3,
        )
    else:
        clf = Pipeline([('pre', pre), ('lr', base)])
    return clf


def fit_by_surface(train_df: pd.DataFrame,
                   surface_col: str = 'pp_surface',
                   label_col: str = 'won',
                   C: float = 1.0,
                   calibrated: bool = True,
                   min_per_surface: int = 100) -> dict:
    """Fit one model per surface. Returns dict[surface] -> fitted pipeline."""
    models = {}
    for surf, g in train_df.groupby(surface_col):
        if g[label_col].sum() < 5 or len(g) < min_per_surface:
            continue
        clf = make_pipeline(C=C, calibrated=calibrated)
        X = g[NUMERIC_FEATURES + CATEGORICAL_FEATURES]
        y = g[label_col].astype(int).to_numpy()
        clf.fit(X, y)
        models[surf] = clf
    return models


def predict_by_surface(models: dict,
                       test_df: pd.DataFrame,
                       surface_col: str = 'pp_surface',
                       fallback_prob: float = 0.10) -> np.ndarray:
    """Predict per-runner win prob, routing each row to its surface model."""
    out = np.full(len(test_df), fallback_prob, dtype=float)
    test_df = test_df.reset_index(drop=True)
    for surf, clf in models.items():
        mask = (test_df[surface_col] == surf).to_numpy()
        if not mask.any():
            continue
        X = test_df.loc[mask, NUMERIC_FEATURES + CATEGORICAL_FEATURES]
        out[mask] = clf.predict_proba(X)[:, 1]
    # If a row has no model, fallback (already set)
    # If a row's surface model exists but predict failed, the fallback stays
    return out


def race_softmax(per_runner_probs: np.ndarray, race_ids: np.ndarray,
                 temperature: float = 1.0) -> np.ndarray:
    """Convert per-runner win probabilities to within-race probabilities that
    sum to 1.0 per race. Uses softmax over logit(p) within each race.

    temperature > 1 flattens; < 1 sharpens.
    """
    p = np.clip(np.asarray(per_runner_probs, dtype=float), 1e-6, 1 - 1e-6)
    logit = np.log(p / (1 - p))
    out = np.zeros_like(p)
    df = pd.DataFrame({'race_id': race_ids, 'logit': logit})
    for rid, g in df.groupby('race_id'):
        z = g['logit'].to_numpy() / max(temperature, 1e-3)
        z = z - z.max()
        e = np.exp(z)
        out[g.index.to_numpy()] = e / e.sum()
    return out


# ------- Persistence -------

def save_models(models: dict, path: str | Path, metadata: Optional[dict] = None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'models': models, 'metadata': metadata or {}}
    with open(path, 'wb') as f:
        pickle.dump(payload, f)


def load_models(path: str | Path) -> tuple[dict, dict]:
    with open(path, 'rb') as f:
        payload = pickle.load(f)
    return payload['models'], payload.get('metadata', {})
