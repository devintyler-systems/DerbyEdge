"""Synthetic end-to-end forward-capture contract dry-run harness.

Proves the full workflow -- raw pre-race artifact + result reference -> evidence
ledger -> forward-capture prefill -> operator-completed manifest -> historical
snapshot contract audit -- using only synthetic fixtures.  A contract-passing
manifest is possible only with complete explicit operator evidence; every
intermediate object stays ``DRAFT_NOT_ELIGIBLE`` / ``RECON_ONLY_NOT_ELIGIBLE``
and the harness report is ``DRY_RUN_ONLY_NOT_ELIGIBLE``.  No DB, no ingestion.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.services.forward_capture_dry_run import (
    DRY_RUN_STATUS,
    FAILURE_CASE_NAMES,
    build_synthetic_dry_run_inputs,
    execute_dry_run,
    write_dry_run_artifacts,
)
from src.services.historical_snapshot_contract_audit import validate_manifest_payload

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dry_run(tmp_path: Path):
    inputs = build_synthetic_dry_run_inputs(tmp_path / "fixtures")
    result = execute_dry_run(inputs, tmp_path / "out")
    return inputs, result


# ---- A. Intermediate stages remain non-eligible ---------------------------- #
def test_evidence_bundle_is_draft_not_eligible(dry_run) -> None:
    _, result = dry_run
    assert result.evidence_bundle_status == "DRAFT_NOT_ELIGIBLE"


def test_prefill_draft_is_draft_not_eligible(dry_run) -> None:
    _, result = dry_run
    assert result.prefill_draft_status == "DRAFT_NOT_ELIGIBLE"


def test_auto_generated_prefill_draft_fails_contract_audit(dry_run) -> None:
    _, result = dry_run
    assert result.incomplete_draft_audit_passed is False


# ---- B. Complete synthetic case ------------------------------------------- #
def test_completed_manifest_passes_only_with_full_evidence(dry_run) -> None:
    _, result = dry_run
    assert result.completed_manifest_audit_passed is True
    manifest = result.completed_manifest
    # tz-qualified as-of, strictly before post
    audit = validate_manifest_payload(manifest, manifest_path="m.json")
    assert audit["contract_pass"] is True
    assert audit["evaluated_conditions"]["source_as_of_timestamp"]["issue"] is None
    assert audit["evaluated_conditions"]["source_strictly_before_target_post"]["valid"] is True
    assert manifest["source_as_of_tier"] in ("PROVEN", "OPERATOR_ATTESTED")
    assert manifest["expected_active_starter_count"] == manifest["observed_active_starter_count"] >= 2
    assert manifest["identity_reconciliation_status"] == "COMPLETE"
    assert manifest["field_completeness_status"] == "COMPLETE"
    assert manifest["feature_vector_status"] == "COMPLETE"
    assert manifest["raw_artifact_retained"] is True
    assert manifest["pre_race_fields_only"] is True
    assert manifest["outcome_provenance_status"] in ("PROVEN", "OPERATOR_ATTESTED")


def test_completed_manifest_result_reference_matches_candidate_key(dry_run) -> None:
    inputs, result = dry_run
    manifest = result.completed_manifest
    assert manifest["target_track_code"] == inputs.candidate_key.split("|")[0]
    assert manifest["target_race_date"] == inputs.candidate_key.split("|")[1]
    assert manifest["target_race_number"] == int(inputs.candidate_key.split("|")[2].lstrip("R"))
    assert manifest["outcome_reference"]["sha256"] == inputs.result_sha256


def test_dropping_any_single_evidence_field_breaks_the_pass(dry_run) -> None:
    _, result = dry_run
    base = result.completed_manifest
    for field, bad in (
        ("source_as_of_tier", "UNKNOWN_TIER"),
        ("raw_artifact_retained", False),
        ("feature_vector_status", "PARTIAL"),
    ):
        broken = dict(base)
        broken[field] = bad
        assert validate_manifest_payload(broken, manifest_path="m.json")["contract_pass"] is False


# ---- C. Failure matrix --------------------------------------------------- #
def test_failure_matrix_every_case_is_rejected(dry_run) -> None:
    _, result = dry_run
    assert set(result.failure_case_results) == set(FAILURE_CASE_NAMES)
    for name, case in result.failure_case_results.items():
        assert case["rejected"] is True, f"{name} unexpectedly passed"
        assert case["expected"] == "REJECTED"


def test_failure_matrix_covers_required_cases(dry_run) -> None:
    _, result = dry_run
    for required in (
        "source_as_of_timestamp_no_timezone",
        "source_as_of_not_strictly_before_post",
        "missing_source_as_of_tier_or_provenance",
        "candidate_key_mismatch_pre_race_vs_result",
        "result_sha256_mismatch",
        "expected_observed_starter_count_mismatch",
        "identity_status_not_complete",
        "field_completeness_status_not_complete",
        "feature_vector_status_not_complete",
        "raw_artifact_retained_false",
        "pre_race_fields_only_false",
        "incomplete_outcome_provenance",
    ):
        assert required in result.failure_case_results


# ---- D. Side-effect assertions ----------------------------------------- #
def test_no_database_created_or_altered(tmp_path: Path) -> None:
    db = tmp_path / "db" / "derbyedge.db"
    db.parent.mkdir()
    db.write_bytes(b"SQLite format 3\x00 sentinel")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    inputs = build_synthetic_dry_run_inputs(tmp_path / "fixtures")
    result = execute_dry_run(inputs, tmp_path / "out")
    write_dry_run_artifacts(result, tmp_path / "out")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert [p for p in tmp_path.rglob("*.db") if p != db] == []
    assert [p for p in tmp_path.rglob("*.sqlite*")] == []


def test_no_files_written_outside_supplied_dirs(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    out = tmp_path / "out"
    inputs = build_synthetic_dry_run_inputs(fixtures)
    result = execute_dry_run(inputs, out)
    written = write_dry_run_artifacts(result, out)
    for path in written.values():
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
    assert all(p.resolve().is_relative_to(tmp_path.resolve()) for p in tmp_path.rglob("*"))


def test_prohibited_side_effect_checks_all_true(dry_run) -> None:
    _, result = dry_run
    assert result.prohibited_side_effect_checks
    assert all(result.prohibited_side_effect_checks.values())
    assert result.status == DRY_RUN_STATUS == "DRY_RUN_ONLY_NOT_ELIGIBLE"


def test_no_scoring_or_market_artifacts_produced(tmp_path: Path) -> None:
    inputs = build_synthetic_dry_run_inputs(tmp_path / "fixtures")
    result = execute_dry_run(inputs, tmp_path / "out")
    written = write_dry_run_artifacts(result, tmp_path / "out")
    names = " ".join(Path(p).name for p in written.values()).lower()
    for banned in ("score", "fair_odds", "market", "wager", "feature", "calibrat", "train"):
        assert banned not in names


def test_outputs_are_deterministic(tmp_path: Path) -> None:
    inputs = build_synthetic_dry_run_inputs(tmp_path / "fixtures")
    r1 = execute_dry_run(inputs, tmp_path / "o1")
    r2 = execute_dry_run(inputs, tmp_path / "o2")
    a = write_dry_run_artifacts(r1, tmp_path / "o1")
    b = write_dry_run_artifacts(r2, tmp_path / "o2")
    for key in a:
        assert Path(a[key]).read_bytes() == Path(b[key]).read_bytes()


def test_report_json_is_labelled_and_sorted(dry_run, tmp_path: Path) -> None:
    _, result = dry_run
    written = write_dry_run_artifacts(result, tmp_path / "rep")
    report = json.loads(Path(written["report"]).read_text(encoding="utf-8"))
    assert report["status"] == "DRY_RUN_ONLY_NOT_ELIGIBLE"
    assert report["completed_manifest_audit_passed"] is True
    assert report["incomplete_draft_audit_passed"] is False
    text = Path(written["report"]).read_text(encoding="utf-8")
    assert json.dumps(report, indent=2, sort_keys=True) + "\n" == text


# ---- CLI --------------------------------------------------------------- #
def test_cli_runs_green_and_is_deterministic(tmp_path: Path) -> None:
    db = ROOT / "db" / "derbyedge.db"
    db_before = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
    out = tmp_path / "acceptance"

    first = subprocess.run(
        [sys.executable, "scripts/run_forward_capture_dry_run.py", "--output-dir", str(out)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert first.returncode == 0, first.stderr + first.stdout
    assert "DRY_RUN_ONLY_NOT_ELIGIBLE" in first.stdout
    report_path = out / "forward_capture_dry_run_report.json"
    assert report_path.is_file()
    blob = report_path.read_bytes()

    second = subprocess.run(
        [sys.executable, "scripts/run_forward_capture_dry_run.py", "--output-dir", str(out)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert second.returncode == 0
    assert report_path.read_bytes() == blob

    if db_before is not None:
        assert hashlib.sha256(db.read_bytes()).hexdigest() == db_before


def test_cli_rejects_bad_options(tmp_path: Path) -> None:
    bad = subprocess.run(
        [sys.executable, "scripts/run_forward_capture_dry_run.py", "--not-a-flag", "x"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert bad.returncode == 2
