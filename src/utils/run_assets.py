"""Card-scoped filesystem locations for generated race artifacts."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "output" / "runs"


def card_run_key(track_abbrev: str, card_date: str, race_number: int | str) -> str:
    """Return the stable artifact key ``{track}_{date}_r{race_number}``."""
    track = re.sub(r"[^a-z0-9]+", "", str(track_abbrev).strip().lower())
    date = str(card_date).strip()
    if not track or not date or race_number in (None, ""):
        raise ValueError("track abbreviation, card date, and race number are required")
    return f"{track}_{date}_r{int(race_number)}"


def run_dir_for_card(
    card_id: int,
    conn: Optional[sqlite3.Connection] = None,
) -> Path:
    """Return the card-specific artifact directory, creating no files."""
    own_conn = conn is None
    if own_conn:
        from src.utils.db import get_connection
        conn = get_connection()

    try:
        row = conn.execute(
            """
            SELECT t.abbrev AS track_abbrev, rc.card_date, rc.race_number
            FROM race_cards rc
            JOIN tracks t ON t.track_id = rc.track_id
            WHERE rc.card_id = ?
            """,
            (card_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown card_id={card_id}")
        return RUNS_DIR / card_run_key(
            row["track_abbrev"], row["card_date"], row["race_number"]
        )
    finally:
        if own_conn:
            conn.close()
