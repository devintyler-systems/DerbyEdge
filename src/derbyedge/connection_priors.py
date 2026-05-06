"""Connection priors: jockey, trainer, jockey-trainer combo win rates.

Critical rule: every prior is AS-OF the row's race_date. We never let a future
race contribute to a past row's feature. Implemented as expanding-window
counts using a single sorted pass.

Priors computed per row:
    jockey_starts_prior        # of prior starts for this jockey
    jockey_wins_prior
    jockey_winrate_prior       Smoothed: (wins + a) / (starts + a + b), Beta(a,b) prior
    jockey_winrate_surface     Same, restricted to today's surface
    trainer_starts_prior
    trainer_wins_prior
    trainer_winrate_prior
    trainer_winrate_surface
    jt_combo_starts_prior      # of prior starts for this exact jockey+trainer pair
    jt_combo_wins_prior
    jt_combo_winrate_prior

Smoothing: Beta(2, 12) prior reflects ~14% league win rate with mild shrinkage.
Connections with <5 starts get pulled hard toward the league rate, which is
correct because their personal rate is unreliable.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

import numpy as np
import pandas as pd


# League prior: roughly 14% win rate, weight ~14 effective starts
PRIOR_ALPHA = 2.0   # pseudo-wins
PRIOR_BETA = 12.0   # pseudo-losses


def _smoothed_rate(wins: float, starts: float,
                   alpha: float = PRIOR_ALPHA, beta: float = PRIOR_BETA) -> float:
    return (wins + alpha) / (starts + alpha + beta)


def add_connection_priors(df: pd.DataFrame,
                          alpha: float = PRIOR_ALPHA,
                          beta: float = PRIOR_BETA) -> pd.DataFrame:
    """Add connection prior features. Operates on the historical training set.

    Required input columns:
        race_date, jockey_id, trainer_id, pp_surface, won

    Adds:
        jockey_starts_prior, jockey_wins_prior, jockey_winrate_prior,
        jockey_winrate_surface,
        trainer_starts_prior, trainer_wins_prior, trainer_winrate_prior,
        trainer_winrate_surface,
        jt_combo_starts_prior, jt_combo_wins_prior, jt_combo_winrate_prior
    """
    # Sort once by date so an expanding pass is safe
    df = df.copy()
    df['race_date'] = pd.to_datetime(df['race_date'])
    df = df.sort_values('race_date', kind='mergesort').reset_index(drop=False)
    df = df.rename(columns={'index': '_orig_index'})

    # Running counters
    j_starts = defaultdict(int)
    j_wins = defaultdict(int)
    j_starts_surf = defaultdict(int)   # keyed by (jockey_id, surface)
    j_wins_surf = defaultdict(int)

    t_starts = defaultdict(int)
    t_wins = defaultdict(int)
    t_starts_surf = defaultdict(int)
    t_wins_surf = defaultdict(int)

    jt_starts = defaultdict(int)
    jt_wins = defaultdict(int)

    n = len(df)
    out_cols = {
        'jockey_starts_prior': np.zeros(n, dtype=int),
        'jockey_wins_prior': np.zeros(n, dtype=int),
        'jockey_winrate_prior': np.zeros(n, dtype=float),
        'jockey_winrate_surface': np.zeros(n, dtype=float),
        'trainer_starts_prior': np.zeros(n, dtype=int),
        'trainer_wins_prior': np.zeros(n, dtype=int),
        'trainer_winrate_prior': np.zeros(n, dtype=float),
        'trainer_winrate_surface': np.zeros(n, dtype=float),
        'jt_combo_starts_prior': np.zeros(n, dtype=int),
        'jt_combo_wins_prior': np.zeros(n, dtype=int),
        'jt_combo_winrate_prior': np.zeros(n, dtype=float),
    }

    # Iterate in date order. Read priors BEFORE updating counters so the
    # current row sees only past info.
    j_arr = df['jockey_id'].fillna(-1).astype(int).to_numpy()
    t_arr = df['trainer_id'].fillna(-1).astype(int).to_numpy()
    s_arr = df['pp_surface'].fillna('U').astype(str).to_numpy()
    w_arr = df['won'].fillna(0).astype(int).to_numpy()

    for i in range(n):
        j, t, s, w = j_arr[i], t_arr[i], s_arr[i], w_arr[i]

        # Read priors (PRE-update)
        out_cols['jockey_starts_prior'][i] = j_starts[j]
        out_cols['jockey_wins_prior'][i] = j_wins[j]
        out_cols['jockey_winrate_prior'][i] = _smoothed_rate(j_wins[j], j_starts[j], alpha, beta)
        out_cols['jockey_winrate_surface'][i] = _smoothed_rate(
            j_wins_surf[(j, s)], j_starts_surf[(j, s)], alpha, beta
        )

        out_cols['trainer_starts_prior'][i] = t_starts[t]
        out_cols['trainer_wins_prior'][i] = t_wins[t]
        out_cols['trainer_winrate_prior'][i] = _smoothed_rate(t_wins[t], t_starts[t], alpha, beta)
        out_cols['trainer_winrate_surface'][i] = _smoothed_rate(
            t_wins_surf[(t, s)], t_starts_surf[(t, s)], alpha, beta
        )

        jt_key = (j, t)
        out_cols['jt_combo_starts_prior'][i] = jt_starts[jt_key]
        out_cols['jt_combo_wins_prior'][i] = jt_wins[jt_key]
        out_cols['jt_combo_winrate_prior'][i] = _smoothed_rate(
            jt_wins[jt_key], jt_starts[jt_key], alpha, beta
        )

        # POST-update: this row's outcome contributes to FUTURE rows only
        if j > 0:
            j_starts[j] += 1
            j_wins[j] += w
            j_starts_surf[(j, s)] += 1
            j_wins_surf[(j, s)] += w
        if t > 0:
            t_starts[t] += 1
            t_wins[t] += w
            t_starts_surf[(t, s)] += 1
            t_wins_surf[(t, s)] += w
        if j > 0 and t > 0:
            jt_starts[jt_key] += 1
            jt_wins[jt_key] += w

    for col, arr in out_cols.items():
        df[col] = arr

    df = df.sort_values('_orig_index').drop(columns=['_orig_index']).reset_index(drop=True)
    return df


def snapshot_connection_state(historical_df: pd.DataFrame,
                              cutoff_date: pd.Timestamp,
                              alpha: float = PRIOR_ALPHA,
                              beta: float = PRIOR_BETA) -> dict:
    """Build a connection snapshot for scoring TODAY's entries.

    Returns dict of dicts keyed by id, with smoothed win rates as of cutoff_date.
    Used by features.py to attach priors to today's entries without re-running
    the full training set.

    Output:
        {
            'jockey':         {jockey_id: {'starts','wins','rate'}},
            'jockey_surface': {(jockey_id, surface): {...}},
            'trainer':        {...},
            'trainer_surface':{...},
            'jt_combo':       {(j,t): {...}},
            'alpha': alpha, 'beta': beta,
        }
    """
    df = historical_df.copy()
    df['race_date'] = pd.to_datetime(df['race_date'])
    df = df[df['race_date'] < pd.to_datetime(cutoff_date)]

    j = df.groupby('jockey_id').agg(starts=('won', 'size'), wins=('won', 'sum'))
    t = df.groupby('trainer_id').agg(starts=('won', 'size'), wins=('won', 'sum'))
    j_s = df.groupby(['jockey_id', 'pp_surface']).agg(starts=('won', 'size'), wins=('won', 'sum'))
    t_s = df.groupby(['trainer_id', 'pp_surface']).agg(starts=('won', 'size'), wins=('won', 'sum'))
    jt = df.groupby(['jockey_id', 'trainer_id']).agg(starts=('won', 'size'), wins=('won', 'sum'))

    def _to_dict(g):
        return {
            idx: {
                'starts': int(row['starts']),
                'wins': int(row['wins']),
                'rate': _smoothed_rate(row['wins'], row['starts'], alpha, beta),
            }
            for idx, row in g.iterrows()
        }

    return {
        'jockey': _to_dict(j),
        'jockey_surface': _to_dict(j_s),
        'trainer': _to_dict(t),
        'trainer_surface': _to_dict(t_s),
        'jt_combo': _to_dict(jt),
        'alpha': alpha,
        'beta': beta,
        'league_rate': _smoothed_rate(0, 0, alpha, beta),
    }


def attach_priors_to_entries(entries_features: pd.DataFrame,
                             snapshot: dict,
                             jockey_col: str = 'jockey_id',
                             trainer_col: str = 'trainer_id',
                             surface_col: str = 'today_surface') -> pd.DataFrame:
    """Add the same prior features to today's-entry feature rows."""
    df = entries_features.copy()
    league = snapshot['league_rate']
    alpha = snapshot['alpha']
    beta = snapshot['beta']

    def lookup(table: dict, key, want: str):
        rec = table.get(key)
        if rec is None:
            if want == 'rate':
                return league
            return 0
        return rec[want]

    j = df[jockey_col].fillna(-1).astype(int).to_numpy() if jockey_col in df.columns else None
    t = df[trainer_col].fillna(-1).astype(int).to_numpy() if trainer_col in df.columns else None
    s = df[surface_col].fillna('U').astype(str).to_numpy() if surface_col in df.columns else None

    n = len(df)
    if j is None or t is None or s is None:
        # Can't compute; fill league rate
        for col in ('jockey_winrate_prior', 'jockey_winrate_surface',
                    'trainer_winrate_prior', 'trainer_winrate_surface',
                    'jt_combo_winrate_prior'):
            df[col] = league
        for col in ('jockey_starts_prior', 'jockey_wins_prior',
                    'trainer_starts_prior', 'trainer_wins_prior',
                    'jt_combo_starts_prior', 'jt_combo_wins_prior'):
            df[col] = 0
        return df

    out = {
        'jockey_starts_prior': np.zeros(n, int),
        'jockey_wins_prior': np.zeros(n, int),
        'jockey_winrate_prior': np.full(n, league),
        'jockey_winrate_surface': np.full(n, league),
        'trainer_starts_prior': np.zeros(n, int),
        'trainer_wins_prior': np.zeros(n, int),
        'trainer_winrate_prior': np.full(n, league),
        'trainer_winrate_surface': np.full(n, league),
        'jt_combo_starts_prior': np.zeros(n, int),
        'jt_combo_wins_prior': np.zeros(n, int),
        'jt_combo_winrate_prior': np.full(n, league),
    }

    for i in range(n):
        ji, ti, si = int(j[i]), int(t[i]), str(s[i])
        out['jockey_starts_prior'][i] = lookup(snapshot['jockey'], ji, 'starts')
        out['jockey_wins_prior'][i] = lookup(snapshot['jockey'], ji, 'wins')
        out['jockey_winrate_prior'][i] = lookup(snapshot['jockey'], ji, 'rate')
        out['jockey_winrate_surface'][i] = lookup(snapshot['jockey_surface'], (ji, si), 'rate')
        out['trainer_starts_prior'][i] = lookup(snapshot['trainer'], ti, 'starts')
        out['trainer_wins_prior'][i] = lookup(snapshot['trainer'], ti, 'wins')
        out['trainer_winrate_prior'][i] = lookup(snapshot['trainer'], ti, 'rate')
        out['trainer_winrate_surface'][i] = lookup(snapshot['trainer_surface'], (ti, si), 'rate')
        out['jt_combo_starts_prior'][i] = lookup(snapshot['jt_combo'], (ji, ti), 'starts')
        out['jt_combo_wins_prior'][i] = lookup(snapshot['jt_combo'], (ji, ti), 'wins')
        out['jt_combo_winrate_prior'][i] = lookup(snapshot['jt_combo'], (ji, ti), 'rate')

    for col, arr in out.items():
        df[col] = arr
    return df
