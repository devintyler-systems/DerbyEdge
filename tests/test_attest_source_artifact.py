"""Focused tests for bounded operator source-artifact attestation."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.attest_source_artifact import attest, run


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE source_artifacts(
        artifact_id INTEGER PRIMARY KEY, source_filename TEXT,
        validation_status TEXT, declared_as_of_timestamp TEXT
      );
      CREATE TABLE race_cards(
        card_id INTEGER PRIMARY KEY, card_date TEXT, scheduled_post_time_utc TEXT
      );
      CREATE TABLE horse_starts(
        start_id INTEGER PRIMARY KEY, card_id INTEGER, start_date TEXT,
        source_artifact_id INTEGER, source_as_of_ts TEXT, provenance_status TEXT
      );
      INSERT INTO source_artifacts VALUES(2,'SAR_DK_Horse_R6_9-4-26.md','UNPROVEN','2026-09-04');
      INSERT INTO race_cards VALUES(73,'2026-09-04','2026-09-04T13:03:00');
      INSERT INTO horse_starts VALUES(1,73,'2025-09-04',2,'2026-09-04','REPAIRABLE_PARTIAL');
      INSERT INTO horse_starts VALUES(2,73,'2025-09-04',2,'2026-09-04','REPAIRABLE_PARTIAL');
    """)
    return conn


def _attest(conn: sqlite3.Connection, **kwargs: object) -> dict:
    payload: dict[str, object] = {
        "artifact_id": 2, "attested_timestamp": "2026-09-04T12:13:00",
        "attested_by": "Devin Tyler", "note": "attested local modification time",
    }
    payload.update(kwargs)
    return attest(conn, **payload)


def test_attestation_updates_artifact_and_lifts_linked_partial_rows():
    conn = _conn(); report = _attest(conn)
    row = conn.execute("SELECT validation_status,declared_as_of_timestamp,attested_by,attested_at,attestation_note FROM source_artifacts").fetchone()
    assert row[:3] == ("OPERATOR_ATTESTED", "2026-09-04T12:13:00", "Devin Tyler")
    assert row[3] and row[4] == "attested local modification time"
    assert conn.execute("SELECT COUNT(*) FROM horse_starts WHERE provenance_status='REPAIRABLE'").fetchone()[0] == 2
    assert report["before_provenance_status_counts"] == {"REPAIRABLE_PARTIAL": 2}
    assert report["after_provenance_status_counts"] == {"REPAIRABLE": 2}


def test_rejects_invalid_timestamp_and_postrace_timestamp():
    conn = _conn()
    with pytest.raises(ValueError, match="ISO-8601"):
        _attest(conn, attested_timestamp="not-a-time")
    with pytest.raises(ValueError, match="not strictly before"):
        _attest(conn, attested_timestamp="2026-09-04T13:03:00")
    assert conn.execute("SELECT validation_status FROM source_artifacts").fetchone()[0] == "UNPROVEN"


def test_transaction_rolls_back_on_injected_failure():
    conn = _conn()
    with pytest.raises(RuntimeError, match="injected"):
        _attest(conn, fail_after_update=True)
    assert conn.execute("SELECT validation_status FROM source_artifacts").fetchone()[0] == "UNPROVEN"
    assert conn.execute("SELECT COUNT(*) FROM horse_starts WHERE provenance_status='REPAIRABLE_PARTIAL'").fetchone()[0] == 2
    assert "attested_by" not in {row[1] for row in conn.execute("PRAGMA table_info(source_artifacts)")}


def test_dry_run_makes_no_database_changes():
    conn = _conn(); report = _attest(conn, dry_run=True)
    assert report["dry_run"] is True
    assert conn.execute("SELECT validation_status FROM source_artifacts").fetchone()[0] == "UNPROVEN"
    assert "attested_by" not in {row[1] for row in conn.execute("PRAGMA table_info(source_artifacts)")}
    assert conn.execute("SELECT COUNT(*) FROM horse_starts WHERE provenance_status='REPAIRABLE_PARTIAL'").fetchone()[0] == 2


def test_report_is_written_with_expected_keys():
    root = Path.cwd() / "output" / "acceptance" / "attestation-run-test"; root.mkdir(parents=True, exist_ok=True)
    db = root / "fixture.db"; conn = _conn()
    disk = sqlite3.connect(db); conn.backup(disk); disk.close(); conn.close()
    report = run(db, root / "out", artifact_id=2, attested_timestamp="2026-09-04T12:13:00", attested_by="Devin Tyler", note="test")
    assert set(report) >= {"artifact_id", "attestation_type", "before_provenance_status_counts", "after_provenance_status_counts", "report_path"}
    assert Path(report["report_path"]).exists()
