"""Fail-closed score-eligibility service tests without scoring or Streamlit."""
from __future__ import annotations

import json
import pickle
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.services.score_eligibility import active_feature_weights, evaluate_score_eligibility


ROOT = Path(__file__).resolve().parents[1]


def _artifact(features, *, calibration=None, policy=None):
    config = {"feature_groups": {"group": {"group_weight": 1.0, "features": features}}, "calibration_method": "temperature_softmax"}
    if policy: config["missingness_policy"] = policy
    return {"race_type_key": "dirt_sprint", "artifact_schema_version": 2, "training_rows": 1, "config": config,
            "calibration_audit": calibration or {"calibration_status": "valid", "audit_timestamp": "2026-09-04T12:00:00Z"}}


def _evaluate(feature_name="distance_fit", *, value=0.5, status="DERIVED", source="draftkings_markdown", evidence=1, artifact=None, race="NORMAL_RACE", as_of="2026-09-03"):
    artifact = artifact or _artifact({feature_name: 1.0})
    lineage = [{"feature_name": feature_name, "status": status, "source_system": source, "evidence_count": evidence, "as_of_max_date": as_of, "fallback_reason": None}]
    return evaluate_score_eligibility(
        race_context={"card_id": 73, "entry_id": 654, "race_family_classification": race},
        feature_row={feature_name: value, "feature_lineage_json": json.dumps(lineage)},
        decision={"run_timestamp": "2026-09-04T12:00:00Z"},
        model={"model_id": 6, "model_name": "fixture", "version": "1", "feature_schema_version": "2"}, artifact=artifact,
    )


def test_effective_weight_extraction_uses_group_composition():
    weights = active_feature_weights({"config": {"feature_groups": {"a": {"group_weight": 0.2, "features": {"x": 0.5}}, "b": {"group_weight": 0.8, "features": {"x": 0.25, "y": 1.0}}}}})
    assert weights[0]["feature_name"] == "x"
    assert weights[0]["effective_weight"] == pytest.approx(0.3)
    assert weights[0]["groups"] == ["a", "b"]
    assert weights[1] == {"feature_name": "y", "effective_weight": 0.8, "groups": ["b"]}


def test_normal_race_active_derby_feature_fails_closed():
    result = _evaluate("derby_override_score")
    assert not result["score_valid"]
    assert "derby_only_feature_active_for_normal_race" in result["active_rows"][0]["failure_reason"]


def test_non_null_placeholder_seeded_zero_evidence_fails_closed():
    result = _evaluate(value=0.5, status="PLACEHOLDER", source="seeded/default", evidence=0)
    assert not result["score_valid"]
    assert "active_input_unavailable_or_defaulted_without_audited_policy" in result["active_rows"][0]["failure_reason"]


def test_active_unavailable_speed_feature_without_policy_fails_closed():
    result = _evaluate("speed_last", value=None, status="UNAVAILABLE", source="seeded/default", evidence=0)
    assert not result["score_valid"]
    assert result["active_rows"][0]["missingness_policy_status"] == "NO_AUDITED_POLICY"


def test_audited_missingness_policy_stays_transparent_not_source_backed():
    policy = {"version": "missingness-v1", "allowed_features": ["speed_last"]}
    audit = {"calibration_status": "valid", "audit_timestamp": "2026-09-04T12:00:00Z", "missingness_policy_status": "covered", "missingness_policy_version": "missingness-v1"}
    result = _evaluate("speed_last", value=None, status="UNAVAILABLE", source="seeded/default", evidence=0, artifact=_artifact({"speed_last": 1.0}, calibration=audit, policy=policy))
    row = result["active_rows"][0]
    assert row["missingness_policy_status"] == "AUDITED_ALLOWED"
    assert row["lineage_status"] == "UNAVAILABLE"
    assert row["lineage_source"] == "seeded/default"


def test_calibration_unavailable_fails_closed():
    result = _evaluate(artifact=_artifact({"distance_fit": 1.0}, calibration={"calibration_status": "not_available"}))
    assert not result["score_valid"]
    assert "calibration_unavailable_or_incompatible" in result["reason_codes"]


def test_score_eligibility_cli_keeps_database_bytes_unchanged(tmp_path):
    artifact_path = tmp_path / "invalid-artifact.pkl"
    with artifact_path.open("wb") as handle:
        pickle.dump({"artifact_schema_version": 1, "config": {"feature_groups": {"speed": {"group_weight": 1, "features": {"speed_last": 1}}}}}, handle)
    db_path = tmp_path / "fixture.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE race_cards (card_id INTEGER, card_date TEXT, race_number INTEGER, surface TEXT, distance_yards INTEGER, distance_furlongs REAL, race_class TEXT, field_size INTEGER, stakes_name TEXT, scheduled_post_time_utc TEXT);
        CREATE TABLE entries (entry_id INTEGER, card_id INTEGER);
        CREATE TABLE feature_store (card_id INTEGER, entry_id INTEGER, build_ts TEXT, feature_source_mix TEXT, speed_last REAL, feature_lineage_json TEXT);
        CREATE TABLE score_runs (card_id INTEGER, run_timestamp TEXT, created_at TEXT, model_id INTEGER);
        CREATE TABLE model_registry (model_id INTEGER, artifact_path TEXT, model_name TEXT, version TEXT, feature_schema_version TEXT, target_race_type_key TEXT, training_window_start TEXT, training_window_end TEXT);
    """)
    conn.execute("INSERT INTO race_cards VALUES (73, '2026-09-04', 6, 'dirt', 1760, 8, 'claiming', 10, NULL, NULL)")
    conn.execute("INSERT INTO entries VALUES (654, 73)")
    conn.execute("INSERT INTO feature_store VALUES (73, 654, '2026-09-04T12:00:00Z', 'draftkings_markdown', NULL, ?)", (json.dumps([]),))
    conn.execute("INSERT INTO score_runs VALUES (73, '2026-09-04T12:00:00Z', '2026-09-04T12:00:00Z', 6)")
    conn.execute("INSERT INTO model_registry VALUES (6, ?, 'invalid', '1', '2', 'dirt_sprint', NULL, NULL)", (str(artifact_path),))
    conn.commit(); conn.close()
    before = db_path.read_bytes()
    completed = subprocess.run([sys.executable, "scripts/validate_score_eligibility.py", "--card-id", "73", "--entry-id", "654", "--db-path", str(db_path), "--output-dir", str(tmp_path / "out")], cwd=ROOT, text=True, capture_output=True, check=False)
    assert completed.returncode == 1
    assert "FAIL active_features=1 invalid_active_features=1" in completed.stdout
    assert db_path.read_bytes() == before
