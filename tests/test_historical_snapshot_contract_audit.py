from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.services.historical_snapshot_contract_audit import audit_manifest, validate_manifest_payload, write_acceptance_result


def _manifest(tmp_path: Path, **overrides: object) -> dict[str, object]:
    artifact = tmp_path / "historical-card.raw"
    artifact.write_bytes(b"immutable historical pre-race card")
    payload: dict[str, object] = {
        "artifact_path_or_uri": artifact.name,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "source_provider": "example_provider",
        "parser_version": "snapshot-contract-test-v1",
        "source_as_of_timestamp": "2024-06-01T12:00:00-04:00",
        "source_as_of_tier": "PROVEN",
        "source_as_of_provenance": "PUBLISHER_TIMESTAMP",
        "target_track_code": "SAR",
        "target_race_date": "2024-06-01",
        "target_race_number": 6,
        "target_scheduled_post_timestamp": "2024-06-01T13:00:00-04:00",
        "target_surface": "dirt",
        "target_distance_furlongs": 6.0,
        "expected_active_starter_count": 8,
        "observed_active_starter_count": 8,
        "identity_reconciliation_status": "COMPLETE",
        "field_completeness_status": "COMPLETE",
        "feature_vector_status": "COMPLETE",
        "raw_artifact_retained": True,
        "pre_race_fields_only": True,
        "outcome_reference": {
            "path_or_uri": "https://example.invalid/results/SAR-2024-06-01-R6.json",
            "sha256": "a" * 64,
            "source_provider": "official_results",
            "parser_version": "results-v1",
        },
        "outcome_provenance_status": "PROVEN",
    }
    payload.update(overrides)
    return payload


def _write_manifest(tmp_path: Path, payload: dict[str, object], name: str = "manifest.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_valid_proven_manifest_with_local_sha_match(tmp_path: Path) -> None:
    result = audit_manifest(_write_manifest(tmp_path, _manifest(tmp_path)))
    assert result["contract_pass"] is True
    assert result["rejection_codes"] == []
    assert result["evaluated_conditions"]["artifact_sha256"]["status"] == "LOCAL_ARTIFACT_SHA256_VERIFIED"


def test_valid_operator_attested_manifest(tmp_path: Path) -> None:
    result = audit_manifest(_write_manifest(tmp_path, _manifest(
        tmp_path,
        source_as_of_tier="OPERATOR_ATTESTED",
        source_as_of_provenance="OPERATOR_ATTESTED",
        outcome_provenance_status="OPERATOR_ATTESTED",
    )))
    assert result["contract_pass"] is True


def test_equal_or_later_source_timestamp_rejects(tmp_path: Path) -> None:
    equal = validate_manifest_payload(_manifest(tmp_path, source_as_of_timestamp="2024-06-01T13:00:00-04:00"), manifest_path=tmp_path / "equal.json")
    later = validate_manifest_payload(_manifest(tmp_path, source_as_of_timestamp="2024-06-01T13:01:00-04:00"), manifest_path=tmp_path / "later.json")
    assert "SOURCE_AS_OF_NOT_STRICTLY_BEFORE_TARGET_POST" in equal["rejection_codes"]
    assert "SOURCE_AS_OF_NOT_STRICTLY_BEFORE_TARGET_POST" in later["rejection_codes"]


def test_date_only_and_filename_derived_provenance_reject(tmp_path: Path) -> None:
    date_only = validate_manifest_payload(_manifest(tmp_path, source_as_of_timestamp="2024-06-01"), manifest_path=tmp_path / "date.json")
    filename = validate_manifest_payload(_manifest(tmp_path, source_as_of_provenance="FILENAME_DERIVED"), manifest_path=tmp_path / "filename.json")
    assert "SOURCE_AS_OF_TIMESTAMP_DATE_ONLY" in date_only["rejection_codes"]
    assert "FILENAME_DERIVED_CANNOT_CLAIM_PROVEN" in filename["rejection_codes"]


def test_incomplete_field_identity_and_sha_mismatch_reject(tmp_path: Path) -> None:
    incomplete = validate_manifest_payload(_manifest(tmp_path, identity_reconciliation_status="INCOMPLETE"), manifest_path=tmp_path / "incomplete.json")
    insufficient = validate_manifest_payload(_manifest(tmp_path, expected_active_starter_count=1, observed_active_starter_count=1), manifest_path=tmp_path / "insufficient.json")
    mismatch = validate_manifest_payload(_manifest(tmp_path, sha256="b" * 64), manifest_path=tmp_path / "mismatch.json")
    assert "IDENTITY_RECONCILIATION_INCOMPLETE" in incomplete["rejection_codes"]
    assert "ACTIVE_STARTER_COUNT_INSUFFICIENT" in insufficient["rejection_codes"]
    assert "RAW_ARTIFACT_SHA256_MISMATCH" in mismatch["rejection_codes"]


def test_template_fails_and_acceptance_json_is_deterministic(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    template = root / "templates" / "historical_pre_race_snapshot_manifest.template.json"
    template_result = audit_manifest(template)
    assert template_result["contract_pass"] is False
    payload = _manifest(tmp_path)
    result = audit_manifest(_write_manifest(tmp_path, payload, "stable.json"))
    output_dir = tmp_path / "output" / "acceptance"
    first = write_acceptance_result(result, tmp_path / "stable.json", output_dir)
    initial = first.read_bytes()
    second = write_acceptance_result(result, tmp_path / "stable.json", output_dir)
    assert first == second
    assert second.read_bytes() == initial


def test_audit_never_creates_or_changes_sqlite_db(tmp_path: Path) -> None:
    db_path = tmp_path / "audit-must-not-open.db"
    assert not db_path.exists()
    result = audit_manifest(_write_manifest(tmp_path, _manifest(tmp_path)))
    assert result["contract_pass"] is True
    assert not db_path.exists()
