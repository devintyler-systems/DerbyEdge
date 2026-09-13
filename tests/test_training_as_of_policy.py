"""Anti-leakage tests separating PP runtime evidence from training evidence."""
from __future__ import annotations

import json
import sqlite3

from src.services.horse_starts_provenance_audit import collect_horse_starts_provenance
from src.services.model_readiness_audit import _inventory
from src.services.training_as_of import (
    MISSING_SOURCE_ARTIFACT,
    MISSING_SOURCE_AS_OF,
    MISSING_TARGET_DECISION_TIME,
    POST_RACE_TARGET_SNAPSHOT,
    PRE_RACE_TARGET_PROVEN,
    training_as_of_status,
)


def _contract() -> dict:
    return {"dirt_sprint": {"registry": {"model_id": 1, "version": "test"}, "active_features": [{"feature_name": "speed_last", "effective_weight": 1.0}], "artifact": {}, "calibration_audit": {}}}


def _conn(*, source_time: str, target_date: str, validation: str = "PASS", as_of: str = "PROVEN") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE tracks(track_id INTEGER PRIMARY KEY, abbrev TEXT);
      CREATE TABLE race_cards(card_id INTEGER PRIMARY KEY, track_id INTEGER, card_date TEXT, scheduled_post_time_utc TEXT, surface TEXT, distance_furlongs REAL, race_class TEXT, age_restriction TEXT, field_size INTEGER, stakes_name TEXT, race_number INTEGER);
      CREATE TABLE entries(entry_id INTEGER PRIMARY KEY, card_id INTEGER, horse_id INTEGER, post_position INTEGER, scratch_flag INTEGER);
      CREATE TABLE feature_store(feature_id INTEGER PRIMARY KEY, card_id INTEGER, entry_id INTEGER, horse_id INTEGER, build_ts TEXT, speed_last REAL, feature_lineage_json TEXT);
      CREATE TABLE source_artifacts(artifact_id INTEGER PRIMARY KEY, card_id INTEGER, source_provider TEXT, sha256 TEXT, parser_version TEXT, validation_status TEXT, declared_as_of_timestamp TEXT, as_of_status TEXT);
      CREATE TABLE horse_starts(start_id INTEGER PRIMARY KEY, entry_id INTEGER, horse_id INTEGER, card_id INTEGER, finish_position INTEGER, start_date TEXT, source_artifact_id INTEGER, source_as_of_ts TEXT, provenance_status TEXT, surface TEXT, distance_furlongs REAL, race_class_raw TEXT);
      INSERT INTO tracks VALUES(1,'TST');
      INSERT INTO race_cards VALUES(73,1,'2026-09-04','2026-09-04T13:05:00','dirt',6.0,'CLM','3UP',8,NULL,6);
      INSERT INTO entries VALUES(10,73,100,1,0);
    """)
    lineage = json.dumps([{"feature_name": "speed_last", "status": "IMPLEMENTED", "source_system": "canonical_db", "evidence_count": 2, "as_of_max_date": "2026-09-04T12:00:00"}])
    conn.execute("INSERT INTO feature_store VALUES(1,73,10,100,'2026-09-04T12:30:00',90.0,?)", (lineage,))
    conn.execute("INSERT INTO source_artifacts VALUES(2,73,'draftkings_markdown','sha','v1',?,?,?)", (validation, source_time, as_of))
    conn.execute("INSERT INTO horse_starts VALUES(311,10,100,73,1,?,2,?,'REPAIRABLE','dirt',6.0,'CLM')", (target_date, source_time))
    conn.commit()
    return conn


def test_later_pp_is_runtime_repairable_but_training_postrace_leakage():
    conn = _conn(source_time="2026-09-04T12:13:00", target_date="2026-07-17", validation="OPERATOR_ATTESTED", as_of="UNPROVEN")
    provenance, _ = collect_horse_starts_provenance(conn)
    assert provenance[0]["classification"] == "REPAIRABLE"
    assert provenance[0]["training_as_of_status"] == POST_RACE_TARGET_SNAPSHOT
    rows, exclusions = _inventory(conn, _contract())
    assert rows[0]["training_eligible"] is False
    assert rows[0]["training_as_of_status"] == POST_RACE_TARGET_SNAPSHOT
    assert any(item["reason_code"] == "POST_RACE_TARGET_SNAPSHOT_LEAKAGE" and item["training_as_of_status"] == POST_RACE_TARGET_SNAPSHOT for item in exclusions)


def test_pre_target_source_requires_valid_artifact_status_for_eligibility():
    valid = _conn(source_time="2026-07-16T12:00:00", target_date="2026-07-17T13:00:00")
    rows, _ = _inventory(valid, _contract())
    assert rows[0]["training_as_of_status"] == PRE_RACE_TARGET_PROVEN
    assert rows[0]["training_eligible"] is True
    invalid = _conn(source_time="2026-07-16T12:00:00", target_date="2026-07-17T13:00:00", validation="UNPROVEN", as_of="UNPROVEN")
    rows, _ = _inventory(invalid, _contract())
    assert rows[0]["training_eligible"] is False
    assert "SOURCE_PROVENANCE_OR_AS_OF_INVALID" in rows[0]["exclusion_reason_codes"]


def test_missing_provenance_and_target_time_have_distinct_statuses():
    common = {"artifact_validation_status": "PASS", "artifact_as_of_status": "PROVEN"}
    assert training_as_of_status(source_artifact_id=None, source_as_of_timestamp="2026-01-01T01:00:00", target_decision_timestamp="2026-01-02T01:00:00", **common) == MISSING_SOURCE_ARTIFACT
    assert training_as_of_status(source_artifact_id=1, source_as_of_timestamp=None, target_decision_timestamp="2026-01-02T01:00:00", **common) == MISSING_SOURCE_AS_OF
    assert training_as_of_status(source_artifact_id=1, source_as_of_timestamp="2026-01-01T01:00:00", target_decision_timestamp=None, **common) == MISSING_TARGET_DECISION_TIME
    assert training_as_of_status(source_artifact_id=1, source_as_of_timestamp="2026-09-04T12:13:00", target_decision_timestamp="2026-09-04", **common) == MISSING_TARGET_DECISION_TIME


def test_operator_parent_attestation_never_qualifies_earlier_target_and_derby_is_unchanged():
    conn = _conn(source_time="2026-09-04T12:13:00", target_date="2026-07-17", validation="OPERATOR_ATTESTED", as_of="UNPROVEN")
    rows, exclusions = _inventory(conn, _contract())
    assert rows[0]["training_eligible"] is False
    assert "POST_RACE_TARGET_SNAPSHOT_LEAKAGE" in rows[0]["exclusion_reason_codes"]
    assert any(item["reason_code"] == "SOURCE_PROVENANCE_OR_AS_OF_INVALID" for item in exclusions)
    assert rows[0]["model_family"] == "dirt_sprint"
