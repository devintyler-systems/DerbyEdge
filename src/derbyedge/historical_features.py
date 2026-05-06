"""Per-PP historical feature extraction for training.

Each row in horse_starts is a labeled example: did this horse win this start?
Features are computed AS-OF each PP's race_date using only that horse's
earlier PPs (strict <, not <=). Pure pre-race information.

This is the training mirror of features.py (which computes today-side features
from PPs). The functions here intentionally share definitions: run_style,
late_fig_z, devcurve, etc. We reuse the same helpers where possible.

Output columns (per historical start):
    horse_reg, pp_track_id, pp_race_date, pp_race_number, start_id
    pp_surface, pp_distance_bucket, pp_grade, pp_n_starters, pp_purse_usa
    n_starts, n_starts_today_surface, n_starts_today_distbucket
    avg_speed_last3, best_speed_last3, speed_trend_slope
    pace2_avg_last3, finish_energy_raw
    run_style (E/EP/P/S/unknown)
    days_since_last, layoff_flag
    odds_int (the horse's actual odds in this race; useful as a market signal)
    -- LABEL --
    won (1 if official_finish == 1 else 0)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from .features import (
    _clean_speed,
    _clean_pace,
    _distance_bucket,
    classify_run_style,
)
from .connection_priors import add_connection_priors


def build_historical_training_set(conn: sqlite3.Connection,
                                  min_prior_starts: int = 1) -> pd.DataFrame:
    """Build one row per historical horse_start with as-of features.

    min_prior_starts: drop debut-runners (no prior PPs to compute features from).
                      Default 1 keeps any horse with at least one prior start.
    """
    starts = pd.read_sql_query("""
        SELECT start_id, horse_reg, pp_track_id, pp_race_date, pp_race_number,
               pp_surface, pp_distance_id, pp_distance_unit,
               pp_grade, pp_n_starters, pp_purse_usa, pp_race_type,
               official_finish, speed_figure,
               pace_figure_1, pace_figure_2, pace_figure_3,
               class_rating, odds_int,
               jockey_id, trainer_id
        FROM horse_starts
        WHERE pp_race_date IS NOT NULL AND official_finish > 0
    """, conn)
    starts['race_date'] = pd.to_datetime(starts['pp_race_date'], errors='coerce')
    starts = starts.dropna(subset=['race_date']).sort_values(
        ['horse_reg', 'race_date']
    ).reset_index(drop=True)

    starts['speed_clean'] = starts['speed_figure'].apply(_clean_speed)
    starts['pace1_clean'] = starts['pace_figure_1'].apply(_clean_pace)
    starts['pace2_clean'] = starts['pace_figure_2'].apply(_clean_pace)
    starts['pace3_clean'] = starts['pace_figure_3'].apply(_clean_pace)
    starts['class_clean'] = starts['class_rating'].where(starts['class_rating'] > 0)

    # Pull point-of-call once and merge for first-call positions
    poc = pd.read_sql_query("""
        SELECT start_id, point_of_call, position_int
        FROM point_of_call WHERE point_of_call IN ('1','F')
    """, conn)
    first_call = poc[poc['point_of_call']=='1'][['start_id','position_int']].rename(
        columns={'position_int':'pos_call_1'})
    finish_call = poc[poc['point_of_call']=='F'][['start_id','position_int']].rename(
        columns={'position_int':'pos_finish'})
    starts = starts.merge(first_call, on='start_id', how='left')
    starts = starts.merge(finish_call, on='start_id', how='left')

    # ---- Build per-row as-of features by walking each horse's history
    rows = []
    for horse_reg, group in starts.groupby('horse_reg'):
        g = group.sort_values('race_date').reset_index(drop=True)
        for i, row in g.iterrows():
            today = row['race_date']
            prior = g.iloc[:i]   # strictly before this start
            n_starts = len(prior)
            if n_starts < min_prior_starts:
                continue

            # Today-side context (this PP's own race characteristics)
            today_bucket = _distance_bucket(row['pp_distance_id'], row['pp_distance_unit'])
            today_surface = row['pp_surface']

            # Surface / bucket counts among prior starts
            n_today_surface = int((prior['pp_surface'] == today_surface).sum()) if today_surface else 0
            bucket_match = prior.apply(
                lambda r: _distance_bucket(r['pp_distance_id'], r['pp_distance_unit']) == today_bucket,
                axis=1,
            )
            n_today_distbucket = int(bucket_match.sum()) if len(prior) else 0

            # Speed last 3
            spd3 = prior['speed_clean'].dropna().tail(3).tolist()
            avg_speed_last3 = float(np.mean(spd3)) if spd3 else None
            best_speed_last3 = float(np.max(spd3)) if spd3 else None

            # Speed trend (last 4)
            last4 = prior[['race_date','speed_clean']].dropna().tail(4)
            if len(last4) >= 2:
                x = (last4['race_date'].astype('int64') // 86_400_000_000_000).to_numpy().astype(float)
                y = last4['speed_clean'].to_numpy().astype(float)
                x = x - x.mean()
                denom = (x**2).sum()
                slope = float((x*(y-y.mean())).sum()/denom) if denom > 0 else 0.0
            else:
                slope = None

            # Pace 2 avg
            pace2 = prior['pace2_clean'].dropna().tail(3).mean()
            pace2_avg_last3 = float(pace2) if not pd.isna(pace2) else None

            # Class avg
            cls = prior['class_clean'].dropna().tail(3).mean()
            class_avg_last3 = float(cls) if not pd.isna(cls) else None

            # Run style from last 3 first-call positions
            fc = prior['pos_call_1'].dropna().tail(3).astype(int).tolist()
            run_style = classify_run_style(fc)

            # Finish energy: positions gained 1st-call -> finish, last 3
            mvmt_df = prior[['pos_call_1','pos_finish']].dropna().tail(3)
            if len(mvmt_df):
                finish_energy_raw = float((mvmt_df['pos_call_1'] - mvmt_df['pos_finish']).mean())
            else:
                finish_energy_raw = 0.0

            # Days since last
            last_date = prior['race_date'].max()
            days_since_last = int((today - last_date).days) if pd.notna(last_date) else None

            rows.append({
                'start_id': row['start_id'],
                'horse_reg': horse_reg,
                'pp_track_id': row['pp_track_id'],
                'race_date': today,
                'pp_race_number': row['pp_race_number'],
                'jockey_id': row['jockey_id'] if pd.notna(row['jockey_id']) else -1,
                'trainer_id': row['trainer_id'] if pd.notna(row['trainer_id']) else -1,
                'pp_surface': today_surface,
                'pp_dist_bucket': today_bucket,
                'pp_grade': row['pp_grade'],
                'pp_n_starters': row['pp_n_starters'],
                'pp_purse_usa': row['pp_purse_usa'],
                'pp_race_type': row['pp_race_type'],
                'odds_int': row['odds_int'],
                'n_starts': n_starts,
                'n_starts_today_surface': n_today_surface,
                'n_starts_today_distbucket': n_today_distbucket,
                'avg_speed_last3': avg_speed_last3,
                'best_speed_last3': best_speed_last3,
                'speed_trend_slope': slope,
                'pace2_avg_last3': pace2_avg_last3,
                'class_avg_last3': class_avg_last3,
                'finish_energy_raw': finish_energy_raw,
                'run_style': run_style,
                'days_since_last': days_since_last,
                'layoff_flag': int(days_since_last is not None and days_since_last >= 60),
                'won': int(row['official_finish'] == 1),
            })
    df = pd.DataFrame(rows)
    if len(df):
        df = add_connection_priors(df)
    return df
