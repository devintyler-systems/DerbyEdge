"""Tests for src/analysis/calibration_snapshot.py"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from tests.conftest import insert_minimal_race


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_race_results(conn: sqlite3.Connection, seed: dict) -> None:
    """Insert race_results: Alpha (rank 1, post 1) wins; official odds ladder."""
    card_id   = seed["card_id"]
    entry_ids = seed["entry_ids"]
    horse_ids = seed["horse_ids"]
    for i, (eid, hid) in enumerate(zip(entry_ids, horse_ids), start=1):
        conn.execute(
            """INSERT INTO race_results
                   (card_id, entry_id, horse_id, post_position,
                    finish_position, official_finish,
                    is_scratched, is_disqualified,
                    official_odds_decimal, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, datetime('now'))""",
            (card_id, eid, hid, i, i, i, float(i * 2 - 1)),
        )
    conn.commit()


def _seed_confidence(conn: sqlite3.Connection, run_id: str) -> None:
    """Populate confidence columns on entry_scores for run_id."""
    from src.utils.db import ensure_entry_scores_columns
    ensure_entry_scores_columns(conn)
    conn.execute(
        """UPDATE entry_scores
           SET confidence_score   = 0.72,
               confidence_bucket  = 'HIGH',
               confidence_reasons = 'clear top-pick separation;veteran (8 starts)',
               confidence_flag    = 1,
               missing_data_flag  = 0
           WHERE run_id = ? AND rank = 1""",
        (run_id,),
    )
    conn.execute(
        """UPDATE entry_scores
           SET confidence_score  = 0.55,
               confidence_bucket = 'MEDIUM',
               confidence_flag   = 1,
               missing_data_flag = 0
           WHERE run_id = ? AND rank > 1""",
        (run_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_snapshot_runs_on_mem_conn(mem_conn):
    """build_snapshot returns rows and includes all required output columns."""
    seed = insert_minimal_race(mem_conn)
    _seed_race_results(mem_conn, seed)
    _seed_confidence(mem_conn, seed["run_id"])

    from src.analysis.calibration_snapshot import build_snapshot, SNAPSHOT_COLS
    rows = build_snapshot(mem_conn)

    assert len(rows) >= 1
    first = rows[0]
    missing = [c for c in SNAPSHOT_COLS if c not in first]
    assert not missing, f"Columns missing from snapshot output: {missing}"


def test_confidence_bucket_and_ptf_fields_present(mem_conn):
    """Key outcome/confidence columns are present and satisfy basic invariants."""
    seed = insert_minimal_race(mem_conn)
    _seed_race_results(mem_conn, seed)
    _seed_confidence(mem_conn, seed["run_id"])

    from src.analysis.calibration_snapshot import build_snapshot
    rows = build_snapshot(mem_conn)
    assert rows
    row = rows[0]

    required = [
        "confidence_bucket", "confidence_score",
        "ptf_horse_name", "ptf_odds", "ptf_won",
        "winner_official_odds", "ptf_aligned", "value_gap_top_vs_ptf",
    ]
    for col in required:
        assert col in row, f"Missing column: {col}"

    # ptf_won is 0 or 1
    assert row["ptf_won"] in (0, 1, None)

    # confidence_bucket is a known value when present
    if row["confidence_bucket"] is not None:
        assert row["confidence_bucket"] in {"LOW", "MEDIUM", "HIGH"}

    # implied_ptf_prob is derived from ptf_odds
    if row["ptf_odds"] is not None:
        expected = round(1.0 / (row["ptf_odds"] + 1.0), 6)
        assert abs(row["implied_ptf_prob"] - expected) < 1e-5

    # Alpha wins and is the lowest-odds runner (odds=1.0 → PTF), so ptf_aligned=1
    assert row["ptf_aligned"] == 1
    assert row["ptf_won"] == 1

    # Confidence fields match what we seeded for rank=1
    assert row["confidence_bucket"] == "HIGH"
    assert abs(row["confidence_score"] - 0.72) < 1e-6
    assert row["confidence_score_bucket"] == "0.6-0.8"


def test_scratched_handling_consistent_with_race_review(mem_conn):
    """When the original top pick is scratched, snapshot metrics are still well-defined."""
    seed = insert_minimal_race(mem_conn, run_id="run-scratch")
    card_id   = seed["card_id"]
    entry_ids = seed["entry_ids"]
    horse_ids = seed["horse_ids"]

    # Alpha (rank 1, post 1) is scratched; Bravo (rank 2) wins.
    for i, (eid, hid) in enumerate(zip(entry_ids, horse_ids), start=1):
        is_scr = 1 if i == 1 else 0
        finish = None if i == 1 else (i - 1)
        off    = None if i == 1 else (i - 1)
        mem_conn.execute(
            """INSERT INTO race_results
                   (card_id, entry_id, horse_id, post_position,
                    finish_position, official_finish,
                    is_scratched, is_disqualified,
                    official_odds_decimal, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, datetime('now'))""",
            (card_id, eid, hid, i, finish, off, is_scr, float(i * 2)),
        )
    mem_conn.commit()

    _seed_confidence(mem_conn, "run-scratch")

    from src.analysis.calibration_snapshot import build_snapshot
    rows = build_snapshot(mem_conn)
    assert rows
    row = rows[0]

    # Original top pick was scratched
    assert row["original_tp_scratched"] == 1

    # top_pick_hit should be 0 (scratched horse did not win)
    assert row["top_pick_hit"] == 0

    # Winner is defined (Bravo)
    assert row["winner_name"] is not None
    assert row["winner_rank"] is not None

    # Effective top pick shifts to Bravo (first non-scratched by model rank)
    assert row["effective_tp_name"] is not None
    assert row["effective_tp_won"] is not None


def test_idempotent_run(mem_conn, tmp_path, monkeypatch):
    """Running build_snapshot twice produces identical CSV output."""
    seed = insert_minimal_race(mem_conn)
    _seed_race_results(mem_conn, seed)
    _seed_confidence(mem_conn, seed["run_id"])

    import src.analysis.calibration_snapshot as snap_mod
    monkeypatch.setattr(snap_mod, "OUTPUT_DIR", tmp_path)

    fixed_date = date(2026, 5, 9)

    rows1 = snap_mod.build_snapshot(mem_conn)
    path1 = snap_mod.write_snapshot(rows1, today=fixed_date)
    assert path1.exists()
    text1 = path1.read_text(encoding="utf-8")

    rows2 = snap_mod.build_snapshot(mem_conn)
    path2 = snap_mod.write_snapshot(rows2, today=fixed_date)
    text2 = path2.read_text(encoding="utf-8")

    assert len(rows1) == len(rows2)
    assert text1 == text2
