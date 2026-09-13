"""Contract tests for the permanent read-only model-readiness audit."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from src.services.model_readiness_audit import (
    _inventory,
    calibration_readiness,
    classify_model_family,
    open_readonly_connection,
    propose_chronological_race_grouped_splits,
    run_model_readiness_audit,
)


def _make_db(path: Path, *, source_as_of: str = "2026-01-01T10:00:00Z", with_outcome: bool = True, lineage_source: str = "canonical_db") -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE tracks(track_id INTEGER PRIMARY KEY, abbrev TEXT);
    CREATE TABLE race_cards(card_id INTEGER PRIMARY KEY, track_id INTEGER, card_date TEXT,
      scheduled_post_time_utc TEXT, surface TEXT, distance_furlongs REAL, race_class TEXT,
      age_restriction TEXT, field_size INTEGER, stakes_name TEXT, race_number INTEGER);
    CREATE TABLE entries(entry_id INTEGER PRIMARY KEY, card_id INTEGER, horse_id INTEGER,
      post_position INTEGER, scratch_flag INTEGER);
    CREATE TABLE feature_store(feature_id INTEGER PRIMARY KEY, card_id INTEGER, entry_id INTEGER,
      horse_id INTEGER, build_ts TEXT, speed_last REAL, feature_lineage_json TEXT);
    CREATE TABLE horse_starts(entry_id INTEGER, finish_position INTEGER);
    CREATE TABLE dk_markdown_imports(card_id INTEGER, file_sha256 TEXT, parser_version TEXT,
      validation_status TEXT, scoring_as_of TEXT);
    CREATE TABLE model_registry(model_id INTEGER PRIMARY KEY, model_name TEXT, model_family TEXT,
      version TEXT, artifact_path TEXT, calibration_artifact_path TEXT);
    INSERT INTO tracks VALUES(1,'TST');
    INSERT INTO race_cards VALUES(1,1,'2026-01-02','2026-01-02T12:00:00Z','dirt',6.0,'CLM',
      '3UP',8,NULL,1);
    INSERT INTO entries VALUES(10,1,100,1,0);
    """)
    lineage = json.dumps([{"feature_name": "speed_last", "status": "IMPLEMENTED", "source_system": lineage_source, "evidence_count": 2, "as_of_max_date": "2026-01-01T09:00:00Z"}])
    conn.execute("INSERT INTO feature_store VALUES(1,1,10,100,'2026-01-01T11:00:00Z',90.0,?)", (lineage,))
    conn.execute("INSERT INTO dk_markdown_imports VALUES(1,'sha','v1','PASS',?)", (source_as_of,))
    if with_outcome:
        conn.execute("INSERT INTO horse_starts VALUES(10,1)")
    conn.commit(); conn.close()
    return path


def _contract() -> dict:
    return {"dirt_sprint": {"registry": {"model_id": 9, "version": "test"}, "active_features": [{"feature_name": "speed_last", "effective_weight": 1.0}], "artifact": {}, "calibration_audit": {}}}


def test_read_only_audit_leaves_database_bytes_unchanged(tmp_path: Path):
    db = _make_db(tmp_path / "audit.db")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = run_model_readiness_audit(db, tmp_path / "out")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert result["summary"]["read_only"] is True
    assert (tmp_path / "out" / "model_readiness_inventory.csv").exists()


def test_race_group_integrity_and_chronological_order():
    rows = []
    for race_id in range(1, 10):
        for runner in range(2):
            rows.append({"card_id": race_id, "race_date": f"2026-01-{race_id:02d}", "training_eligible": True, "_outcome": {"finish_position": 1 if runner == 0 else 2}})
    splits = propose_chronological_race_grouped_splits(rows, min_races_per_fold=1, min_wins_per_fold=1)
    seen = set()
    for split in splits:
        race_ids = set(json.loads(split["race_ids"]))
        assert not seen.intersection(race_ids)
        seen.update(race_ids)
    assert splits[0]["date_max"] < splits[1]["date_min"] < splits[2]["date_min"]


def test_post_race_as_of_invalid_is_excluded(tmp_path: Path):
    db = _make_db(tmp_path / "audit.db", source_as_of="2026-01-03T10:00:00Z")
    conn = open_readonly_connection(db)
    try:
        rows, exclusions = _inventory(conn, _contract())
    finally:
        conn.close()
    assert rows[0]["training_eligible"] is False
    assert any(row["reason_code"] == "SOURCE_PROVENANCE_OR_AS_OF_INVALID" for row in exclusions)


def test_missing_result_outcome_linkage_is_excluded(tmp_path: Path):
    db = _make_db(tmp_path / "audit.db", with_outcome=False)
    conn = open_readonly_connection(db)
    try:
        rows, _ = _inventory(conn, _contract())
    finally:
        conn.close()
    assert "OUTCOME_LINKAGE_MISSING" in rows[0]["exclusion_reason_codes"]


def test_seeded_defaulted_active_feature_is_excluded(tmp_path: Path):
    db = _make_db(tmp_path / "audit.db", lineage_source="seeded/default")
    conn = open_readonly_connection(db)
    try:
        rows, _ = _inventory(conn, _contract())
    finally:
        conn.close()
    assert "ACTIVE_FEATURE_SEEDED_OR_DEFAULTED" in rows[0]["exclusion_reason_codes"]


def test_kentucky_derby_is_not_an_ordinary_dirt_route():
    assert classify_model_family("dirt", 10.0, "Kentucky Derby", "G1") == "kentucky_derby"
    assert classify_model_family("dirt", 10.0, "", "G1") == "dirt_route"


def test_absent_calibration_audit_is_non_ready():
    state = calibration_readiness({"registry": {"version": "1", "calibration_artifact_path": "x"}, "artifact": {"config": {"calibration_method": "temperature_softmax"}}, "calibration_audit": {}}, True)
    assert state["calibration_status"] == "CALIBRATION_UNAUDITED"
    assert state["production_eligible"] is False


def test_card_73_reminder_never_marks_scores_or_wagers_valid(tmp_path: Path):
    # The reminder is intentionally deterministic even in a minimal fixture.
    db = _make_db(tmp_path / "audit.db")
    result = run_model_readiness_audit(db, tmp_path / "out")
    reminder = result["summary"]["card_73_operational_reminder"]
    assert reminder["score_eligibility_remains_false"] is True
    assert reminder["fair_odds_valid"] is False
    assert reminder["market_comparison_valid"] is False
    assert reminder["wager_valid"] is False


def test_audit_never_trains_calibrates_scores_or_ingests(tmp_path: Path):
    db = _make_db(tmp_path / "audit.db")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = run_model_readiness_audit(db, tmp_path / "out")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert result["summary"]["training_or_calibration_performed"] is False
    assert result["summary"]["feature_build_or_rescore_performed"] is False
    assert result["summary"]["source_ingestion_or_reconciliation_performed"] is False
