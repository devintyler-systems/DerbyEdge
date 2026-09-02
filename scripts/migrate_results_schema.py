"""Add race_results table for post-race outcome storage.

Run once:  python scripts/migrate_results_schema.py
Safe to re-run — all statements use CREATE TABLE/INDEX IF NOT EXISTS.
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.db import get_connection

DDL = """
CREATE TABLE IF NOT EXISTS race_results (
    result_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id                 INTEGER NOT NULL REFERENCES race_cards(card_id),
    entry_id                INTEGER          REFERENCES entries(entry_id),
    horse_id                INTEGER NOT NULL REFERENCES horses(horse_id),
    post_position           INTEGER,
    finish_position         INTEGER,         -- NULL if scratched
    official_finish         INTEGER,         -- after DQ adjustments
    is_scratched            INTEGER NOT NULL DEFAULT 0 CHECK(is_scratched IN (0,1)),
    is_disqualified         INTEGER NOT NULL DEFAULT 0 CHECK(is_disqualified IN (0,1)),
    official_odds_decimal   REAL,
    official_odds_american  INTEGER,
    beaten_lengths          REAL,
    speed_figure            INTEGER,
    beyer_figure            INTEGER,
    final_time              TEXT,
    earned_purse            INTEGER,
    comment                 TEXT,
    ingested_at             TEXT NOT NULL,
    UNIQUE(card_id, entry_id)
);

CREATE INDEX IF NOT EXISTS idx_rr_card  ON race_results(card_id);
CREATE INDEX IF NOT EXISTS idx_rr_horse ON race_results(horse_id);
CREATE INDEX IF NOT EXISTS idx_rr_entry ON race_results(entry_id);
"""


def main() -> None:
    conn = get_connection()
    conn.executescript(DDL)
    conn.commit()
    # Verify
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "race_results" in tables:
        n = conn.execute("SELECT COUNT(*) FROM race_results").fetchone()[0]
        print(f"race_results table ready ({n} existing rows).")
    else:
        print("ERROR: race_results table was not created.")
    conn.close()


if __name__ == "__main__":
    main()
