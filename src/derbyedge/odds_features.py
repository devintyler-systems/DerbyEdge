"""Per-entry odds features computed from odds_snapshots.

Outputs to odds_features table (one row per entry).

Computes:
    morning_line_dec / _prob   - From book_id='morningline' if present
    best_dec_now               - Highest decimal across non-ML books (best for bettor)
    best_book_now              - Which book offered it
    median_dec_now             - Median decimal across non-ML books
    market_prob_devig          - Avg of devigged probs across books
    publicness_score           - 0-10 derived from rank of market_prob_devig
                                 vs equal-mass (1/N). High score = heavily backed.
    odds_drift_pct             - vs earliest snapshot today
    drift_direction            - shortening / drifting / flat
    n_books                    - distinct books with current odds
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from statistics import median

import pandas as pd

from .odds_math import (
    decimal_to_implied_prob,
    devig_proportional,
    drift_pct,
    drift_direction,
)


def _latest_per_book(snapshots: pd.DataFrame) -> pd.DataFrame:
    """For each (race_id, book_id, program_number), keep the most recent snapshot."""
    if snapshots.empty:
        return snapshots
    s = snapshots.sort_values('captured_at')
    return s.groupby(['race_id', 'book_id', 'program_number'], as_index=False).tail(1)


def _earliest_per_book(snapshots: pd.DataFrame) -> pd.DataFrame:
    if snapshots.empty:
        return snapshots
    s = snapshots.sort_values('captured_at')
    return s.groupby(['race_id', 'book_id', 'program_number'], as_index=False).head(1)


def build_odds_features(conn: sqlite3.Connection) -> pd.DataFrame:
    snaps = pd.read_sql_query(
        "SELECT * FROM odds_snapshots WHERE is_scratched=0",
        conn,
    )
    entries = pd.read_sql_query(
        "SELECT entry_id, race_id, program_number FROM entries",
        conn,
    )
    if snaps.empty:
        # Empty result set with right columns
        return pd.DataFrame(columns=[
            'entry_id', 'race_id', 'morning_line_dec', 'morning_line_prob',
            'best_dec_now', 'best_book_now', 'median_dec_now',
            'market_prob_devig', 'publicness_score',
            'odds_drift_pct', 'drift_direction', 'n_books', 'last_updated_at',
        ])

    latest = _latest_per_book(snaps)
    earliest = _earliest_per_book(snaps)

    # ---- Devig per (race_id, book_id, captured_at) on the latest snapshot per book
    # We compute one devigged prob per runner per book, then average across books.
    devig_rows = []
    non_ml = latest[latest['is_morning_line'] == 0].copy()
    for (race_id, book_id), grp in non_ml.groupby(['race_id', 'book_id']):
        decs = grp['decimal_odds'].tolist()
        probs = devig_proportional(decs)
        for (idx, _), p in zip(grp.iterrows(), probs):
            devig_rows.append({
                'race_id': race_id,
                'book_id': book_id,
                'program_number': non_ml.at[idx, 'program_number'],
                'devig_prob': p,
            })
    devig_df = pd.DataFrame(devig_rows)

    # ---- Morning line per entry
    ml = latest[latest['is_morning_line'] == 1][[
        'race_id', 'program_number', 'decimal_odds'
    ]].rename(columns={'decimal_odds': 'morning_line_dec'})
    ml['morning_line_prob'] = ml['morning_line_dec'].apply(decimal_to_implied_prob)

    # ---- Best / median / n_books per entry across non-ML books (no apply tricks)
    rows_agg = []
    if not non_ml.empty:
        for (race_id, program_number), grp in non_ml.groupby(['race_id', 'program_number']):
            decs = grp['decimal_odds'].dropna().tolist()
            if not decs:
                rows_agg.append({
                    'race_id': race_id, 'program_number': program_number,
                    'best_dec_now': None, 'best_book_now': None,
                    'median_dec_now': None, 'n_books': 0,
                })
                continue
            best_idx = grp['decimal_odds'].idxmax()
            rows_agg.append({
                'race_id': race_id, 'program_number': program_number,
                'best_dec_now': float(max(decs)),
                'best_book_now': grp.at[best_idx, 'book_id'],
                'median_dec_now': float(median(decs)),
                'n_books': len(decs),
            })
    agg = pd.DataFrame(rows_agg) if rows_agg else pd.DataFrame(columns=[
        'race_id', 'program_number', 'best_dec_now', 'best_book_now',
        'median_dec_now', 'n_books',
    ])

    # ---- Avg devigged prob per entry across books
    if not devig_df.empty:
        market_prob = devig_df.groupby(
            ['race_id', 'program_number'], as_index=False
        )['devig_prob'].mean().rename(columns={'devig_prob': 'market_prob_devig'})
    else:
        market_prob = pd.DataFrame(columns=['race_id', 'program_number', 'market_prob_devig'])

    # ---- Drift: best_dec earliest vs best_dec latest, per entry
    earliest_non_ml = earliest[earliest['is_morning_line'] == 0]
    if not earliest_non_ml.empty:
        e_best = earliest_non_ml.groupby(['race_id', 'program_number'], as_index=False)[
            'decimal_odds'
        ].max().rename(columns={'decimal_odds': 'best_dec_open'})
    else:
        e_best = pd.DataFrame(columns=['race_id', 'program_number', 'best_dec_open'])

    # ---- Last update timestamp per entry
    last_ts = latest.groupby(['race_id', 'program_number'], as_index=False)[
        'captured_at'
    ].max().rename(columns={'captured_at': 'last_updated_at'})

    # ---- Merge everything onto the entries spine
    df = entries.merge(ml, on=['race_id', 'program_number'], how='left')
    df = df.merge(agg, on=['race_id', 'program_number'], how='left')
    df = df.merge(market_prob, on=['race_id', 'program_number'], how='left')
    df = df.merge(e_best, on=['race_id', 'program_number'], how='left')
    df = df.merge(last_ts, on=['race_id', 'program_number'], how='left')

    df['odds_drift_pct'] = df.apply(
        lambda r: drift_pct(r.get('best_dec_open'), r.get('best_dec_now')), axis=1
    )
    df['drift_direction'] = df.apply(
        lambda r: drift_direction(r.get('best_dec_open'), r.get('best_dec_now')), axis=1
    )

    # ---- Publicness score: 0-10 vs equal-mass baseline
    # If field has N runners, equal mass = 1/N. Publicness = how heavily
    # this entry is backed RELATIVE to the field. Score 5 = equal mass.
    import math
    pub_scores = []
    race_sizes = df.groupby('race_id')['entry_id'].transform('count')
    for i, r in df.iterrows():
        n = race_sizes.iloc[i]
        mp = r.get('market_prob_devig')
        if not n or n <= 0 or mp is None or pd.isna(mp) or mp <= 0:
            pub_scores.append(5.0)
            continue
        eq = 1.0 / n
        ratio = mp / eq
        score = 5.0 + 2.5 * math.log2(ratio)
        pub_scores.append(round(max(0.0, min(10.0, score)), 2))
    df['publicness_score'] = pub_scores

    keep = [
        'entry_id', 'race_id', 'morning_line_dec', 'morning_line_prob',
        'best_dec_now', 'best_book_now', 'median_dec_now',
        'market_prob_devig', 'publicness_score',
        'odds_drift_pct', 'drift_direction', 'n_books', 'last_updated_at',
    ]
    for c in keep:
        if c not in df.columns:
            df[c] = None
    return df[keep]


def write_odds_features(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    cur = conn.cursor()
    cur.execute("DELETE FROM odds_features")
    for _, r in df.iterrows():
        cur.execute(
            """INSERT INTO odds_features
               (entry_id, race_id, morning_line_dec, morning_line_prob,
                best_dec_now, best_book_now, median_dec_now, market_prob_devig,
                publicness_score, odds_drift_pct, drift_direction, n_books,
                last_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r['entry_id'], r['race_id'],
             r['morning_line_dec'], r['morning_line_prob'],
             r['best_dec_now'], r['best_book_now'], r['median_dec_now'],
             r['market_prob_devig'], r['publicness_score'],
             r['odds_drift_pct'], r['drift_direction'], r['n_books'],
             r['last_updated_at']),
        )
    conn.commit()
    return len(df)
