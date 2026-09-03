"""Add live_odds table to the Operator Console DB.

Safe to re-run — uses CREATE TABLE IF NOT EXISTS.
Run: python scripts/migrate_odds_schema.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.db import get_connection

_DDL = """
CREATE TABLE IF NOT EXISTS live_odds (
    lo_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at      TEXT    NOT NULL,
    book_id          TEXT    NOT NULL DEFAULT 'manual',
    card_id          INTEGER NOT NULL,
    entry_id         INTEGER,
    post_position    INTEGER,
    decimal_odds     REAL,
    american_odds    INTEGER,
    is_scratched     INTEGER NOT NULL DEFAULT 0 CHECK(is_scratched IN (0,1)),
    is_morning_line  INTEGER NOT NULL DEFAULT 0 CHECK(is_morning_line IN (0,1)),
    odds_type        TEXT    NOT NULL DEFAULT 'live_tote'
                     CHECK(odds_type IN ('morning_line','live_tote','off_odds','unknown')),
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_lo_card  ON live_odds(card_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_lo_entry ON live_odds(entry_id);
"""


def main() -> None:
    conn = get_connection()
    conn.executescript(_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(live_odds)")}
    if "odds_type" not in columns:
        conn.execute(
            "ALTER TABLE live_odds ADD COLUMN odds_type TEXT NOT NULL DEFAULT 'live_tote'"
        )
    conn.commit()
    conn.close()
    print("[migrate_odds_schema] live_odds table ready.")


if __name__ == "__main__":
    main()
