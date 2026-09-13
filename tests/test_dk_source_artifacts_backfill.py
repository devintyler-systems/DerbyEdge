"""Focused DK source-artifact persistence and backfill tests."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from scripts.backfill_dk_source_artifacts import _date_from_filename, backfill, run
from src.services.draftkings_markdown_intake import _persist_dk_source_artifact


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
      CREATE TABLE race_cards(card_id INTEGER PRIMARY KEY,card_date TEXT,surface TEXT,distance_furlongs REAL,stakes_name TEXT,race_class TEXT);
      CREATE TABLE source_artifacts(artifact_id INTEGER PRIMARY KEY,source_provider TEXT NOT NULL,source_filename TEXT NOT NULL,source_path TEXT,raw_bytes BLOB NOT NULL,sha256 TEXT NOT NULL,parser_version TEXT NOT NULL,ingestion_timestamp TEXT NOT NULL,declared_as_of_timestamp TEXT,as_of_status TEXT NOT NULL,validation_status TEXT NOT NULL,validation_errors_json TEXT NOT NULL,normalized_record_count INTEGER NOT NULL,normalized_records_json TEXT NOT NULL,card_id INTEGER,UNIQUE(source_provider,sha256,parser_version,card_id));
      CREATE TABLE dk_markdown_imports(import_id INTEGER PRIMARY KEY,file_sha256 TEXT NOT NULL UNIQUE,source_filename TEXT NOT NULL,source_path TEXT,source_format TEXT NOT NULL,parser_version TEXT NOT NULL,validation_status TEXT NOT NULL,validation_json TEXT NOT NULL,scoring_as_of TEXT NOT NULL,card_id INTEGER,source_artifact_id INTEGER);
      CREATE TABLE horse_starts(start_id INTEGER PRIMARY KEY,card_id INTEGER,source_document_id TEXT,source_artifact_id INTEGER,source_as_of_ts TEXT,provenance_status TEXT);
      INSERT INTO race_cards VALUES(1,'2026-09-04','dirt',6.0,NULL,'CLM');
    """)


def _import(conn: sqlite3.Connection, *, filename: str = "SAR_DK_Horse_R6_9-4-26.md", raw: bytes = b"raw") -> str:
    sha = hashlib.sha256(raw).hexdigest()
    conn.execute("INSERT INTO dk_markdown_imports VALUES(1,?,?,?,?,?,?,?,?,?,NULL)", (sha, filename, None, "draftkings_markdown", "1.0.0", "PASS", "{}", "2026-09-04T00:00:00Z", 1))
    conn.execute("INSERT INTO horse_starts VALUES(1,1,?,NULL,NULL,NULL)", (f"dk_markdown:{sha}",)); conn.commit()
    return sha


def test_intake_persists_filename_derived_unproven_artifact_idempotently():
    conn = sqlite3.connect(":memory:"); _schema(conn); raw = b"exact raw"; sha = _import(conn, raw=raw)
    card = SimpleNamespace(raw_bytes=raw, source_sha256=sha, parser_version="1.0.0", entries=[])
    first = _persist_dk_source_artifact(conn, card, source_filename="SAR_DK_Horse_R6_9-4-26.md", source_path=None, card_id=1)
    second = _persist_dk_source_artifact(conn, card, source_filename="SAR_DK_Horse_R6_9-4-26.md", source_path=None, card_id=1)
    row = conn.execute("SELECT sha256,declared_as_of_timestamp,as_of_status,validation_status FROM source_artifacts").fetchone()
    assert first == second and row == (sha, "2026-09-04", "UNPROVEN", "UNPROVEN")
    assert conn.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 1


def test_backfill_filename_pattern_and_partial_status():
    assert _date_from_filename("SAR_DK_Horse_R6_9-4-26.md") == "2026-09-04"
    root = Path.cwd() / "output" / "acceptance" / "dk-backfill-test"; root.mkdir(parents=True, exist_ok=True)
    raw = b"backfill"; path = root / "SAR_DK_Horse_R6_9-4-26.md"; path.write_bytes(raw)
    conn = sqlite3.connect(":memory:"); _schema(conn); sha = _import(conn, raw=raw)
    conn.execute("UPDATE dk_markdown_imports SET source_path=? WHERE file_sha256=?", (str(path), sha)); conn.commit()
    outcome = backfill(conn)
    assert outcome["horse_starts_linked"] == 1
    assert conn.execute("SELECT provenance_status FROM horse_starts").fetchone()[0] == "REPAIRABLE_PARTIAL"


def test_backfill_marks_no_artifact_when_filename_date_is_unextractable():
    conn = sqlite3.connect(":memory:"); _schema(conn); _import(conn, filename="unknown.md")
    backfill(conn)
    assert conn.execute("SELECT provenance_status FROM horse_starts").fetchone()[0] == "INELIGIBLE_NO_ARTIFACT"


def test_backfill_rolls_back_on_injected_failure():
    root = Path.cwd() / "output" / "acceptance" / "dk-backfill-test"; root.mkdir(parents=True, exist_ok=True)
    raw = b"rollback"; path = root / "SAR_DK_Horse_R6_9-4-26.md"; path.write_bytes(raw)
    conn = sqlite3.connect(":memory:"); _schema(conn); sha = _import(conn, raw=raw)
    conn.execute("UPDATE dk_markdown_imports SET source_path=? WHERE file_sha256=?", (str(path), sha)); conn.commit()
    try: backfill(conn, fail_after=1)
    except RuntimeError: pass
    else: raise AssertionError("expected injected failure")
    assert conn.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 0
    assert conn.execute("SELECT provenance_status FROM horse_starts").fetchone()[0] is None


def test_report_written_with_expected_keys():
    root = Path.cwd() / "output" / "acceptance" / "dk-backfill-run-test"; root.mkdir(parents=True, exist_ok=True)
    db = root / "fixture.db"; conn = sqlite3.connect(db)
    conn.executescript("DROP TABLE IF EXISTS horse_starts; DROP TABLE IF EXISTS dk_markdown_imports; DROP TABLE IF EXISTS source_artifacts; DROP TABLE IF EXISTS race_cards;"); _schema(conn)
    raw = b"report"; path = root / "SAR_DK_Horse_R6_9-4-26.md"; path.write_bytes(raw); sha = _import(conn, raw=raw)
    conn.execute("UPDATE dk_markdown_imports SET source_path=? WHERE file_sha256=?", (str(path), sha)); conn.commit(); conn.close()
    report = run(db, root / "out")
    assert set(report) >= {"before", "after", "horse_starts_linked", "report_path"}
    assert Path(report["report_path"]).exists()
