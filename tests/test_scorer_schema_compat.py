"""
tests/test_scorer_schema_compat.py

Regression tests for scorer.py schema compatibility.

Covers _get_table_columns and _fetch_race_meta against two representative
race_cards schemas:

  "new" schema  — has race_date, race_no, distance_furlongs, surface
  "old" schema  — has card_date, race_number, distance_yards, surface
                  (mirrors the live DB that triggered the rc2.race_date crash)

All tests use in-memory SQLite; no files are written to disk.
"""
import sqlite3

import pytest

from src.models.scorer import _fetch_race_meta, _get_table_columns

# ---------------------------------------------------------------------------
# In-memory DB fixtures
# ---------------------------------------------------------------------------

def _conn_new_schema() -> sqlite3.Connection:
    """race_cards with race_date + race_no + distance_furlongs (newer schema)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tracks (
            track_id INTEGER PRIMARY KEY,
            abbrev   TEXT NOT NULL
        );
        CREATE TABLE race_cards (
            card_id           INTEGER PRIMARY KEY,
            track_id          INTEGER REFERENCES tracks(track_id),
            race_date         TEXT,
            race_no           INTEGER,
            distance_furlongs REAL,
            surface           TEXT
        );
        INSERT INTO tracks    VALUES (1, 'CD');
        INSERT INTO race_cards VALUES (42, 1, '2026-05-03', 7, 10.0, 'dirt');
    """)
    return conn


def _conn_old_schema() -> sqlite3.Connection:
    """race_cards with card_date + race_number + distance_yards (older schema,
    matches the live DB that triggered the no-such-column crash)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tracks (
            track_id INTEGER PRIMARY KEY,
            abbrev   TEXT NOT NULL
        );
        CREATE TABLE race_cards (
            card_id        INTEGER PRIMARY KEY,
            track_id       INTEGER REFERENCES tracks(track_id),
            card_date      TEXT,
            race_number    INTEGER,
            distance_yards INTEGER,
            surface        TEXT
        );
        INSERT INTO tracks    VALUES (1, 'CD');
        INSERT INTO race_cards VALUES (42, 1, '2026-05-03', 7, 2200, 'dirt');
    """)
    return conn


def _conn_no_optional_cols() -> sqlite3.Connection:
    """Minimal race_cards with only card_id, track_id — no date/no/dist/surface."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tracks (
            track_id INTEGER PRIMARY KEY,
            abbrev   TEXT NOT NULL
        );
        CREATE TABLE race_cards (
            card_id  INTEGER PRIMARY KEY,
            track_id INTEGER REFERENCES tracks(track_id)
        );
        INSERT INTO tracks    VALUES (1, 'AQU');
        INSERT INTO race_cards VALUES (99, 1);
    """)
    return conn


# ---------------------------------------------------------------------------
# _get_table_columns
# ---------------------------------------------------------------------------

class TestGetTableColumns:

    def test_returns_expected_columns(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT, c REAL)")
        assert _get_table_columns(conn, "t") == {"a", "b", "c"}

    def test_nonexistent_table_returns_empty_set(self):
        conn = sqlite3.connect(":memory:")
        assert _get_table_columns(conn, "nonexistent") == set()


# ---------------------------------------------------------------------------
# _fetch_race_meta — new schema (race_date / race_no / distance_furlongs)
# ---------------------------------------------------------------------------

class TestFetchRaceMetaNewSchema:

    def test_race_date_from_db(self):
        conn = _conn_new_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="turf", fallback_dist=8.0)
        assert meta["race_date"] == "2026-05-03"

    def test_race_no_from_db(self):
        conn = _conn_new_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="turf", fallback_dist=8.0)
        assert meta["race_no"] == "7"

    def test_track_from_db(self):
        conn = _conn_new_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="turf", fallback_dist=8.0)
        assert meta["track"] == "CD"

    def test_distance_furlongs_from_db(self):
        conn = _conn_new_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="turf", fallback_dist=8.0)
        assert meta["distance_furlongs"] == 10.0

    def test_surface_from_db(self):
        conn = _conn_new_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="turf", fallback_dist=8.0)
        assert meta["surface"] == "dirt"


# ---------------------------------------------------------------------------
# _fetch_race_meta — old schema (card_date / race_number / distance_yards)
# ---------------------------------------------------------------------------

class TestFetchRaceMetaOldSchema:

    def test_does_not_raise(self):
        """The crash that triggered this fix — must not raise OperationalError."""
        conn = _conn_old_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="dirt", fallback_dist=10.0)
        assert isinstance(meta, dict)

    def test_race_date_falls_back_to_card_date(self):
        conn = _conn_old_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="dirt", fallback_dist=10.0)
        assert meta["race_date"] == "2026-05-03"

    def test_race_no_falls_back_to_race_number(self):
        conn = _conn_old_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="dirt", fallback_dist=10.0)
        assert meta["race_no"] == "7"

    def test_track_resolved(self):
        conn = _conn_old_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="dirt", fallback_dist=10.0)
        assert meta["track"] == "CD"

    def test_distance_furlongs_uses_fallback_when_col_absent(self):
        conn = _conn_old_schema()
        meta = _fetch_race_meta(conn, 42, fallback_surface="dirt", fallback_dist=10.0)
        assert meta["distance_furlongs"] == 10.0

    def test_surface_from_db_not_fallback(self):
        conn = _conn_old_schema()
        # Pass a different fallback — DB value should win
        meta = _fetch_race_meta(conn, 42, fallback_surface="turf", fallback_dist=8.0)
        assert meta["surface"] == "dirt"


# ---------------------------------------------------------------------------
# _fetch_race_meta — missing card_id
# ---------------------------------------------------------------------------

class TestFetchRaceMetaMissingCard:

    def test_returns_empty_strings_for_text_fields(self):
        conn = _conn_new_schema()
        meta = _fetch_race_meta(conn, 999, fallback_surface="turf", fallback_dist=9.0)
        assert meta["race_date"] == ""
        assert meta["track"] == ""
        assert meta["race_no"] == ""

    def test_uses_fallback_dist_when_card_missing(self):
        conn = _conn_new_schema()
        meta = _fetch_race_meta(conn, 999, fallback_surface="turf", fallback_dist=9.0)
        assert meta["distance_furlongs"] == 9.0

    def test_uses_fallback_surface_when_card_missing(self):
        conn = _conn_new_schema()
        meta = _fetch_race_meta(conn, 999, fallback_surface="turf", fallback_dist=9.0)
        assert meta["surface"] == "turf"


# ---------------------------------------------------------------------------
# _fetch_race_meta — schema with no optional columns at all
# ---------------------------------------------------------------------------

class TestFetchRaceMetaMinimalSchema:

    def test_does_not_raise_with_no_optional_cols(self):
        conn = _conn_no_optional_cols()
        meta = _fetch_race_meta(conn, 99, fallback_surface="synthetic", fallback_dist=6.0)
        assert isinstance(meta, dict)

    def test_all_text_fields_are_strings(self):
        conn = _conn_no_optional_cols()
        meta = _fetch_race_meta(conn, 99, fallback_surface="synthetic", fallback_dist=6.0)
        assert isinstance(meta["race_date"], str)
        assert isinstance(meta["track"],     str)
        assert isinstance(meta["race_no"],   str)
        assert isinstance(meta["surface"],   str)

    def test_fallbacks_applied(self):
        conn = _conn_no_optional_cols()
        meta = _fetch_race_meta(conn, 99, fallback_surface="synthetic", fallback_dist=6.0)
        assert meta["track"]             == "AQU"
        assert meta["race_date"]         == ""
        assert meta["race_no"]           == ""
        assert meta["distance_furlongs"] == 6.0
        assert meta["surface"]           == "synthetic"
