"""Add is_hidden column to race_cards for soft-delete support.

Safe to re-run — checks for existing column before altering.
Run: python scripts/migrate_add_is_hidden.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.db import get_connection


def main() -> None:
    conn = get_connection()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(race_cards)").fetchall()}
    if "is_hidden" in cols:
        print("[migrate_add_is_hidden] is_hidden column already present — skipping.")
        conn.close()
        return

    conn.execute(
        "ALTER TABLE race_cards ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0"
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM race_cards WHERE is_hidden = 0").fetchone()[0]
    print(f"[migrate_add_is_hidden] Added is_hidden column. {n} existing races set to visible.")
    conn.close()


if __name__ == "__main__":
    main()
