"""Integration test for the odds pipeline:
CSV -> ingest -> features -> edge calc.

Uses a synthetic 3-runner race in an in-memory SQLite.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'src'))

import pandas as pd
import pytest

from derbyedge.schema import init_db
from derbyedge.odds_schema import init_odds_schema
from derbyedge.odds_ingest import adapter_manual_csv, write_snapshots
from derbyedge.odds_features import build_odds_features, write_odds_features
from derbyedge.edge_calc import build_edge_table


def _seed_race(conn):
    conn.executescript("""
        INSERT INTO tracks (track_id, track_name, country) VALUES ('CD','Churchill','USA');
        INSERT INTO horses (registration_number, horse_name) VALUES
            ('H1','Alpha'),('H2','Bravo'),('H3','Charlie');
        INSERT INTO races (race_id, track_id, race_date, race_number, surface, distance_id, distance_unit, number_of_runners)
            VALUES ('CD|2026-05-02|12','CD','2026-05-02',12,'D',1000,'F',3);
        INSERT INTO entries (entry_id, race_id, program_number, post_position, horse_reg) VALUES
            ('E1','CD|2026-05-02|12','1',1,'H1'),
            ('E2','CD|2026-05-02|12','2',2,'H2'),
            ('E3','CD|2026-05-02|12','3',3,'H3');
    """)
    conn.commit()


def _write_csv(tmp_path: Path) -> Path:
    csv = tmp_path / "odds.csv"
    csv.write_text(
        "book_id,race_id,program_number,decimal_odds,is_morning_line\n"
        "morningline,CD|2026-05-02|12,1,3.0,1\n"
        "morningline,CD|2026-05-02|12,2,5.0,1\n"
        "morningline,CD|2026-05-02|12,3,8.0,1\n"
        "fanduel,CD|2026-05-02|12,1,2.8,0\n"
        "fanduel,CD|2026-05-02|12,2,5.5,0\n"
        "fanduel,CD|2026-05-02|12,3,9.0,0\n"
        "draftkings,CD|2026-05-02|12,1,2.9,0\n"
        "draftkings,CD|2026-05-02|12,2,5.0,0\n"
        "draftkings,CD|2026-05-02|12,3,10.0,0\n"
    )
    return csv


def test_csv_ingest_to_edge_table(tmp_path):
    conn = sqlite3.connect(':memory:')
    init_db(conn)
    init_odds_schema(conn)
    _seed_race(conn)

    csv = _write_csv(tmp_path)
    recs = adapter_manual_csv(csv, conn=conn)
    assert len(recs) == 9

    n = write_snapshots(conn, recs)
    assert n == 9

    # All entry_ids resolved
    assert all(r.entry_id is not None for r in recs)

    feat = build_odds_features(conn)
    assert len(feat) == 3
    write_odds_features(conn, feat)

    # Best decimal across non-ML books
    alpha = feat[feat['entry_id'] == 'E1'].iloc[0]
    assert alpha['best_dec_now'] == 2.9       # DK 2.9 > FD 2.8
    assert alpha['best_book_now'] == 'draftkings'
    assert alpha['n_books'] == 2
    assert alpha['morning_line_dec'] == 3.0

    # Devigged probabilities sum to 1 within race
    s = feat['market_prob_devig'].sum()
    assert abs(s - 1.0) < 1e-3

    # Now run edge calc with a fake model: Bravo undervalued
    model = pd.DataFrame([
        {'entry_id': 'E1', 'model_prob': 0.30},
        {'entry_id': 'E2', 'model_prob': 0.40},
        {'entry_id': 'E3', 'model_prob': 0.30},
    ])
    edges = build_edge_table(conn, model_probs=model)
    assert len(edges) == 3

    bravo = edges[edges['entry_id'] == 'E2'].iloc[0]
    # Bravo: market ~29% devigged, model 40% => moderate edge ~35%
    assert bravo['bet_tag'] in ('STRONG_PLAY', 'VALUE_PLAY', 'WATCH')
    assert bravo['edge'] > 0.20

    # Alpha: market favorite, model says 30% — slightly underbet
    alpha_edge = edges[edges['entry_id'] == 'E1'].iloc[0]
    assert alpha_edge['kelly_frac'] >= 0


def test_drift_detection(tmp_path):
    """Two snapshots over time -> drift_direction populated."""
    from datetime import datetime, timedelta, timezone

    conn = sqlite3.connect(':memory:')
    init_db(conn)
    init_odds_schema(conn)
    _seed_race(conn)

    t0 = '2026-05-02T08:00:00Z'
    t1 = '2026-05-02T18:00:00Z'

    csv0 = tmp_path / "open.csv"
    csv0.write_text(
        "book_id,race_id,program_number,decimal_odds,captured_at\n"
        f"fanduel,CD|2026-05-02|12,1,5.0,{t0}\n"
        f"fanduel,CD|2026-05-02|12,2,4.0,{t0}\n"
        f"fanduel,CD|2026-05-02|12,3,8.0,{t0}\n"
    )
    csv1 = tmp_path / "close.csv"
    csv1.write_text(
        "book_id,race_id,program_number,decimal_odds,captured_at\n"
        f"fanduel,CD|2026-05-02|12,1,3.0,{t1}\n"   # shortened
        f"fanduel,CD|2026-05-02|12,2,4.0,{t1}\n"   # flat
        f"fanduel,CD|2026-05-02|12,3,12.0,{t1}\n"  # drifted
    )

    write_snapshots(conn, adapter_manual_csv(csv0, conn=conn))
    write_snapshots(conn, adapter_manual_csv(csv1, conn=conn))

    feat = build_odds_features(conn)
    a = feat[feat['entry_id'] == 'E1'].iloc[0]
    b = feat[feat['entry_id'] == 'E2'].iloc[0]
    c = feat[feat['entry_id'] == 'E3'].iloc[0]

    assert a['drift_direction'] == 'shortening'
    assert b['drift_direction'] == 'flat'
    assert c['drift_direction'] == 'drifting'
