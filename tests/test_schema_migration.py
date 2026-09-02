"""
Tests for schema migration helpers in src/utils/db.py.

All tests use in-memory SQLite so they are fast, isolated, and leave no
files on disk.  They simulate the "old DB without new columns" scenario
by creating a minimal table then calling the ensure_* functions.
"""
import sqlite3

import pandas as pd
import pytest

from src.utils.db import (
    ensure_entry_scores_columns,
    ensure_score_runs_columns,
    entry_scores_cols,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENTRY_SCORES_BASE = """
CREATE TABLE IF NOT EXISTS entry_scores (
    entry_id      INTEGER PRIMARY KEY,
    run_id        TEXT,
    rank          INTEGER,
    horse_name    TEXT,
    confidence_flag     INTEGER NOT NULL DEFAULT 0,
    missing_data_flag   INTEGER NOT NULL DEFAULT 0
);
"""

_SCORE_RUNS_BASE = """
CREATE TABLE IF NOT EXISTS score_runs (
    run_id         TEXT PRIMARY KEY,
    run_timestamp  TEXT
);
"""


def _old_entry_scores_conn() -> sqlite3.Connection:
    """In-memory DB with the V1 entry_scores schema (no confidence_* columns)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_ENTRY_SCORES_BASE)
    return conn


def _old_score_runs_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCORE_RUNS_BASE)
    return conn


def _col_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------------------
# ensure_entry_scores_columns
# ---------------------------------------------------------------------------

class TestEnsureEntryScoresColumns:

    def test_adds_confidence_score(self):
        conn = _old_entry_scores_conn()
        assert "confidence_score" not in _col_names(conn, "entry_scores")
        ensure_entry_scores_columns(conn)
        assert "confidence_score" in _col_names(conn, "entry_scores")

    def test_adds_confidence_bucket(self):
        conn = _old_entry_scores_conn()
        ensure_entry_scores_columns(conn)
        assert "confidence_bucket" in _col_names(conn, "entry_scores")

    def test_adds_confidence_reasons(self):
        conn = _old_entry_scores_conn()
        ensure_entry_scores_columns(conn)
        assert "confidence_reasons" in _col_names(conn, "entry_scores")

    def test_adds_chaos_columns(self):
        conn = _old_entry_scores_conn()
        ensure_entry_scores_columns(conn)
        cols = _col_names(conn, "entry_scores")
        assert "chaos_score" in cols
        assert "chaos_boost" in cols
        assert "chaos_tier" in cols
        assert "chaos_eligible" in cols

    def test_adds_low_conf_bet_block(self):
        conn = _old_entry_scores_conn()
        ensure_entry_scores_columns(conn)
        assert "low_conf_bet_block" in _col_names(conn, "entry_scores")

    def test_idempotent_no_error_on_second_call(self):
        conn = _old_entry_scores_conn()
        ensure_entry_scores_columns(conn)
        # Second call must not raise
        ensure_entry_scores_columns(conn)
        assert "confidence_score" in _col_names(conn, "entry_scores")

    def test_idempotent_column_count_stable(self):
        conn = _old_entry_scores_conn()
        ensure_entry_scores_columns(conn)
        count_after_first = len(_col_names(conn, "entry_scores"))
        ensure_entry_scores_columns(conn)
        assert len(_col_names(conn, "entry_scores")) == count_after_first

    def test_backfill_confidence_bucket_from_flag_0(self):
        conn = _old_entry_scores_conn()
        conn.execute(
            "INSERT INTO entry_scores (entry_id, run_id, confidence_flag) VALUES (1, 'r1', 0)"
        )
        conn.commit()
        ensure_entry_scores_columns(conn)
        row = conn.execute(
            "SELECT confidence_bucket FROM entry_scores WHERE entry_id=1"
        ).fetchone()
        assert row["confidence_bucket"] == "LOW"

    def test_backfill_confidence_bucket_from_flag_1(self):
        conn = _old_entry_scores_conn()
        conn.execute(
            "INSERT INTO entry_scores (entry_id, run_id, confidence_flag) VALUES (2, 'r1', 1)"
        )
        conn.commit()
        ensure_entry_scores_columns(conn)
        row = conn.execute(
            "SELECT confidence_bucket FROM entry_scores WHERE entry_id=2"
        ).fetchone()
        assert row["confidence_bucket"] == "MEDIUM"

    def test_backfill_does_not_overwrite_existing_bucket(self):
        """Rows that already have confidence_bucket must not be touched."""
        conn = _old_entry_scores_conn()
        # Simulate a row that already has the new columns (fully migrated DB)
        conn.execute("ALTER TABLE entry_scores ADD COLUMN confidence_bucket TEXT")
        conn.execute(
            "INSERT INTO entry_scores (entry_id, run_id, confidence_flag, confidence_bucket)"
            " VALUES (3, 'r1', 0, 'HIGH')"
        )
        conn.commit()
        ensure_entry_scores_columns(conn)
        row = conn.execute(
            "SELECT confidence_bucket FROM entry_scores WHERE entry_id=3"
        ).fetchone()
        assert row["confidence_bucket"] == "HIGH"

    def test_empty_table_migrates_without_error(self):
        conn = _old_entry_scores_conn()
        ensure_entry_scores_columns(conn)  # no rows — must not raise
        assert "confidence_bucket" in _col_names(conn, "entry_scores")


# ---------------------------------------------------------------------------
# ensure_score_runs_columns
# ---------------------------------------------------------------------------

class TestEnsureScoreRunsColumns:

    def test_adds_derby_override_active(self):
        conn = _old_score_runs_conn()
        ensure_score_runs_columns(conn)
        assert "derby_override_active" in _col_names(conn, "score_runs")

    def test_adds_chaos_active(self):
        conn = _old_score_runs_conn()
        ensure_score_runs_columns(conn)
        assert "chaos_active" in _col_names(conn, "score_runs")

    def test_adds_chaos_intensity(self):
        conn = _old_score_runs_conn()
        ensure_score_runs_columns(conn)
        assert "chaos_intensity" in _col_names(conn, "score_runs")

    def test_adds_field_entropy_score(self):
        conn = _old_score_runs_conn()
        ensure_score_runs_columns(conn)
        assert "field_entropy_score" in _col_names(conn, "score_runs")

    def test_adds_quality_tier(self):
        conn = _old_score_runs_conn()
        ensure_score_runs_columns(conn)
        assert "quality_tier" in _col_names(conn, "score_runs")

    def test_idempotent(self):
        conn = _old_score_runs_conn()
        ensure_score_runs_columns(conn)
        ensure_score_runs_columns(conn)  # must not raise
        assert "chaos_active" in _col_names(conn, "score_runs")


# ---------------------------------------------------------------------------
# entry_scores_cols helper
# ---------------------------------------------------------------------------

class TestEntryScoresCols:

    def test_returns_set_of_strings(self):
        conn = _old_entry_scores_conn()
        cols = entry_scores_cols(conn)
        assert isinstance(cols, set)
        assert "entry_id" in cols

    def test_reflects_migration(self):
        conn = _old_entry_scores_conn()
        assert "confidence_score" not in entry_scores_cols(conn)
        ensure_entry_scores_columns(conn)
        assert "confidence_score" in entry_scores_cols(conn)


# ---------------------------------------------------------------------------
# Integration: load_board-style dynamic SELECT works against old schema
# ---------------------------------------------------------------------------

class TestDynamicSelectFallback:
    """Simulate the PRAGMA-driven SELECT fragment that load_board builds."""

    def test_old_schema_select_uses_fallback_expressions(self):
        conn = _old_entry_scores_conn()
        # Do NOT migrate — old schema
        cols = entry_scores_cols(conn)
        assert "confidence_score" not in cols

        # Build the same fragment load_board builds
        if "confidence_score" in cols:
            frag = "es.confidence_score, es.confidence_bucket, es.confidence_reasons"
        else:
            frag = (
                "NULL AS confidence_score, "
                "CASE WHEN es.confidence_flag = 0 THEN 'LOW' ELSE 'MEDIUM' END AS confidence_bucket, "
                "NULL AS confidence_reasons"
            )

        conn.execute(
            "INSERT INTO entry_scores (entry_id, run_id, rank, horse_name, confidence_flag)"
            " VALUES (10, 'r1', 1, 'TestHorse', 1)"
        )
        conn.commit()

        row = conn.execute(
            f"SELECT entry_id, {frag} FROM entry_scores AS es WHERE entry_id=10"
        ).fetchone()

        assert row["confidence_bucket"] == "MEDIUM"
        assert row["confidence_score"] is None
        assert row["confidence_reasons"] is None

    def test_migrated_schema_select_uses_real_columns(self):
        conn = _old_entry_scores_conn()
        ensure_entry_scores_columns(conn)

        conn.execute(
            "INSERT INTO entry_scores (entry_id, run_id, rank, horse_name,"
            " confidence_flag, confidence_score, confidence_bucket, confidence_reasons)"
            " VALUES (11, 'r1', 1, 'TestHorse', 1, 0.78, 'HIGH', 'veteran (20 starts)')"
        )
        conn.commit()

        cols = entry_scores_cols(conn)
        assert "confidence_score" in cols

        if "confidence_score" in cols:
            frag = "es.confidence_score, es.confidence_bucket, es.confidence_reasons"
        else:
            frag = (
                "NULL AS confidence_score, "
                "CASE WHEN es.confidence_flag = 0 THEN 'LOW' ELSE 'MEDIUM' END AS confidence_bucket, "
                "NULL AS confidence_reasons"
            )

        row = conn.execute(
            f"SELECT entry_id, {frag} FROM entry_scores es WHERE entry_id=11"
        ).fetchone()

        assert row["confidence_bucket"] == "HIGH"
        assert abs(row["confidence_score"] - 0.78) < 0.001
        assert row["confidence_reasons"] == "veteran (20 starts)"

    def test_repeated_startup_ddl_does_not_corrupt_data(self):
        """Simulates app restart calling ensure_* multiple times."""
        conn = _old_entry_scores_conn()
        conn2 = _old_score_runs_conn()
        # Simulate full bootstrap
        for _ in range(3):
            ensure_entry_scores_columns(conn)
            ensure_score_runs_columns(conn2)

        # Data integrity check after repeated migrations
        conn.execute(
            "INSERT INTO entry_scores (entry_id, run_id, confidence_flag)"
            " VALUES (20, 'r2', 0)"
        )
        conn.commit()
        # Migrate again after insert
        ensure_entry_scores_columns(conn)

        row = conn.execute(
            "SELECT confidence_bucket FROM entry_scores WHERE entry_id=20"
        ).fetchone()
        assert row["confidence_bucket"] == "LOW"
