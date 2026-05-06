"""Load parsed records into SQLite."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .schema import init_db
from .parser import parse_directory, parse_file


_TABLE_PK = {
    'tracks': 'track_id',
    'people': 'external_party_id',
    'horses': 'registration_number',
    'races': 'race_id',
    'entries': 'entry_id',
    'horse_starts': 'start_id',
    'fractions': None,        # composite
    'point_of_call': None,
    'company_line': None,
    'workouts': 'workout_id',
}


def _insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ','.join(['?'] * len(cols))
    col_list = ','.join(cols)
    sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
    cur = conn.cursor()
    cur.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    conn.commit()
    return cur.rowcount


def load_directory(xml_dir: str | Path, db_path: str | Path) -> dict[str, int]:
    """Parse all SIMD XML files under xml_dir and load into SQLite at db_path."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)

    merged = parse_directory(xml_dir)
    counts = {}
    # Reference tables first (dicts)
    for t in ('tracks', 'people', 'horses'):
        rows = list(merged[t].values())
        counts[t] = _insert_rows(conn, t, rows)
    # Then dependents
    for t in ('races', 'entries', 'horse_starts', 'fractions',
              'point_of_call', 'company_line', 'workouts'):
        counts[t] = _insert_rows(conn, t, merged[t])

    conn.close()
    return counts


def load_file(xml_path: str | Path, db_path: str | Path) -> dict[str, int]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    parsed = parse_file(xml_path)
    counts = {}
    for t in ('tracks', 'people', 'horses'):
        rows = list(parsed[t].values())
        counts[t] = _insert_rows(conn, t, rows)
    for t in ('races', 'entries', 'horse_starts', 'fractions',
              'point_of_call', 'company_line', 'workouts'):
        counts[t] = _insert_rows(conn, t, parsed[t])
    conn.close()
    return counts
