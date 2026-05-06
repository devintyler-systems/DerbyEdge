"""Tests for workout_features. Verifies no future leak + correct scoring."""
from __future__ import annotations

import pandas as pd
import pytest

from derbyedge.workout_features import (
    compute_workout_features,
    _composite_score,
    _neutral_row,
    GAP_TOO_LONG_DAYS,
    LONG_WORK_DISTANCE_F,
    HANDILY,
)


def _toy_workouts():
    """4 workouts for entry A, 0 for entry B."""
    return pd.DataFrame([
        # Entry A: 4 evenly-spaced 4F breezes ending 3 days before race (2023-02-01)
        {'entry_id': 'A', 'workout_date': '2023-01-29', 'distance_id': 400,
         'type_of_workout': 'B', 'horse_reg': 'X'},
        {'entry_id': 'A', 'workout_date': '2023-01-22', 'distance_id': 400,
         'type_of_workout': 'B', 'horse_reg': 'X'},
        {'entry_id': 'A', 'workout_date': '2023-01-15', 'distance_id': 500,
         'type_of_workout': 'H', 'horse_reg': 'X'},
        {'entry_id': 'A', 'workout_date': '2023-01-08', 'distance_id': 400,
         'type_of_workout': 'B', 'horse_reg': 'X'},
    ])


def _race_dates():
    return pd.DataFrame([
        {'entry_id': 'A', 'race_date': '2023-02-01'},
        {'entry_id': 'B', 'race_date': '2023-02-01'},
    ])


def test_entry_with_no_workouts_gets_neutral_row():
    out = compute_workout_features(_toy_workouts(), _race_dates())
    b = out[out['entry_id'] == 'B'].iloc[0]
    assert b['has_workouts'] == 0
    assert b['n_workouts_30d'] == 0
    assert b['workout_score'] == 3.0
    assert b['gap_too_long_flag'] == 1


def test_entry_with_sharp_pattern_scores_high():
    out = compute_workout_features(_toy_workouts(), _race_dates())
    a = out[out['entry_id'] == 'A'].iloc[0]
    assert a['has_workouts'] == 1
    assert a['n_workouts_30d'] == 4
    assert a['days_since_last_work'] == 3
    assert a['has_recent_handily'] == 1   # H on 1/15 within 30d
    assert a['regular_pattern_flag'] == 1  # 7-day spacing
    assert a['workout_score'] >= 8.0


def test_no_leak_workout_after_race_date_excluded():
    """A workout dated AFTER race_date must not contribute."""
    wks = _toy_workouts().copy()
    # Add a future work
    wks = pd.concat([wks, pd.DataFrame([{
        'entry_id': 'A', 'workout_date': '2023-02-15', 'distance_id': 600,
        'type_of_workout': 'H', 'horse_reg': 'X',
    }])], ignore_index=True)
    wks['workout_date'] = pd.to_datetime(wks['workout_date'])
    out_with_future = compute_workout_features(wks, _race_dates())
    out_without = compute_workout_features(_toy_workouts(), _race_dates())
    a_with = out_with_future[out_with_future['entry_id'] == 'A'].iloc[0]
    a_without = out_without[out_without['entry_id'] == 'A'].iloc[0]
    # All key features must be identical
    for col in ['n_workouts_60d', 'n_workouts_30d', 'days_since_last_work',
                'longest_work_furlongs_60d', 'has_long_work_60d',
                'workout_score', 'has_recent_handily']:
        assert a_with[col] == a_without[col], f"{col} leaked future info"


def test_stale_horse_gets_low_score():
    """Last work 50 days ago, no recent volume."""
    wks = pd.DataFrame([{
        'entry_id': 'A', 'workout_date': '2022-12-13', 'distance_id': 400,
        'type_of_workout': 'B', 'horse_reg': 'X',
    }])
    wks['workout_date'] = pd.to_datetime(wks['workout_date'])
    out = compute_workout_features(wks, _race_dates()[:1])
    a = out.iloc[0]
    assert a['days_since_last_work'] == 50
    assert a['gap_too_long_flag'] == 1
    assert a['workout_score'] <= 3.5


def test_long_work_flag_triggers_at_6f():
    wks = pd.DataFrame([{
        'entry_id': 'A', 'workout_date': '2023-01-25', 'distance_id': 600,
        'type_of_workout': 'B', 'horse_reg': 'X',
    }])
    wks['workout_date'] = pd.to_datetime(wks['workout_date'])
    out = compute_workout_features(wks, _race_dates()[:1])
    assert out.iloc[0]['has_long_work_60d'] == 1
    assert out.iloc[0]['longest_work_furlongs_60d'] == 6.0


def test_long_work_flag_off_for_short_work():
    wks = pd.DataFrame([{
        'entry_id': 'A', 'workout_date': '2023-01-25', 'distance_id': 400,
        'type_of_workout': 'B', 'horse_reg': 'X',
    }])
    wks['workout_date'] = pd.to_datetime(wks['workout_date'])
    out = compute_workout_features(wks, _race_dates()[:1])
    assert out.iloc[0]['has_long_work_60d'] == 0


def test_composite_score_ranges():
    # Empty / no work
    s_low = _composite_score(0, 0, days_since_last=30, has_recent_handily=0,
                              has_long_work_60d=0, regular_pattern=0,
                              total_f_60d=0)
    s_high = _composite_score(4, 4, days_since_last=4, has_recent_handily=1,
                               has_long_work_60d=1, regular_pattern=1,
                               total_f_60d=22)
    assert 0 <= s_low <= 10
    assert 0 <= s_high <= 10
    assert s_high > s_low + 4


def test_handily_flag_30d_window():
    """H work outside 30d window should NOT trigger has_recent_handily."""
    wks = pd.DataFrame([
        {'entry_id': 'A', 'workout_date': '2022-12-15', 'distance_id': 400,
         'type_of_workout': 'H', 'horse_reg': 'X'},   # ~48 days before 2/1
        {'entry_id': 'A', 'workout_date': '2023-01-28', 'distance_id': 400,
         'type_of_workout': 'B', 'horse_reg': 'X'},   # 4d before, breeze
    ])
    wks['workout_date'] = pd.to_datetime(wks['workout_date'])
    out = compute_workout_features(wks, _race_dates()[:1])
    assert out.iloc[0]['has_recent_handily'] == 0
    assert out.iloc[0]['n_handily_30d'] == 0


def test_neutral_row_has_all_required_columns():
    """Verify neutral row has the same keys as a real entry."""
    real_keys = set(compute_workout_features(_toy_workouts(),
                                              _race_dates()[:1]).columns)
    neutral_keys = set(_neutral_row('A').keys())
    assert real_keys == neutral_keys


def test_output_keyed_by_entry_id_no_dupes():
    out = compute_workout_features(_toy_workouts(), _race_dates())
    assert out['entry_id'].is_unique
    assert len(out) == 2
