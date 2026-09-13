"""Tests for the additive, transactional horse_starts provenance migration."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.migrate_horse_starts_provenance import backfill_provenance, ensure_provenance_columns, migrate


def _conn(*, artifact_as_of: str | None = "2026-05-09T12:00:00Z", document: str | None = "dk_markdown:abc") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE race_cards(card_id INTEGER PRIMARY KEY, card_date TEXT, surface TEXT, distance_furlongs REAL, stakes_name TEXT, race_class TEXT);
      CREATE TABLE horse_starts(start_id INTEGER PRIMARY KEY, entry_id INTEGER, horse_id INTEGER, card_id INTEGER, source_document_id TEXT);
      CREATE TABLE source_artifacts(artifact_id INTEGER PRIMARY KEY, sha256 TEXT, declared_as_of_timestamp TEXT);
      INSERT INTO race_cards VALUES(1,'2026-05-10','dirt',6.0,NULL,'CLM');
    """)
    conn.execute("INSERT INTO source_artifacts VALUES(1,'abc',?)", (artifact_as_of,))
    conn.execute("INSERT INTO horse_starts VALUES(1,1,1,1,?)", (document,))
    conn.commit()
    return conn


def _status(conn: sqlite3.Connection) -> tuple:
    return tuple(conn.execute("SELECT source_artifact_id,source_as_of_ts,provenance_status FROM horse_starts WHERE start_id=1").fetchone())


def test_migration_is_idempotent():
    conn = _conn()
    assert ensure_provenance_columns(conn) == ["source_artifact_id", "source_as_of_ts", "provenance_status"]
    conn.commit()
    first = backfill_provenance(conn)
    assert ensure_provenance_columns(conn) == []
    second = backfill_provenance(conn)
    assert first["classification_counts"] == second["classification_counts"] == {"REPAIRABLE": 1}
    assert _status(conn) == (1, "2026-05-09T12:00:00Z", "REPAIRABLE")


def test_backfill_marks_repairable_when_as_of_precedes_race_date():
    conn = _conn(); ensure_provenance_columns(conn); conn.commit(); backfill_provenance(conn)
    assert _status(conn)[2] == "REPAIRABLE"


def test_backfill_marks_partial_for_same_or_later_date_and_missing_as_of():
    for as_of in ("2026-05-10T00:00:00Z", None):
        conn = _conn(artifact_as_of=as_of); ensure_provenance_columns(conn); conn.commit(); backfill_provenance(conn)
        assert _status(conn)[2] == "REPAIRABLE_PARTIAL"


def test_backfill_marks_no_artifact_when_document_has_no_exact_artifact_match():
    conn = _conn(document="dk_markdown:not-present"); ensure_provenance_columns(conn); conn.commit(); backfill_provenance(conn)
    assert _status(conn) == (None, None, "INELIGIBLE_NO_ARTIFACT")


def test_backfill_transaction_rolls_back_on_injected_failure():
    conn = _conn(); ensure_provenance_columns(conn); conn.commit()
    try:
        backfill_provenance(conn, fail_after=1)
    except RuntimeError as exc:
        assert "injected" in str(exc)
    else:
        raise AssertionError("expected injected failure")
    assert _status(conn) == (None, None, None)


def test_migration_report_is_written_with_expected_keys():
    root = Path.cwd() / "output" / "acceptance" / "horse-starts-migration-test"
    root.mkdir(parents=True, exist_ok=True)
    db = root / "fixture.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
      DROP TABLE IF EXISTS horse_starts;
      DROP TABLE IF EXISTS source_artifacts;
      DROP TABLE IF EXISTS race_cards;
      CREATE TABLE race_cards(card_id INTEGER PRIMARY KEY, card_date TEXT, surface TEXT, distance_furlongs REAL, stakes_name TEXT, race_class TEXT);
      CREATE TABLE horse_starts(start_id INTEGER PRIMARY KEY, entry_id INTEGER, horse_id INTEGER, card_id INTEGER, source_document_id TEXT);
      CREATE TABLE source_artifacts(artifact_id INTEGER PRIMARY KEY, sha256 TEXT, declared_as_of_timestamp TEXT);
      INSERT INTO race_cards VALUES(1,'2026-05-10','dirt',6.0,NULL,'CLM');
      INSERT INTO source_artifacts VALUES(1,'abc','2026-05-09T12:00:00Z');
      INSERT INTO horse_starts VALUES(1,1,1,1,'dk_markdown:abc');
    """)
    conn.commit(); conn.close()
    report = migrate(db, root / "out")
    assert report["success"] is True
    assert set(report) >= {"added_columns", "backfill", "per_family", "report_path", "schema_gap"}
    assert Path(report["report_path"]).exists()
