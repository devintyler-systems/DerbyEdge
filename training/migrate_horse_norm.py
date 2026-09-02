"""
training/migrate_horse_norm.py

Idempotent migration: add horse_norm column to starter_observations and
backfill it from the existing horse column.

Usage
-----
    python -m training.migrate_horse_norm
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.db import get_connection
from src.utils.horse_norm import normalize_horse_name


def run() -> None:
    conn = get_connection()

    # Add column (no-op if already present)
    try:
        conn.execute("ALTER TABLE starter_observations ADD COLUMN horse_norm TEXT")
        conn.commit()
        print("Column horse_norm added to starter_observations.")
    except Exception:
        print("Column horse_norm already exists — skipping ALTER.")

    # Backfill rows where horse_norm is NULL
    rows = conn.execute(
        "SELECT obs_id, horse FROM starter_observations WHERE horse_norm IS NULL"
    ).fetchall()

    if not rows:
        print("All rows already have horse_norm — nothing to backfill.")
        conn.close()
        return

    updated = 0
    for obs_id, horse in rows:
        norm = normalize_horse_name(horse or "")
        conn.execute(
            "UPDATE starter_observations SET horse_norm = ? WHERE obs_id = ?",
            (norm, obs_id),
        )
        updated += 1

    conn.commit()
    conn.close()
    print(f"Backfilled horse_norm for {updated} row(s).")


if __name__ == "__main__":
    run()
