"""Focused read-only provenance-audit tests using an in-memory SQLite fixture."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.services.horse_starts_provenance_audit import (
    ROW_COLUMNS,
    SUMMARY_COLUMNS,
    collect_horse_starts_provenance,
    run_horse_starts_provenance_audit,
    summarize_horse_starts_provenance,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE race_cards(card_id INTEGER PRIMARY KEY, card_date TEXT, surface TEXT,
        distance_furlongs REAL, stakes_name TEXT, race_class TEXT);
      CREATE TABLE horse_starts(start_id INTEGER PRIMARY KEY, entry_id INTEGER, horse_id INTEGER,
        card_id INTEGER, source_provider TEXT, source_artifact_id INTEGER, source_as_of_ts TEXT,
        surface TEXT, distance_furlongs REAL, race_class_raw TEXT);
      CREATE TABLE source_artifacts(artifact_id INTEGER PRIMARY KEY, source_as_of_ts TEXT);
      INSERT INTO race_cards VALUES(1,'2026-05-10','dirt',6.0,NULL,'CLM');
      INSERT INTO source_artifacts VALUES(11,'2026-05-09T12:00:00Z');
      INSERT INTO source_artifacts VALUES(12,NULL);
      INSERT INTO source_artifacts VALUES(13,'2026-05-10T00:00:00Z');
    """)
    return conn


def _row(conn: sqlite3.Connection, start_id: int, artifact_id: int | None):
    conn.execute("INSERT INTO horse_starts VALUES(?,?,?,?,?,?,?,?,?,?)", (start_id, start_id, start_id, 1, "provider", artifact_id, None, "dirt", 6.0, "CLM"))
    conn.commit()
    rows, _ = collect_horse_starts_provenance(conn)
    return rows[0]


def test_repairable_when_artifact_as_of_is_pre_race():
    conn = _conn()
    assert _row(conn, 1, 11)["classification"] == "REPAIRABLE"


def test_repairable_partial_when_artifact_as_of_is_missing():
    conn = _conn()
    assert _row(conn, 1, 12)["classification"] == "REPAIRABLE_PARTIAL"


def test_ineligible_no_artifact_when_artifact_id_is_null():
    conn = _conn()
    assert _row(conn, 1, None)["classification"] == "INELIGIBLE_NO_ARTIFACT"


def test_ineligible_postrace_when_artifact_as_of_is_not_before_race_date():
    conn = _conn()
    assert _row(conn, 1, 13)["classification"] == "INELIGIBLE_POSTRACE"


def test_per_family_summary_counts_are_correct():
    conn = _conn()
    for start_id, artifact_id in ((1, 11), (2, 12), (3, None), (4, 13)):
        conn.execute("INSERT INTO horse_starts VALUES(?,?,?,?,?,?,?,?,?,?)", (start_id, start_id, start_id, 1, "provider", artifact_id, None, "dirt", 6.0, "CLM"))
    conn.commit()
    rows, _ = collect_horse_starts_provenance(conn)
    summary = summarize_horse_starts_provenance(rows)
    assert summary == [{"model_family": "dirt_sprint", "total_rows": 4, "repairable_count": 1, "repairable_partial_count": 1, "ineligible_no_artifact_count": 1, "ineligible_postrace_count": 1, "first_repair_blocker": "INELIGIBLE_NO_ARTIFACT"}]


def test_output_artifacts_have_expected_columns():
    output_root = Path.cwd() / "output" / "acceptance" / "horse-starts-provenance-test"
    output_root.mkdir(parents=True, exist_ok=True)
    db = output_root / "fixture.db"
    disk = sqlite3.connect(db)
    disk.executescript("""
      DROP TABLE IF EXISTS horse_starts;
      DROP TABLE IF EXISTS source_artifacts;
      DROP TABLE IF EXISTS race_cards;
      CREATE TABLE race_cards(card_id INTEGER PRIMARY KEY, card_date TEXT, surface TEXT, distance_furlongs REAL, stakes_name TEXT, race_class TEXT);
      CREATE TABLE horse_starts(start_id INTEGER PRIMARY KEY, entry_id INTEGER, horse_id INTEGER, card_id INTEGER, source_provider TEXT, source_artifact_id INTEGER, source_as_of_ts TEXT, surface TEXT, distance_furlongs REAL, race_class_raw TEXT);
      CREATE TABLE source_artifacts(artifact_id INTEGER PRIMARY KEY, source_as_of_ts TEXT);
      INSERT INTO race_cards VALUES(1,'2026-05-10','dirt',6.0,NULL,'CLM');
      INSERT INTO source_artifacts VALUES(1,'2026-05-09T00:00:00Z');
      INSERT INTO horse_starts VALUES(1,1,1,1,'provider',1,NULL,'dirt',6.0,'CLM');
    """)
    disk.commit(); disk.close()
    result = run_horse_starts_provenance_audit(db, output_root / "out")
    assert result["summary"]["read_only"] is True
    assert tuple((output_root / "out" / "horse_starts_provenance_audit.csv").read_text(encoding="utf-8").splitlines()[0].split(",")) == ROW_COLUMNS
    assert tuple((output_root / "out" / "horse_starts_provenance_summary.csv").read_text(encoding="utf-8").splitlines()[0].split(",")) == SUMMARY_COLUMNS
    assert (output_root / "out" / "horse_starts_provenance_audit.json").exists()
