"""Find and fix morning lines stored with the old decimal-with-stake convention.

Before the fix, ``parse_morning_line`` returned ``num/den + 1`` for fractions (``9/2`` -> 5.5) but
the plain number for whole-number lines (``20`` -> 20.0).  ``entries.morning_line_odds`` is "to-one"
(``9/2`` -> 4.5), so every FRACTIONAL line on a DraftKings-imported card carries a wrong stored value
and a wrong ``morning_line_prob`` (9/2 gave 0.1538, not 0.1818).  The raw DK document is kept in
``source_artifacts``, so the correct value is re-derived from it, not guessed.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.ingest.draftkings_markdown import parse_draftkings_markdown
from src.services.race_card_builder import parse_morning_line


def find_misstored_morning_lines(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_artifacts'").fetchone() is None:
        return []
    artifacts = conn.execute(
        """SELECT card_id, source_filename, raw_bytes FROM source_artifacts
           WHERE source_provider='draftkings_markdown' AND card_id IS NOT NULL
             AND artifact_id IN (SELECT MAX(artifact_id) FROM source_artifacts
                                 WHERE source_provider='draftkings_markdown' GROUP BY card_id)
           ORDER BY card_id"""
    ).fetchall()
    found: list[dict[str, Any]] = []
    for card_id, filename, raw in artifacts:
        card = parse_draftkings_markdown(
            bytes(raw).decode("utf-8"), source_path=filename or "<artifact>",
            as_of=datetime.now(timezone.utc),
        )
        for entry in card.entries:
            expected = parse_morning_line(entry.morning_line)
            row = conn.execute(
                "SELECT entry_id, morning_line_odds FROM entries WHERE card_id=? AND program_number=?",
                (card_id, entry.program_number),
            ).fetchone()
            if expected is None or row is None:
                continue
            if abs(float(row[1]) - expected) > 1e-9:
                found.append({
                    "card_id": card_id, "entry_id": row[0], "program": entry.program_number,
                    "horse": entry.horse_name, "source_ml": entry.morning_line,
                    "stored": float(row[1]), "correct": expected,
                })
    return found


def apply_morning_line_repair(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    for r in rows:
        conn.execute("UPDATE entries SET morning_line_odds=? WHERE entry_id=?", (r["correct"], r["entry_id"]))
    conn.commit()
    return len(rows)
