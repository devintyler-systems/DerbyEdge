"""Edge calculator: combine model_prob with market_prob_devig to produce
fair odds, edge, EV, Kelly fraction, and bet tag per entry.

Inputs:
    - entry_features (from features.build_entry_features)
    - odds_features  (from odds_features.build_odds_features)
    - model_probs    (DataFrame with columns: entry_id, model_prob)
                     If absent, falls back to a placeholder uniform distribution
                     within race (1/N) — this is BLATANTLY wrong and only useful
                     for plumbing tests until the real model lands.

Outputs:
    DataFrame with: entry_id, race_id, program_number, model_prob, market_prob,
                    decimal_odds_used, fair_decimal, edge, ev, kelly_frac, bet_tag
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import pandas as pd

from .odds_math import (
    edge,
    expected_value,
    kelly_fraction,
    bet_tag,
)


def _placeholder_uniform_prob(entries: pd.DataFrame) -> pd.DataFrame:
    """Equal mass within race — explicitly NOT a model. Plumbing only."""
    counts = entries.groupby('race_id')['entry_id'].transform('count')
    out = entries[['entry_id', 'race_id']].copy()
    out['model_prob'] = 1.0 / counts
    return out


def build_edge_table(conn: sqlite3.Connection,
                     model_probs: Optional[pd.DataFrame] = None,
                     model_path: Optional[str] = None,
                     min_edge: float = 0.20,
                     strong_edge: float = 0.40) -> pd.DataFrame:
    entries = pd.read_sql_query(
        """SELECT e.entry_id, e.race_id, e.program_number, e.post_position,
                  h.horse_name
           FROM entries e
           JOIN horses h ON e.horse_reg = h.registration_number""",
        conn,
    )

    odds_feat = pd.read_sql_query("SELECT * FROM odds_features", conn)

    if model_probs is None:
        if model_path is not None:
            from .scoring import score_entries
            scored = score_entries(conn, model_path)
            model_probs = scored[['entry_id', 'model_prob']]
        else:
            model_probs = _placeholder_uniform_prob(entries)
    model_probs = model_probs[['entry_id', 'model_prob']]

    df = entries.merge(model_probs, on='entry_id', how='left')
    df = df.merge(
        odds_feat[['entry_id', 'best_dec_now', 'best_book_now',
                   'market_prob_devig', 'morning_line_dec']],
        on='entry_id', how='left',
    )

    # Use best_dec_now if present, else morning_line_dec, else null
    df['decimal_odds_used'] = df['best_dec_now'].fillna(df['morning_line_dec'])
    df['market_prob'] = df['market_prob_devig'].fillna(
        df['morning_line_dec'].apply(lambda d: 1.0 / d if d and d > 1.0 else None)
    )

    df['fair_decimal'] = df['model_prob'].apply(
        lambda p: round(1.0 / p, 3) if p and p > 0 else None
    )

    rows = []
    for _, r in df.iterrows():
        mp = r['model_prob']
        mkt = r['market_prob']
        odec = r['decimal_odds_used']
        if pd.isna(mp) or pd.isna(mkt) or pd.isna(odec) or mp is None:
            rows.append({'edge': None, 'ev': None, 'kelly_frac': None,
                         'bet_tag': 'NO_MARKET'})
            continue
        e = edge(float(mp), float(mkt))
        ev = expected_value(float(mp), float(odec))
        kf = kelly_fraction(float(mp), float(odec))
        tag = bet_tag(float(mp), float(mkt), float(odec),
                      min_edge=min_edge, strong_edge=strong_edge)
        rows.append({'edge': round(e, 4), 'ev': round(ev, 4),
                     'kelly_frac': kf, 'bet_tag': tag})

    df = pd.concat([df.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    cols = ['entry_id', 'race_id', 'program_number', 'post_position', 'horse_name',
            'model_prob', 'market_prob', 'decimal_odds_used', 'best_book_now',
            'fair_decimal', 'edge', 'ev', 'kelly_frac', 'bet_tag']
    return df[cols]
