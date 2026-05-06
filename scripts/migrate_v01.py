"""Apply v0.1 migrations to an existing v0 SQLite DB.

Adds: markets, odds_snapshots, odds_features tables.
Idempotent.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'src'))

from derbyedge.odds_schema import init_odds_schema


def main():
    db_path = ROOT / 'data' / 'processed' / 'derbyedge.sqlite'
    print(f"Migrating {db_path}")
    conn = sqlite3.connect(db_path)
    init_odds_schema(conn)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    print("Tables:", sorted(tables))
    conn.close()
    print("Done.")


if __name__ == '__main__':
    main()
