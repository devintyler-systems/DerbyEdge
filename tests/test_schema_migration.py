"""
Tests for schema migration helpers in src/utils/db.py.

All tests use in-memory SQLite so they are fast, isolated, and leave no
files on disk.  They simulate the "old DB without new columns" scenario
by creating a minimal table then calling the ensure_* functions.
"""
import sqlite3
from pathlib import Path
from typing import ClassVar

from src.utils.db import (
    ensure_entry_scores_columns,
    ensure_feature_store_columns,
    ensure_score_runs_columns,
    entry_scores_cols,
)

ROOT = Path(__file__).resolve().parents[1]

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

_FEATURE_STORE_BASE = """
CREATE TABLE feature_store (feature_id INTEGER PRIMARY KEY, card_id INTEGER);
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


def _old_feature_store_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_FEATURE_STORE_BASE)
    return conn


def _col_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------------------
# ensure_feature_store_columns
# ---------------------------------------------------------------------------

def test_feature_store_migration_adds_pace_observability_columns():
    conn = _old_feature_store_conn()
    ensure_feature_store_columns(conn)
    assert {
        "run_style_evidence_count", "run_style_source", "pace_band",
        "classified_runner_count", "active_runner_count", "pace_state",
    }.issubset(_col_names(conn, "feature_store"))


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

    def test_adds_probability_provenance_columns(self):
        conn = _old_entry_scores_conn()
        ensure_entry_scores_columns(conn)
        assert {
            "p_ml_implied", "p_signal_pre_market", "p_model_pre_market",
            "p_market_live", "p_model_blended", "edge_vs_live_market",
        }.issubset(_col_names(conn, "entry_scores"))

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

    def test_adds_market_prior_collapse_audit_columns(self):
        conn = _old_score_runs_conn()
        ensure_score_runs_columns(conn)
        assert {
            "effective_run_mode", "model_collapse_status",
            "max_abs_model_ml_delta", "mean_abs_model_ml_delta",
            "displayed_model_assigned_from_market",
        }.issubset(_col_names(conn, "score_runs"))

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


# ---------------------------------------------------------------------------
# Integration: build_features auto-migrates feature_store
# ---------------------------------------------------------------------------

class TestFeatureStoreBuilderMigrationIntegration:
    """Integration test verifying build_features self-migrates local SQLite DB.

    Covers the PR #10 pace observability columns:
      - run_style_evidence_count
      - run_style_source
      - pace_band
      - classified_runner_count
      - active_runner_count
      - pace_state
    """

    PACE_COLUMNS_PR10: ClassVar[set[str]] = {
        "run_style_evidence_count",
        "run_style_source",
        "pace_band",
        "classified_runner_count",
        "active_runner_count",
        "pace_state",
    }

    def _setup_legacy_db(self, tmp_path: Path) -> tuple[Path, int]:
        from src.services.results_intake import _ensure_table, ensure_race_review_view
        from tests.conftest import insert_minimal_race

        db_path = tmp_path / "legacy_derbyedge.db"
        schema_text = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")

        conn = sqlite3.connect(db_path)
        conn.executescript(schema_text)
        _ensure_table(conn)
        ensure_race_review_view(conn)
        race = insert_minimal_race(conn)
        card_id = race["card_id"]

        # Drop the PR #10 pace columns to simulate an unmigrated local SQLite database
        for col in self.PACE_COLUMNS_PR10:
            conn.execute(f"ALTER TABLE feature_store DROP COLUMN {col}")
        conn.commit()

        cols_before = _col_names(conn, "feature_store")
        assert not (self.PACE_COLUMNS_PR10 & cols_before), "PR10 columns must be absent before migration"
        conn.close()
        return db_path, card_id

    def test_build_features_auto_migrates_legacy_feature_store(self, tmp_path, monkeypatch):
        import scripts.migrate_schema as migrate_module
        from src.features.builder import build_features

        db_path, card_id = self._setup_legacy_db(tmp_path)
        monkeypatch.setattr("src.utils.db.DB_PATH", db_path)

        # Track if migrate_schema was ever invoked (it should NOT be required)
        migrate_called = False

        def fake_migrate(*args, **kwargs):
            nonlocal migrate_called
            migrate_called = True
            raise AssertionError("Manual migrate_schema invocation should not be called!")

        monkeypatch.setattr(migrate_module, "main", fake_migrate)

        # 1. Run build_features directly
        feat_df = build_features(card_id=card_id)

        # 2. Assert no manual scripts/migrate_schema.py invocation was required
        assert not migrate_called

        # 3. Assert write succeeds
        assert not feat_df.empty
        assert len(feat_df) == 5

        # 4. Assert migration occurred and the six PR #10 columns now exist in DB
        conn = sqlite3.connect(db_path)
        cols_after = _col_names(conn, "feature_store")
        assert self.PACE_COLUMNS_PR10.issubset(cols_after)

        rows = conn.execute(
            "SELECT feature_id, card_id, run_style_evidence_count, run_style_source, "
            "pace_band, classified_runner_count, active_runner_count, pace_state "
            "FROM feature_store WHERE card_id = ?",
            (card_id,),
        ).fetchall()
        assert len(rows) == len(feat_df)
        conn.close()

    def test_build_features_cli_self_migrates_database(self, tmp_path, monkeypatch):
        import scripts.build_features as build_features_cli

        db_path, card_id = self._setup_legacy_db(tmp_path)
        monkeypatch.setattr("src.utils.db.DB_PATH", db_path)
        monkeypatch.setattr("sys.argv", ["build_features.py", "--card-id", str(card_id)])

        # Run CLI entrypoint
        ret = build_features_cli.main()
        assert ret == 0

        # Assert migration occurred and write succeeded
        conn = sqlite3.connect(db_path)
        cols_after = _col_names(conn, "feature_store")
        assert self.PACE_COLUMNS_PR10.issubset(cols_after)
        count = conn.execute("SELECT COUNT(*) FROM feature_store WHERE card_id = ?", (card_id,)).fetchone()[0]
        assert count == 5
        conn.close()

