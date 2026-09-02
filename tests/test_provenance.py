"""Synthetic-only contract tests for provenance manifests."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.provenance import ManifestValidationError, load_and_validate_manifest, validate_manifest


SHA = "a" * 64
COMMIT = "abcdef1"


def raw_manifest(asset_class: str = "raw_input_snapshot") -> dict:
    manifest = {
        "manifest_id": "raw-20260902-001",
        "asset_class": asset_class,
        "source_provider": "Synthetic Racing Feed",
        "source_url_or_reference": "fixture://race-card-001",
        "retrieved_at_utc": "2026-09-02T12:00:00Z",
        "as_of_utc": "2026-09-02T11:59:00Z",
        "license_or_terms_reference": "synthetic-test-only",
        "file_sha256": SHA,
        "file_size_bytes": 42,
        "schema_fingerprint": SHA,
        "race_scope": {"race_id": "SAR-20260902-R01"},
        "ingestion_tool_version": "test-1.0"
    }
    if asset_class == "curated_seed_fixture":
        manifest.update({"fixture_id": "fixture-race-001", "fixture_schema_version": "v1"})
    return manifest


def model_manifest() -> dict:
    return {
        "manifest_id": "model-manifest-001", "asset_class": "fitted_model_artifact",
        "model_id": "dirt-route-v1", "model_family": "xgboost",
        "target_definition": "win_probability", "training_window": {
            "start_utc": "2024-01-01T00:00:00Z", "end_utc": "2025-12-31T23:59:59Z"
        },
        "training_data_manifest_ids": ["raw-20260902-001"],
        "feature_schema_version": "v1", "feature_columns_sha256": SHA,
        "code_commit_sha": COMMIT, "training_started_at_utc": "2026-09-01T10:00:00Z",
        "training_completed_at_utc": "2026-09-01T10:05:00Z",
        "evaluation_metrics": {"log_loss": 0.62},
        "calibration_summary": {"ece": 0.03}, "promotion_status": "shadow",
        "artifact_sha256": SHA
    }


def run_manifest() -> dict:
    return {
        "manifest_id": "run-manifest-001", "asset_class": "scored_race_run",
        "run_id": "run-20260902-001", "race_id": "SAR-20260902-R01",
        "decision_timestamp_utc": "2026-09-02T12:30:00Z", "code_commit_sha": COMMIT,
        "model_id": "dirt-route-v1", "raw_input_manifest_ids": ["raw-20260902-001"],
        "feature_schema_version": "v1", "odds_snapshot_timestamp_utc": "2026-09-02T12:29:00Z",
        "output_sha256s": {"board.csv": SHA}, "probability_sum": 1.0,
        "bet_policy_version": "v1"
    }


@pytest.mark.parametrize(
    ("manifest", "kind"),
    [(raw_manifest(), "raw_input"), (raw_manifest("curated_seed_fixture"), "raw_input"),
     (model_manifest(), "model"), (run_manifest(), "run")],
)
def test_valid_manifest_contracts(manifest, kind):
    validate_manifest(manifest, kind)


@pytest.mark.parametrize(
    ("manifest", "kind", "field"),
    [(raw_manifest(), "raw_input", "source_provider"),
     (model_manifest(), "model", "artifact_sha256"),
     (run_manifest(), "run", "bet_policy_version")],
)
def test_missing_required_field_fails_closed(manifest, kind, field):
    del manifest[field]
    with pytest.raises(ManifestValidationError, match=field):
        validate_manifest(manifest, kind)


def test_invalid_checksum_and_probability_sum_fail_closed():
    raw = raw_manifest()
    raw["file_sha256"] = "not-a-sha"
    with pytest.raises(ManifestValidationError, match="file_sha256"):
        validate_manifest(raw, "raw_input")

    run = run_manifest()
    run["probability_sum"] = 0.9
    with pytest.raises(ManifestValidationError, match="probability_sum"):
        validate_manifest(run, "run")


def test_curated_seed_requires_fixture_identity():
    fixture = raw_manifest("curated_seed_fixture")
    del fixture["fixture_id"]
    with pytest.raises(ManifestValidationError, match="fixture_id"):
        validate_manifest(fixture, "raw_input")


def test_load_and_validate_small_synthetic_fixture():
    path = Path(__file__).parent / "fixtures" / "provenance" / "run_manifest.json"
    assert load_and_validate_manifest(path, "run")["run_id"] == "run-fixture-001"


def test_load_and_validate_manifest_rejects_missing_file():
    with pytest.raises(ManifestValidationError, match="Cannot read JSON manifest"):
        load_and_validate_manifest("does-not-exist.json", "run")
