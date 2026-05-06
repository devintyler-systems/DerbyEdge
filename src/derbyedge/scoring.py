"""Apply trained model to today's entries to produce model_prob per entry.

Used at race-day time. Bridges:
    today's features (from features.build_entry_features)  +
    today's race surface  +
    trained model artifacts
into the model_prob CSV format that edge_calc.build_edge_table expects.

Steps:
    1. Build today-side features (already wired to use only PPs strictly before today).
    2. Map today's features to the model's feature schema.
    3. Predict per-runner win probabilities using the surface-routed models.
    4. Apply within-race softmax so probabilities sum to 1.0 per race.
    5. Return DataFrame with [entry_id, model_prob, raw_prob, race_id].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .features import build_entry_features, _distance_bucket
from .model import (
    load_models, predict_by_surface, race_softmax,
    NUMERIC_FEATURES, CATEGORICAL_FEATURES,
)
from .connection_priors import (
    snapshot_connection_state, attach_priors_to_entries,
)


def _map_today_features_to_model_schema(feat: pd.DataFrame) -> pd.DataFrame:
    """Today-side features.py uses different column names than historical_features.
    Map them so the model sees a consistent schema.
    """
    df = feat.copy()
    # surface lives on entries via today_surface
    df['pp_surface'] = df.get('today_surface')
    # dist bucket
    df['pp_dist_bucket'] = df.get('today_dist_bucket')
    # n_starters today
    df['pp_n_starters'] = df.get('today_n_runners')
    # purse: not on the entry feature row by default; pull from races
    if 'pp_purse_usa' not in df.columns:
        df['pp_purse_usa'] = np.nan
    # race_type
    df['pp_race_type'] = df.get('race_type')
    # class_avg_last3 isn't computed in features.py v0.1; put NaN -> imputer handles
    if 'class_avg_last3' not in df.columns:
        df['class_avg_last3'] = np.nan
    # Ensure all model features exist
    for c in NUMERIC_FEATURES + CATEGORICAL_FEATURES:
        if c not in df.columns:
            df[c] = np.nan if c in NUMERIC_FEATURES else 'unknown'
    return df


def score_entries(conn: sqlite3.Connection,
                  model_path: str | Path,
                  softmax_temperature: float = 1.0,
                  features_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Returns DataFrame with columns: entry_id, race_id, model_prob, raw_prob."""
    if features_df is None:
        features_df = build_entry_features(conn)

    # Need to attach purse + race_type from races table
    extra = pd.read_sql_query(
        "SELECT race_id, purse_usa AS pp_purse_usa, race_type AS pp_race_type FROM races",
        conn,
    )
    df = features_df.merge(extra, on='race_id', how='left', suffixes=('', '_r'))
    # If features.py provided race_type, prefer it; else use from races
    if 'race_type' not in df.columns or df['race_type'].isna().all():
        df['race_type'] = df['pp_race_type']

    # ---- Connection priors: build a snapshot from history strictly before
    # the earliest race_date in today's set, then attach to today's rows.
    if 'jockey_id' in df.columns and 'trainer_id' in df.columns:
        # Get race date(s) from races table
        race_dates = pd.read_sql_query(
            "SELECT race_id, race_date FROM races", conn
        )
        race_dates['race_date'] = pd.to_datetime(race_dates['race_date'])
        df = df.merge(race_dates, on='race_id', how='left', suffixes=('', '_rd'))
        cutoff = df['race_date'].min() if df['race_date'].notna().any() else None
        if cutoff is not None:
            hist_starts = pd.read_sql_query("""
                SELECT pp_race_date AS race_date, jockey_id, trainer_id,
                       pp_surface, official_finish
                FROM horse_starts
                WHERE pp_race_date IS NOT NULL AND official_finish > 0
            """, conn)
            hist_starts['won'] = (hist_starts['official_finish'] == 1).astype(int)
            snap = snapshot_connection_state(hist_starts, cutoff_date=cutoff)
            df['today_surface'] = df['today_surface'].fillna('U')
            df = attach_priors_to_entries(
                df, snap,
                jockey_col='jockey_id',
                trainer_col='trainer_id',
                surface_col='today_surface',
            )

    df = _map_today_features_to_model_schema(df)
    models, metadata = load_models(model_path)

    # Predict per-runner raw probabilities
    raw = predict_by_surface(models, df, surface_col='pp_surface',
                             fallback_prob=metadata.get('win_base_rate', 0.10))
    df['raw_prob'] = raw

    # Within-race softmax so probs sum to 1 per race
    df['model_prob'] = race_softmax(raw, df['race_id'].to_numpy(),
                                    temperature=softmax_temperature)

    return df[['entry_id', 'race_id', 'horse_name', 'raw_prob', 'model_prob']]
