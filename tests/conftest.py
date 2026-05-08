"""Shared pytest fixtures for the DerbyEdge regression suite."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mem_conn():
    """In-memory SQLite with full DerbyEdge schema + race_results + race_review view."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")

    schema_text = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    # WAL mode is not supported for in-memory connections
    schema_text = "\n".join(
        ln for ln in schema_text.splitlines() if "journal_mode" not in ln
    )
    conn.executescript(schema_text)

    from src.services.results_intake import _ensure_table, ensure_race_review_view
    _ensure_table(conn)
    ensure_race_review_view(conn)
    yield conn
    conn.close()


def insert_minimal_race(
    conn: sqlite3.Connection,
    *,
    run_id: str = "run-001",
    derby_override_active: int = 1,
    chaos_active: int = 1,
    chaos_intensity: float | None = 0.08,
) -> dict:
    """Seed one track, race card, 5 horses/entries, model, score run, and entry scores.

    Returns {track_id, card_id, run_id, entry_ids, horse_ids}.
    entry_ids / horse_ids are lists of 5, indexed in model rank order (index 0 = rank 1).
    """
    cur = conn.execute(
        "INSERT INTO tracks (name, abbrev) VALUES ('Churchill Downs', 'CD')"
    )
    track_id = cur.lastrowid

    # 2200 yards = 10.00 furlongs (GENERATED column)
    cur = conn.execute(
        """INSERT INTO race_cards
               (track_id, card_date, race_number, stakes_name,
                distance_yards, surface, field_size)
           VALUES (?, '2026-05-02', 1, 'Kentucky Derby', 2200, 'dirt', 20)""",
        (track_id,),
    )
    card_id = cur.lastrowid

    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    entry_ids: list[int] = []
    horse_ids: list[int] = []
    for i, name in enumerate(names, start=1):
        cur = conn.execute("INSERT INTO horses (name) VALUES (?)", (name,))
        hid = cur.lastrowid
        horse_ids.append(hid)
        cur = conn.execute(
            """INSERT INTO entries
                   (card_id, horse_id, post_position, morning_line_odds)
               VALUES (?, ?, ?, ?)""",
            (card_id, hid, i, float(i * 2)),
        )
        entry_ids.append(cur.lastrowid)

    cur = conn.execute(
        "INSERT INTO model_registry (model_name, model_family) VALUES ('test_model', 'fallback')"
    )
    model_id = cur.lastrowid

    conn.execute(
        """INSERT INTO score_runs
               (run_id, card_id, model_id, model_type, derby_override_active,
                chaos_active, chaos_intensity, quality_tier)
           VALUES (?, ?, ?, 'fallback', ?, ?, ?, 'seed_only')""",
        (run_id, card_id, model_id, derby_override_active, chaos_active, chaos_intensity),
    )

    for rank, (eid, name) in enumerate(zip(entry_ids, names), start=1):
        prob = round(0.30 - (rank - 1) * 0.05, 2)  # 0.30, 0.25, 0.20, 0.15, 0.10
        conn.execute(
            """INSERT INTO entry_scores
                   (run_id, entry_id, horse_name, post_position, morning_line_odds,
                    win_probability, rank, bet_tag, market_implied_prob,
                    chaos_score, chaos_boost, chaos_tier, chaos_eligible)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'neutral', ?, ?, ?, ?, 0)""",
            (
                run_id, eid, name, rank, float(rank * 2),
                prob, rank,
                round(1.0 / (rank * 2 + 1), 4),
                round(prob + 0.02, 4),
                0.02,
                "light" if rank <= 2 else "none",
            ),
        )

    conn.commit()
    return {
        "track_id":  track_id,
        "card_id":   card_id,
        "run_id":    run_id,
        "entry_ids": entry_ids,
        "horse_ids": horse_ids,
    }
