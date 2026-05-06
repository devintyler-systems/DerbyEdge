"""Tests for connection priors. Critical: verify NO future leak."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from derbyedge.connection_priors import (
    add_connection_priors,
    snapshot_connection_state,
    attach_priors_to_entries,
    PRIOR_ALPHA, PRIOR_BETA, _smoothed_rate,
)


def _toy_history():
    """Hand-built sequence: jockey 1 wins races 1,2; jockey 2 wins race 3 only."""
    return pd.DataFrame([
        {'race_date': '2023-01-01', 'jockey_id': 1, 'trainer_id': 10, 'pp_surface': 'D', 'won': 1},
        {'race_date': '2023-01-02', 'jockey_id': 1, 'trainer_id': 10, 'pp_surface': 'D', 'won': 1},
        {'race_date': '2023-01-03', 'jockey_id': 1, 'trainer_id': 10, 'pp_surface': 'D', 'won': 0},
        {'race_date': '2023-01-04', 'jockey_id': 2, 'trainer_id': 20, 'pp_surface': 'T', 'won': 1},
        {'race_date': '2023-01-05', 'jockey_id': 1, 'trainer_id': 10, 'pp_surface': 'T', 'won': 0},
    ])


def test_first_row_has_zero_priors():
    """The very first row of any connection's history must have zero starts/wins."""
    df = _toy_history()
    out = add_connection_priors(df)
    first_j1 = out.iloc[0]
    assert first_j1['jockey_starts_prior'] == 0
    assert first_j1['jockey_wins_prior'] == 0
    assert first_j1['trainer_starts_prior'] == 0
    assert first_j1['jt_combo_starts_prior'] == 0
    # Smoothed rate falls back to league (alpha / (alpha+beta))
    expected_league = PRIOR_ALPHA / (PRIOR_ALPHA + PRIOR_BETA)
    assert abs(first_j1['jockey_winrate_prior'] - expected_league) < 1e-9


def test_priors_grow_correctly_in_order():
    df = _toy_history()
    out = add_connection_priors(df).sort_values('race_date').reset_index(drop=True)
    # Row index 1 (2023-01-02) should see jockey 1 with 1 prior start, 1 prior win
    assert out.loc[1, 'jockey_starts_prior'] == 1
    assert out.loc[1, 'jockey_wins_prior'] == 1
    # Row index 2 (2023-01-03): 2 starts, 2 wins
    assert out.loc[2, 'jockey_starts_prior'] == 2
    assert out.loc[2, 'jockey_wins_prior'] == 2
    # Row index 4 (2023-01-05): 3 starts, 2 wins
    assert out.loc[4, 'jockey_starts_prior'] == 3
    assert out.loc[4, 'jockey_wins_prior'] == 2


def test_no_leak_future_outcome_does_not_change_past_row():
    """Critical: appending a future row must NOT change any past row's priors."""
    df = _toy_history()
    base = add_connection_priors(df).sort_values('race_date').reset_index(drop=True)

    # Append a future win for jockey 1
    df_extra = pd.concat([
        df,
        pd.DataFrame([{'race_date': '2023-02-01', 'jockey_id': 1, 'trainer_id': 10,
                       'pp_surface': 'D', 'won': 1}])
    ], ignore_index=True)
    out_extra = add_connection_priors(df_extra).sort_values('race_date').reset_index(drop=True)
    # First 5 rows must match exactly
    cols = ['jockey_starts_prior', 'jockey_wins_prior', 'jockey_winrate_prior',
            'jockey_winrate_surface', 'trainer_starts_prior', 'trainer_wins_prior']
    for c in cols:
        np.testing.assert_array_equal(base[c].to_numpy(), out_extra[c].iloc[:5].to_numpy())


def test_surface_specific_counts_separate():
    df = _toy_history()
    out = add_connection_priors(df).sort_values('race_date').reset_index(drop=True)
    # Row 4 (jockey 1, T surface): on T this is jockey 1's first start, so surface starts=0
    assert out.loc[4, 'jockey_id'] == 1
    assert out.loc[4, 'pp_surface'] == 'T'
    # Smoothed rate on T should equal league prior because no T starts yet for j=1
    expected = PRIOR_ALPHA / (PRIOR_ALPHA + PRIOR_BETA)
    assert abs(out.loc[4, 'jockey_winrate_surface'] - expected) < 1e-9


def test_jt_combo_independent_of_individual():
    """JT combo (1,10) wins twice. Combo (2,10) is brand new."""
    df = pd.concat([
        _toy_history(),
        pd.DataFrame([{'race_date': '2023-01-06', 'jockey_id': 2, 'trainer_id': 10,
                       'pp_surface': 'D', 'won': 0}])
    ], ignore_index=True)
    out = add_connection_priors(df).sort_values('race_date').reset_index(drop=True)
    last = out.iloc[-1]
    assert last['jt_combo_starts_prior'] == 0   # (2,10) never seen before
    assert last['trainer_starts_prior'] == 4    # trainer 10 had 4 prior starts


def test_smoothed_rate_pulls_toward_league():
    """A connection with 1 win in 1 start should not have rate=1.0; smoothing pulls down."""
    rate = _smoothed_rate(wins=1, starts=1)
    league = _smoothed_rate(0, 0)
    assert rate > league   # 1-for-1 should be above league
    assert rate < 0.5      # but heavily shrunk


def test_snapshot_respects_cutoff():
    df = _toy_history()
    cutoff = pd.Timestamp('2023-01-04')
    snap = snapshot_connection_state(df, cutoff_date=cutoff)
    # Only rows with date < 2023-01-04 contribute (3 rows for jockey 1)
    assert snap['jockey'][1]['starts'] == 3
    assert snap['jockey'][1]['wins'] == 2
    # Jockey 2 has no rows before cutoff
    assert 2 not in snap['jockey']


def test_attach_priors_unseen_connection_falls_back_to_league():
    df = _toy_history()
    snap = snapshot_connection_state(df, cutoff_date=pd.Timestamp('2023-02-01'))
    today_entries = pd.DataFrame([
        {'jockey_id': 999, 'trainer_id': 999, 'today_surface': 'D'},
        {'jockey_id': 1, 'trainer_id': 10, 'today_surface': 'D'},
    ])
    out = attach_priors_to_entries(today_entries, snap)
    # Unseen connection -> league rate
    assert abs(out.loc[0, 'jockey_winrate_prior'] - snap['league_rate']) < 1e-9
    assert out.loc[0, 'jockey_starts_prior'] == 0
    # Known connection -> matches snapshot (jockey 1 had 4 starts, 2 wins by 2/1)
    assert out.loc[1, 'jockey_starts_prior'] == 4
    assert out.loc[1, 'jockey_wins_prior'] == 2


def test_preserves_input_row_order():
    """add_connection_priors must return rows in original input order."""
    df = _toy_history()
    df_shuffled = df.sample(frac=1, random_state=7).reset_index(drop=True)
    out = add_connection_priors(df_shuffled)
    # race_date column should match input order
    pd.testing.assert_series_equal(
        pd.to_datetime(df_shuffled['race_date']).reset_index(drop=True),
        out['race_date'].reset_index(drop=True),
        check_names=False,
    )


def test_missing_ids_handled_gracefully():
    """Rows with NaN jockey_id should not crash; they accumulate under id=-1."""
    df = pd.DataFrame([
        {'race_date': '2023-01-01', 'jockey_id': None, 'trainer_id': 10,
         'pp_surface': 'D', 'won': 1},
        {'race_date': '2023-01-02', 'jockey_id': 1, 'trainer_id': None,
         'pp_surface': 'D', 'won': 0},
    ])
    out = add_connection_priors(df)
    assert len(out) == 2
    # No exceptions, columns present
    for c in ('jockey_winrate_prior', 'trainer_winrate_prior'):
        assert c in out.columns
