"""Compatibility contract for serialized model artifacts."""
from __future__ import annotations

import pickle

from src.models.trainer import (
    ModelArtifact,
    calibration_audit_for_display,
    load_model_artifact,
    normalize_or_migrate_model_artifact,
)


def _artifact() -> ModelArtifact:
    return ModelArtifact(
        model_type="seed_only_baseline",
        race_type_key="dirt_route",
        model_name="test",
        version="test",
        training_rows=0,
        temperature=1.0,
        feature_importances={},
        group_scores={},
        config={},
    )


def test_legacy_artifact_missing_calibration_audit_is_normalized():
    artifact = _artifact()
    del artifact.calibration_audit
    del artifact.artifact_schema_version

    normalized = normalize_or_migrate_model_artifact(artifact)

    assert normalized.calibration_audit == {
        "schema_status": "missing_on_legacy_artifact",
        "calibration_status": "not_available",
        "temperature_adjustment_status": "not_available",
    }
    assert normalized.artifact_schema_version == 1


def test_display_accessor_reports_unavailable_for_legacy_artifact():
    artifact = _artifact()
    del artifact.calibration_audit

    assert (
        calibration_audit_for_display(artifact)["temperature_adjustment_status"]
        == "not_available"
    )


def test_deserialization_boundary_migrates_legacy_pickle(tmp_path):
    artifact = _artifact()
    del artifact.calibration_audit
    del artifact.artifact_schema_version
    path = tmp_path / "legacy.pkl"
    with path.open("wb") as handle:
        pickle.dump(artifact, handle)

    loaded = load_model_artifact(path)

    assert loaded.calibration_audit["schema_status"] == "missing_on_legacy_artifact"
    assert loaded.artifact_schema_version == 1


def test_new_artifacts_receive_non_shared_empty_audit_defaults():
    first, second = _artifact(), _artifact()

    assert first.calibration_audit == {}
    assert second.calibration_audit == {}
    assert first.calibration_audit is not second.calibration_audit
    assert first.artifact_schema_version >= 2


def test_none_calibration_audit_is_normalized_as_legacy_metadata():
    artifact = _artifact()
    artifact.calibration_audit = None

    assert (
        normalize_or_migrate_model_artifact(artifact)
        .calibration_audit["calibration_status"]
        == "not_available"
    )
