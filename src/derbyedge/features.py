"""Feature engineering for DerbyEdge v0.

All features are AS-OF the entry race date. We never read fields that would
leak post-race information about today's race.

Equibase encoding handled here:
- speed_figure: 9999 means "not assigned" -> NULL
- pace_figure_*: 0 means "not assigned" -> NULL
- official_finish: 0 means DNF/DQ/scratched -> NULL for ranking purposes

Features produced (per entry, one row):
    n_starts                  Career starts available in PP
    n_starts_route            Starts at >= 8f
    n_starts_dirt             Dirt starts
    n_starts_today_surface    Starts on today's surface
    n_starts_today_distbucket Starts in today's distance bucket
    avg_speed_last3           Avg Equibase Speed over last 3 starts
    best_speed_last3          Max Equibase Speed over last 3 starts
    speed_trend_slope         OLS slope of speed vs date (last 4 starts)
    devcurve_score            0-10, mapped from speed_trend_slope
    finish_energy_score       0-10, derived from late-pace fraction
    late_fig_z                Z-score of finish-energy (within today's race)
    pace_fig_avg              Avg pace_figure_2 over last 3 (proxy for early pace)
    early_pace_z              Z-score of pace_fig_avg (within today's race)
    run_style                 E/EP/P/S based on avg early position
    pacefit_score             0-10, race-shape fit given field's pace mix
    distance_proj_score       0-10, derived from route experience + pedigree (placeholder)
    days_since_last           Days from today back to most recent start
    layoff_flag               1 if days_since_last >= 60
    publicness_score          0-10, derived from morning-line probability rank (PROXY)
    market_implied_prob       From morning-line if available, else NULL
"""
from __future__ import annotations

import sqlite3
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np


# ---------------------------------------------------------------------------
# Helpers

def _hundredths_to_seconds(t: int | None) -> float | None:
    if t is None or t <= 0:
        return None
    return t / 100.0


def _clean_speed(v: int | None) -> int | None:
    if v is None or v >= 9000 or v <= 0:
        return None
    return v


def _clean_pace(v: int | None) -> int | None:
    if v is None or v <= 0:
        return None
    return v


def _distance_bucket(distance_id: int | None, unit: str | None) -> str:
    """Group distance into sprint / mid / route / classic. Equibase: F=furlongs*100? Y=yards.
    Convert to yards-equivalent for consistent bucketing.
    """
    if distance_id is None:
        return 'unknown'
    yards = distance_id
    if unit == 'F':
        # DistanceId in F mode is furlongs * 100 (e.g., 700 = 7 furlongs)
        yards = (distance_id / 100.0) * 220
    elif unit == 'M':
        yards = (distance_id / 100.0) * 1760
    # Y mode: already yards (sometimes)
    if yards < 1320:        # < 6f
        return 'sprint_short'
    if yards < 1760:        # < 8f
        return 'sprint'
    if yards < 1980:        # 8-9f
        return 'mid'
    if yards < 2200:        # 9-10f
        return 'route'
    return 'classic'


def _date(d: str | None):
    if not d:
        return None
    try:
        return datetime.strptime(d[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Run-style classification

def classify_run_style(early_positions: list[int]) -> str:
    """Given a list of 1st-call positions, classify run style.
    E   = avg <= 2 (front-runner)
    EP  = avg <= 4 (early-presser)
    P   = avg <= 7 (presser/stalker)
    S   = avg >  7 (closer)
    """
    if not early_positions:
        return 'unknown'
    avg = sum(early_positions) / len(early_positions)
    if avg <= 2.0:
        return 'E'
    if avg <= 4.0:
        return 'EP'
    if avg <= 7.0:
        return 'P'
    return 'S'


# ---------------------------------------------------------------------------
# Per-entry feature row

def build_entry_features(conn: sqlite3.Connection) -> pd.DataFrame:
    """Build one feature row per (race, entry).

    Uses only past performances dated strictly before today's race_date.
    """
    entries = pd.read_sql_query("""
        SELECT e.entry_id, e.race_id, e.program_number, e.post_position,
               e.horse_reg, e.weight_carried, e.equipment_code,
               r.race_date, r.track_id, r.race_number, r.race_type, r.grade,
               r.course_type AS today_course, r.surface AS today_surface,
               r.distance_id AS today_dist_id, r.distance_unit AS today_dist_unit,
               r.purse_usa, r.number_of_runners,
               h.horse_name, h.year_of_birth, h.sex, h.foaling_area
        FROM entries e
        JOIN races r ON e.race_id = r.race_id
        JOIN horses h ON e.horse_reg = h.registration_number
    """, conn)

    starts = pd.read_sql_query("""
        SELECT hs.start_id, hs.entry_id, hs.horse_reg, hs.pp_race_date,
               hs.pp_surface, hs.pp_distance_id, hs.pp_distance_unit,
               hs.pp_purse_usa, hs.pp_n_starters, hs.pp_grade,
               hs.official_finish, hs.speed_figure,
               hs.pace_figure_1, hs.pace_figure_2, hs.pace_figure_3,
               hs.class_rating, hs.odds_int, hs.favorite,
               hs.short_comment, hs.jockey_id, hs.trainer_id
        FROM horse_starts hs
    """, conn)
    starts['race_date'] = pd.to_datetime(starts['pp_race_date'], errors='coerce')
    starts['speed_clean'] = starts['speed_figure'].apply(_clean_speed)
    starts['pace1_clean'] = starts['pace_figure_1'].apply(_clean_pace)
    starts['pace2_clean'] = starts['pace_figure_2'].apply(_clean_pace)
    starts['pace3_clean'] = starts['pace_figure_3'].apply(_clean_pace)

    poc = pd.read_sql_query("""
        SELECT start_id, point_of_call, position_int, lengths_ahead, lengths_behind
        FROM point_of_call
    """, conn)
    # Pivot first-call position per start
    first_call = poc[poc['point_of_call'] == '1'][['start_id', 'position_int']].rename(
        columns={'position_int': 'pos_call_1'})
    finish_call = poc[poc['point_of_call'] == 'F'][['start_id', 'position_int', 'lengths_behind']].rename(
        columns={'position_int': 'pos_finish', 'lengths_behind': 'lengths_back_finish'})
    starts = starts.merge(first_call, on='start_id', how='left')
    starts = starts.merge(finish_call, on='start_id', how='left')

    # Build per-entry feature rows
    rows = []
    entries['race_date_dt'] = pd.to_datetime(entries['race_date'], errors='coerce')
    starts_by_entry = starts.groupby('entry_id')

    for _, e in entries.iterrows():
        eid = e['entry_id']
        today = e['race_date_dt']
        try:
            pps = starts_by_entry.get_group(eid).copy()
        except KeyError:
            pps = starts.iloc[0:0].copy()
        pps = pps[pps['race_date'] < today].sort_values('race_date', ascending=False)

        n_starts = len(pps)
        today_bucket = _distance_bucket(e['today_dist_id'], e['today_dist_unit'])

        # Surface / distance / route counts
        n_starts_route = int((pps['pp_distance_id'].fillna(0) >= 800).sum())  # rough proxy: 8f+
        n_starts_dirt = int((pps['pp_surface'] == 'D').sum())
        n_starts_today_surface = int((pps['pp_surface'] == e['today_surface']).sum()) if e['today_surface'] else 0
        bucket_counts = pps.apply(
            lambda r: _distance_bucket(r['pp_distance_id'], r['pp_distance_unit']) == today_bucket, axis=1)
        n_starts_today_distbucket = int(bucket_counts.sum()) if len(pps) else 0

        # Speed figs: last 3 valid
        spd3 = pps['speed_clean'].dropna().head(3).tolist()
        avg_speed_last3 = float(np.mean(spd3)) if spd3 else None
        best_speed_last3 = float(np.max(spd3)) if spd3 else None

        # Trend slope over last 4 valid speeds
        last4 = pps[['race_date', 'speed_clean']].dropna().head(4)
        if len(last4) >= 2:
            x = (last4['race_date'].astype('int64') // 86_400_000_000_000).to_numpy().astype(float)
            y = last4['speed_clean'].to_numpy().astype(float)
            x = x - x.mean()
            denom = (x ** 2).sum()
            slope = float((x * (y - y.mean())).sum() / denom) if denom > 0 else 0.0
        else:
            slope = None

        # Run-style from last 3 first-call positions
        first_calls = pps['pos_call_1'].dropna().head(3).astype(int).tolist()
        run_style = classify_run_style(first_calls)

        # Finish energy: positions gained from 1st call to finish, last 3 races
        if len(pps) >= 1:
            mvmt = (pps['pos_call_1'].fillna(0).astype(float) -
                    pps['pos_finish'].fillna(0).astype(float)).head(3)
            # Positive = passed horses
            finish_energy_raw = float(mvmt.mean()) if len(mvmt) else 0.0
        else:
            finish_energy_raw = 0.0

        # Pace fig avg (last 3)
        pace2_avg = pps['pace2_clean'].dropna().head(3).mean()
        pace2_avg = float(pace2_avg) if not pd.isna(pace2_avg) else None

        # Layoff
        if len(pps) and pd.notna(pps.iloc[0]['race_date']) and pd.notna(today):
            days_since_last = int((today - pps.iloc[0]['race_date']).days)
        else:
            days_since_last = None

        # Most-recent prior connection as proxy for today's connection
        # (PP-only datasets don't carry today's jockey/trainer; this is a noisy
        # but reasonable stand-in until live entry data is wired in.)
        if len(pps):
            last_pp = pps.iloc[0]
            jockey_id = int(last_pp['jockey_id']) if pd.notna(last_pp['jockey_id']) else -1
            trainer_id = int(last_pp['trainer_id']) if pd.notna(last_pp['trainer_id']) else -1
        else:
            jockey_id = -1
            trainer_id = -1

        rows.append({
            'entry_id': eid,
            'race_id': e['race_id'],
            'program_number': e['program_number'],
            'post_position': e['post_position'],
            'horse_reg': e['horse_reg'],
            'horse_name': e['horse_name'],
            'jockey_id': jockey_id,
            'trainer_id': trainer_id,
            'today_surface': e['today_surface'],
            'today_dist_bucket': today_bucket,
            'today_n_runners': e['number_of_runners'],
            'race_type': e['race_type'],
            'grade': e['grade'],
            'n_starts': n_starts,
            'n_starts_route': n_starts_route,
            'n_starts_dirt': n_starts_dirt,
            'n_starts_today_surface': n_starts_today_surface,
            'n_starts_today_distbucket': n_starts_today_distbucket,
            'avg_speed_last3': avg_speed_last3,
            'best_speed_last3': best_speed_last3,
            'speed_trend_slope': slope,
            'pace2_avg_last3': pace2_avg,
            'finish_energy_raw': finish_energy_raw,
            'run_style': run_style,
            'days_since_last': days_since_last,
            'layoff_flag': int(days_since_last is not None and days_since_last >= 60),
        })

    df = pd.DataFrame(rows)

    # ----- Race-level normalizations: z-scores within race -----
    def _z(s):
        s = s.astype(float)
        m, sd = s.mean(skipna=True), s.std(skipna=True)
        if not sd or pd.isna(sd):
            return s * 0.0
        return (s - m) / sd

    df['recent_fig_z'] = df.groupby('race_id')['avg_speed_last3'].transform(_z)
    df['late_fig_z'] = df.groupby('race_id')['finish_energy_raw'].transform(_z)
    df['early_pace_z'] = df.groupby('race_id')['pace2_avg_last3'].transform(_z)

    # 0-10 scaled scores
    def _to_0_10(s, mn=-2.0, mx=2.0):
        s = s.fillna(0).clip(mn, mx)
        return ((s - mn) / (mx - mn) * 10).round(2)

    df['devcurve_score'] = _to_0_10(df['speed_trend_slope'].fillna(0), mn=-0.5, mx=0.5)
    df['finish_energy_score'] = _to_0_10(df['late_fig_z'])
    df['recent_form_score'] = _to_0_10(df['recent_fig_z'])

    # ---- Race-shape PaceFit ----
    # Lone-speed bonus if only one E in race; collapse risk if >=3 E/EP types.
    def _pacefit(group: pd.DataFrame) -> pd.Series:
        styles = group['run_style']
        n_E = (styles == 'E').sum()
        n_EP = (styles == 'EP').sum()
        n_speed = n_E + n_EP
        out = []
        for _, r in group.iterrows():
            base = 5.0
            rs = r['run_style']
            if n_E == 1 and rs == 'E':
                base = 9.0       # lone speed wins often
            elif n_speed >= 3 and rs in ('S', 'P'):
                base = 8.0       # closers benefit from pace meltdown
            elif n_speed >= 3 and rs in ('E', 'EP'):
                base = 3.0       # speed dueling is bad
            elif rs == 'EP':
                base = 6.5
            elif rs == 'P':
                base = 6.0
            elif rs == 'S':
                base = 5.5
            out.append(base)
        return pd.Series(out, index=group.index)

    df['pacefit_score'] = df.groupby('race_id', group_keys=False).apply(_pacefit)

    # ---- DistanceProj: simple proxy from route/distance experience ----
    df['distance_proj_score'] = (
        2.0 + 4.0 * (df['n_starts_today_distbucket'] >= 1).astype(float)
        + 2.0 * (df['n_starts_today_distbucket'] >= 3).astype(float)
        + 2.0 * (df['n_starts_today_surface'] >= 3).astype(float)
    ).clip(0, 10)

    # ---- Workout features (today-side; trainer intent + fitness)
    try:
        from .workout_features import attach_workout_features
        df = attach_workout_features(conn, df)
    except Exception as e:
        # Don't crash feature build if workouts table is missing/empty
        for c in ('workout_score',):
            if c not in df.columns:
                df[c] = 5.0

    # Publicness: pull from odds_features if the table exists & has rows; else 5.0 (neutral)
    try:
        of = pd.read_sql_query(
            "SELECT entry_id, publicness_score AS pub_market FROM odds_features",
            conn,
        )
        if not of.empty:
            df = df.merge(of, on='entry_id', how='left')
            df['publicness_score'] = df['pub_market'].fillna(5.0)
            df = df.drop(columns=['pub_market'])
        else:
            df['publicness_score'] = 5.0
    except Exception:
        # odds_features table doesn't exist yet; fine
        df['publicness_score'] = 5.0

    return df
