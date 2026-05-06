"""Workout features: per-entry (today-side) trainer-intent and fitness signals.

Equibase free dataset surfaces only a subset of workout fields:
    workout_date, distance_id (always furlongs), workout_track_id,
    type_of_workout (B = breeze, H = handily), track_condition.
The richer fields (workout_time, rank_in_set, set_size, workout_note) are NOT
populated in this feed. So we cannot derive bullet-work flags or pace metrics.

What we CAN derive:
    - Volume:  number of works in last 30/60d, total furlongs.
    - Recency: days since last work (>14 = warning).
    - Pattern: regular ~7-day spacing flag (well-managed campaign).
    - Intensity: has a recent H-type work (sharper than routine breeze).
    - Stamina: longest distance in last 60d (>= 600F = route-conditioning).
    - Composite workout_score (0-10) for the edge sheet.

Critical rule: every feature is AS-OF the race_date. Workouts on or after
race_date are not used. Workouts table joins via entry_id, but we filter on
workout_date < race_date defensively in case the dataset is extended later.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import numpy as np
import pandas as pd


WINDOW_30D_DAYS = 30
WINDOW_60D_DAYS = 60
GAP_TOO_LONG_DAYS = 14
REGULAR_SPACING_DAYS = 7
REGULAR_SPACING_TOLERANCE = 3
LONG_WORK_DISTANCE_F = 600   # furlongs * 100, so 6f
HANDILY = 'H'


def _load_workouts(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """SELECT entry_id, horse_reg, workout_date, distance_id,
                  workout_track_id, type_of_workout, track_condition
           FROM workouts
           WHERE workout_date IS NOT NULL""",
        conn,
    )
    df['workout_date'] = pd.to_datetime(df['workout_date'], errors='coerce')
    return df.dropna(subset=['workout_date'])


def compute_workout_features(workouts: pd.DataFrame,
                             entry_race_dates: pd.DataFrame) -> pd.DataFrame:
    """Compute per-entry workout features.

    Args:
        workouts: long-form workouts dataframe with cols
            entry_id, workout_date, distance_id, type_of_workout
        entry_race_dates: dataframe with entry_id, race_date

    Returns:
        DataFrame keyed by entry_id with workout features. Entries with no
        workouts get neutral/zero values plus has_workouts=0.
    """
    out_rows = []
    if workouts.empty:
        for eid in entry_race_dates['entry_id']:
            out_rows.append(_neutral_row(eid))
        return pd.DataFrame(out_rows)

    # Defensive: ensure workout_date is datetime
    workouts = workouts.copy()
    workouts['workout_date'] = pd.to_datetime(workouts['workout_date'], errors='coerce')
    workouts = workouts.dropna(subset=['workout_date'])

    # Index workouts by entry_id for fast lookup
    by_entry = dict(list(workouts.groupby('entry_id')))

    for _, row in entry_race_dates.iterrows():
        eid = row['entry_id']
        race_date = pd.to_datetime(row['race_date'])
        wks = by_entry.get(eid)
        if wks is None or len(wks) == 0:
            out_rows.append(_neutral_row(eid))
            continue
        # Strict as-of: workouts before race_date
        wks = wks[wks['workout_date'] < race_date].copy()
        if len(wks) == 0:
            out_rows.append(_neutral_row(eid))
            continue
        wks = wks.sort_values('workout_date', ascending=False)

        # Days-to-race for each work
        wks['days_to_race'] = (race_date - wks['workout_date']).dt.days

        n_total = len(wks)
        n_30d = int((wks['days_to_race'] <= WINDOW_30D_DAYS).sum())
        n_60d = int((wks['days_to_race'] <= WINDOW_60D_DAYS).sum())
        days_since_last = int(wks['days_to_race'].min())

        # Total furlongs in last 60d (distance_id is furlongs * 100)
        recent60 = wks[wks['days_to_race'] <= WINDOW_60D_DAYS]
        total_f_60d = float(recent60['distance_id'].sum()) / 100.0 if len(recent60) else 0.0

        # Longest single work in 60d (in furlongs)
        longest_f_60d = float(recent60['distance_id'].max()) / 100.0 if len(recent60) else 0.0

        # Handily count in 30d (sharpness signal)
        if 'type_of_workout' in wks.columns:
            n_handily_30d = int(((wks['days_to_race'] <= WINDOW_30D_DAYS) &
                                  (wks['type_of_workout'] == HANDILY)).sum())
        else:
            n_handily_30d = 0
        has_recent_handily = int(n_handily_30d >= 1)

        # Long work flag: any >= 6f work in 60d
        has_long_work_60d = int((recent60['distance_id'] >= LONG_WORK_DISTANCE_F).any()) if len(recent60) else 0

        # Regular pattern: median gap close to 7d, std of gaps small
        regular_pattern = 0
        if n_60d >= 3:
            gaps = recent60.sort_values('workout_date')['workout_date'].diff().dt.days.dropna()
            if len(gaps):
                med = gaps.median()
                std = gaps.std() if len(gaps) > 1 else 0
                if (abs(med - REGULAR_SPACING_DAYS) <= REGULAR_SPACING_TOLERANCE
                        and (std is None or std <= 4)):
                    regular_pattern = 1

        # Composite 0-10 score
        score = _composite_score(
            n_30d=n_30d, n_60d=n_60d,
            days_since_last=days_since_last,
            has_recent_handily=has_recent_handily,
            has_long_work_60d=has_long_work_60d,
            regular_pattern=regular_pattern,
            total_f_60d=total_f_60d,
        )

        out_rows.append({
            'entry_id': eid,
            'has_workouts': 1,
            'n_workouts_60d': n_60d,
            'n_workouts_30d': n_30d,
            'days_since_last_work': days_since_last,
            'gap_too_long_flag': int(days_since_last > GAP_TOO_LONG_DAYS),
            'n_handily_30d': n_handily_30d,
            'has_recent_handily': has_recent_handily,
            'longest_work_furlongs_60d': longest_f_60d,
            'has_long_work_60d': has_long_work_60d,
            'total_furlongs_60d': total_f_60d,
            'regular_pattern_flag': regular_pattern,
            'workout_score': score,
        })

    return pd.DataFrame(out_rows)


def _neutral_row(entry_id: str) -> dict:
    return {
        'entry_id': entry_id,
        'has_workouts': 0,
        'n_workouts_60d': 0,
        'n_workouts_30d': 0,
        'days_since_last_work': None,
        'gap_too_long_flag': 1,    # no recent work = worse than too-long gap
        'n_handily_30d': 0,
        'has_recent_handily': 0,
        'longest_work_furlongs_60d': 0.0,
        'has_long_work_60d': 0,
        'total_furlongs_60d': 0.0,
        'regular_pattern_flag': 0,
        'workout_score': 3.0,   # mild penalty: not seeing works isn't proof of unfitness
    }


def _composite_score(n_30d: int, n_60d: int,
                     days_since_last: int,
                     has_recent_handily: int,
                     has_long_work_60d: int,
                     regular_pattern: int,
                     total_f_60d: float) -> float:
    """0-10 score. 5 = neutral. Sharp pattern -> 8-10. Stale/sparse -> 1-3."""
    score = 5.0

    # Recency
    if days_since_last is None:
        score -= 2.0
    elif days_since_last <= 7:
        score += 1.5
    elif days_since_last <= 14:
        score += 0.5
    elif days_since_last <= 21:
        score -= 0.5
    else:
        score -= 1.5

    # Volume
    if n_30d >= 3:
        score += 1.0
    elif n_30d >= 2:
        score += 0.5
    elif n_30d == 0:
        score -= 1.0

    # Stamina indicator
    if has_long_work_60d:
        score += 0.8

    # Sharpness signal
    if has_recent_handily:
        score += 0.7

    # Pattern regularity (well-managed)
    if regular_pattern:
        score += 0.5

    # Total volume
    if total_f_60d >= 20:    # ~5 four-furlong works
        score += 0.5
    elif total_f_60d < 8:
        score -= 0.5

    return float(np.clip(score, 0.0, 10.0))


def attach_workout_features(conn: sqlite3.Connection,
                            entries_df: pd.DataFrame) -> pd.DataFrame:
    """Attach workout features to an entries dataframe.

    Args:
        conn: sqlite connection
        entries_df: must have entry_id and either race_date or race_id

    Returns:
        Same dataframe with workout feature columns added.
    """
    if 'race_date' not in entries_df.columns:
        if 'race_id' not in entries_df.columns:
            raise ValueError("entries_df must contain race_date or race_id")
        races = pd.read_sql_query(
            "SELECT race_id, race_date FROM races", conn
        )
        entries_df = entries_df.merge(races, on='race_id', how='left')

    entry_dates = entries_df[['entry_id', 'race_date']].drop_duplicates()
    workouts = _load_workouts(conn)
    feats = compute_workout_features(workouts, entry_dates)
    return entries_df.merge(feats, on='entry_id', how='left')
