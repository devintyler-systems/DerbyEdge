"""Read-only validator for future historical pre-race snapshot acquisition.

This module deliberately has no database or pipeline dependency.  It validates a
manifest and, when an artifact path is locally accessible, verifies its exact raw
bytes against the declared SHA-256.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "historical_pre_race_snapshot_manifest.schema.json"
AUDIT_VERSION = "1.0"
_OUTCOME_ACCEPTED = frozenset({"PROVEN", "OPERATOR_ATTESTED"})


def load_manifest_schema() -> dict[str, Any]:
    """Load the versioned acquisition contract schema from the repository."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _schema_errors(payload: Any) -> list[dict[str, Any]]:
    validator = Draft202012Validator(load_manifest_schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: (list(error.absolute_path), error.message))
    return [
        {
            "path": "/".join(str(part) for part in error.absolute_path) or "$",
            "validator": error.validator,
            "message": error.message,
        }
        for error in errors
    ]


def _timestamp(value: Any) -> tuple[datetime | None, str | None]:
    text = str(value or "").strip()
    if "T" not in text and " " not in text:
        return None, "DATE_ONLY"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, "INVALID"
    if parsed.tzinfo is None:
        return None, "TIMEZONE_MISSING"
    return parsed, None


def _local_path(value: str, base_dir: Path) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return Path(path)
    if parsed.scheme:
        return None
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def _artifact_hash_condition(manifest: Mapping[str, Any], base_dir: Path) -> dict[str, Any]:
    local = _local_path(str(manifest.get("artifact_path_or_uri", "")), base_dir)
    if local is None:
        return {"status": "DURABLE_REFERENCE_NOT_LOCALLY_VERIFIED", "local_path": None, "sha256_match": None}
    if not local.is_file():
        return {"status": "DURABLE_REFERENCE_NOT_LOCALLY_VERIFIED", "local_path": str(local), "sha256_match": None}
    actual = hashlib.sha256(local.read_bytes()).hexdigest()
    expected = str(manifest.get("sha256", "")).lower()
    return {
        "status": "LOCAL_ARTIFACT_SHA256_VERIFIED" if actual == expected else "LOCAL_ARTIFACT_SHA256_MISMATCH",
        "local_path": str(local),
        "sha256_match": actual == expected,
        "actual_sha256": actual,
    }


def validate_manifest_payload(payload: Any, *, manifest_path: str | Path = "manifest.json") -> dict[str, Any]:
    """Evaluate one manifest without opening or modifying any database."""
    path = Path(manifest_path)
    errors = _schema_errors(payload)
    conditions: dict[str, Any] = {"schema": {"valid": not errors, "errors": errors}}
    codes: list[str] = []
    if errors:
        codes.append("MANIFEST_SCHEMA_INVALID")
    if not isinstance(payload, Mapping):
        return _result(path, conditions, codes)

    source, source_issue = _timestamp(payload.get("source_as_of_timestamp"))
    target, target_issue = _timestamp(payload.get("target_scheduled_post_timestamp"))
    conditions["source_as_of_timestamp"] = {"valid": source_issue is None, "issue": source_issue}
    conditions["target_scheduled_post_timestamp"] = {"valid": target_issue is None, "issue": target_issue}
    if source_issue == "DATE_ONLY":
        codes.append("SOURCE_AS_OF_TIMESTAMP_DATE_ONLY")
    elif source_issue:
        codes.append("SOURCE_AS_OF_TIMESTAMP_INVALID_OR_TIMEZONE_MISSING")
    if target_issue:
        codes.append("TARGET_SCHEDULED_POST_TIMESTAMP_INVALID_OR_TIMEZONE_MISSING")
    if source is not None and target is not None:
        before = source < target
        conditions["source_strictly_before_target_post"] = {"valid": before}
        if not before:
            codes.append("SOURCE_AS_OF_NOT_STRICTLY_BEFORE_TARGET_POST")
    else:
        conditions["source_strictly_before_target_post"] = {"valid": False, "not_evaluated": True}

    filename_only = str(payload.get("source_as_of_provenance", "")).upper() == "FILENAME_DERIVED"
    proven = str(payload.get("source_as_of_tier", "")).upper() == "PROVEN"
    conditions["filename_provenance_is_not_proven"] = {"valid": not (filename_only and proven)}
    if filename_only and proven:
        codes.append("FILENAME_DERIVED_CANNOT_CLAIM_PROVEN")

    expected = payload.get("expected_active_starter_count")
    observed = payload.get("observed_active_starter_count")
    identity = str(payload.get("identity_reconciliation_status", "")).upper()
    complete_field = str(payload.get("field_completeness_status", "")).upper()
    vectors = str(payload.get("feature_vector_status", "")).upper()
    field_valid = (
        isinstance(expected, int) and expected >= 2 and observed == expected
        and identity == "COMPLETE" and complete_field == "COMPLETE" and vectors == "COMPLETE"
    )
    conditions["complete_non_scratched_field_and_vectors"] = {
        "valid": field_valid, "expected_active_starter_count": expected,
        "observed_active_starter_count": observed, "identity_reconciliation_status": identity,
        "field_completeness_status": complete_field, "feature_vector_status": vectors,
    }
    if not isinstance(expected, int) or expected < 2:
        codes.append("ACTIVE_STARTER_COUNT_INSUFFICIENT")
    if identity != "COMPLETE":
        codes.append("IDENTITY_RECONCILIATION_INCOMPLETE")
    if complete_field != "COMPLETE" or observed != expected:
        codes.append("PARTIAL_STARTER_FIELD")
    if vectors != "COMPLETE":
        codes.append("PARTIAL_FEATURE_VECTOR")

    raw_retained = payload.get("raw_artifact_retained") is True
    pre_race_only = payload.get("pre_race_fields_only") is True
    outcome_status = str(payload.get("outcome_provenance_status", "")).upper()
    conditions["raw_artifact_retained"] = {"valid": raw_retained}
    conditions["pre_race_fields_only"] = {"valid": pre_race_only}
    conditions["outcome_provenance"] = {"valid": outcome_status in _OUTCOME_ACCEPTED, "status": outcome_status}
    if not raw_retained:
        codes.append("RAW_ARTIFACT_NOT_RETAINED")
    if not pre_race_only:
        codes.append("PRE_RACE_FIELDS_ONLY_REQUIRED")
    if outcome_status not in _OUTCOME_ACCEPTED:
        codes.append("OUTCOME_PROVENANCE_INSUFFICIENT")

    artifact_hash = _artifact_hash_condition(payload, path.parent)
    conditions["artifact_sha256"] = artifact_hash
    if artifact_hash["status"] == "LOCAL_ARTIFACT_SHA256_MISMATCH":
        codes.append("RAW_ARTIFACT_SHA256_MISMATCH")
    return _result(path, conditions, codes)


def _result(path: Path, conditions: Mapping[str, Any], codes: list[str]) -> dict[str, Any]:
    rejection_codes = list(dict.fromkeys(codes))
    return {
        "audit_version": AUDIT_VERSION,
        "contract_pass": not rejection_codes,
        "manifest_path": str(path),
        "rejection_codes": rejection_codes,
        "evaluated_conditions": conditions,
    }


def audit_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Load and validate a JSON manifest; filesystem reads are strictly read-only."""
    path = Path(manifest_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result(path, {"manifest_json": {"valid": False, "error": str(exc)}}, ["MANIFEST_JSON_UNREADABLE"])
    return validate_manifest_payload(payload, manifest_path=path)


def write_acceptance_result(result: Mapping[str, Any], manifest_path: str | Path, output_dir: str | Path) -> Path:
    """Write the sole audit output; callers choose the gitignored acceptance directory."""
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(manifest_path).stem) or "manifest"
    path = Path(output_dir) / f"historical_snapshot_contract_{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
