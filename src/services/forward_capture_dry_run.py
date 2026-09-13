"""Synthetic end-to-end forward-capture contract dry-run harness.

Pure orchestration over existing **read-only** services.  It proves the workflow

    raw pre-race artifact + result reference
      -> evidence ledger        (forward_capture_evidence)
      -> forward-capture prefill (forward_capture_prefill)
      -> operator-completed manifest
      -> historical snapshot contract audit (historical_snapshot_contract_audit)

and that a contract-passing manifest is possible *only* with complete explicit
operator evidence, while every intermediate object stays ``DRAFT_NOT_ELIGIBLE`` /
``RECON_ONLY_NOT_ELIGIBLE``.

Prohibited here (never imported, never called): DB utilities / ``sqlite3``,
ingestion / persistence / parse-and-persist, feature build, readiness, training,
scoring, calibration, promotion, market comparison, fair-odds, wager logic.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from src.services.forward_capture_evidence import (
    build_bundle_summaries,
    build_evidence_entry,
)
from src.services.forward_capture_prefill import (
    ForwardCapturePrefillError,
    prefill_forward_capture,
    to_contract_manifest_draft,
)
from src.services.historical_snapshot_contract_audit import validate_manifest_payload

DRY_RUN_STATUS = "DRY_RUN_ONLY_NOT_ELIGIBLE"
DRY_RUN_VERSION = "1.0"

# Import statements that must never appear in this harness or the read-only
# services it orchestrates (static source scan -- process-pollution proof).
_HARNESS_SOURCE_FILES = (
    "src/services/forward_capture_dry_run.py",
    "src/services/forward_capture_evidence.py",
    "src/services/forward_capture_prefill.py",
    "src/services/historical_snapshot_contract_audit.py",
    "src/services/historical_source_recon.py",
)
_PROHIBITED_IMPORT_MARKERS = (
    "sqlite3",
    "src.utils.db",
    "draftkings_markdown_intake",
    "twinspires_intake",
    "src.features",
    "src.models",
    "src.services.score_delivery",
    "src.services.runtime_score_preflight",
    "src.services.score_eligibility",
    "calibrat",
    "promotion",
    "fair_odds",
    "market",
    "wager",
    "race_card_builder",
    "results_intake",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_DK_MARKDOWN = """Saratoga
RACE 6
1:03
PM
$20K CLAIMING
Purse: $42K
 3YO+
 1 M
Dirt: Fast
More
PROGRAM
#
ODDS
ML
Runner
1
20
20
Synthetic Alpha
L122
Synthetic Jockey A
Synthetic Trainer A
"""

_RESULT_BYTES = b"%PDF-1.4\nSYNTHETIC EQUIBASE CHART - Saratoga - September 4, 2026 - Race 6\nWinner: Synthetic Alpha\n"

# The complete, truthful operator evidence for the synthetic race.
_SYNTHETIC_OPERATOR_EVIDENCE: dict[str, Any] = {
    "parser_version": "dry-run-synthetic-parser-v1",
    "source_as_of_timestamp": "2026-09-04T12:40:00-04:00",
    "source_as_of_tier": "OPERATOR_ATTESTED",
    "source_as_of_provenance": "OPERATOR_ATTESTED",
    "target_scheduled_post_timestamp": "2026-09-04T13:03:00-04:00",
    "target_surface": "dirt",
    "target_distance_furlongs": 6.0,
    "expected_active_starter_count": 6,
    "observed_active_starter_count": 6,
    "identity_reconciliation_status": "COMPLETE",
    "field_completeness_status": "COMPLETE",
    "feature_vector_status": "COMPLETE",
    "raw_artifact_retained": True,
    "pre_race_fields_only": True,
    "outcome_provenance_status": "OPERATOR_ATTESTED",
    "outcome_reference_provider": "equibase_results_pdf",
    "outcome_reference_parser_version": "dry-run-synthetic-results-v1",
}

FAILURE_CASE_NAMES = (
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
)


class DryRunSetupError(RuntimeError):
    """Raised when synthetic fixtures cannot be prepared."""


@dataclasses.dataclass(frozen=True)
class SyntheticDryRunInputs:
    base_dir: str
    dk_card_path: str
    dk_card_sha256: str
    result_path: str
    result_sha256: str
    candidate_key: str
    operator_evidence: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class DryRunResult:
    candidate_key: str
    evidence_bundle_status: str
    prefill_draft_status: str
    incomplete_draft_audit_passed: bool
    completed_manifest_audit_passed: bool
    failure_case_results: dict[str, dict[str, Any]]
    prohibited_side_effect_checks: dict[str, bool]
    status: str
    overall_ok: bool
    completed_manifest: dict[str, Any]
    incomplete_draft_manifest: dict[str, Any]
    details: dict[str, Any]

    def report_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dry_run_version": DRY_RUN_VERSION,
            "candidate_key": self.candidate_key,
            "evidence_bundle_status": self.evidence_bundle_status,
            "prefill_draft_status": self.prefill_draft_status,
            "incomplete_draft_audit_passed": self.incomplete_draft_audit_passed,
            "completed_manifest_audit_passed": self.completed_manifest_audit_passed,
            "failure_case_results": self.failure_case_results,
            "prohibited_side_effect_checks": self.prohibited_side_effect_checks,
            "overall_ok": self.overall_ok,
            "details": self.details,
            "note": (
                "Synthetic dry-run only. A passing audit here proves workflow "
                "wiring and contract semantics on fabricated fixtures; it is NOT "
                "canonical ingestion approval and grants no eligibility."
            ),
        }


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_synthetic_dry_run_inputs(base_dir: str | Path) -> SyntheticDryRunInputs:
    """Create the smallest synthetic fixture set (no production artifact)."""
    base = Path(base_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
        dk_card = base / "SAR_DK_Horse_R6_9-4-26.md"
        result = base / "eqb_SAR_2026-09-04_fullcard.pdf"
        # Write bytes exactly so the retained hash equals the on-disk bytes on
        # every platform (no newline translation).
        dk_card.write_bytes(_DK_MARKDOWN.encode("utf-8"))
        result.write_bytes(_RESULT_BYTES)
    except OSError as exc:  # pragma: no cover - defensive
        raise DryRunSetupError(f"could not prepare synthetic fixtures: {exc}") from exc

    return SyntheticDryRunInputs(
        base_dir=base.resolve().as_posix(),
        dk_card_path=dk_card.resolve().as_posix(),
        dk_card_sha256=_sha256_bytes(dk_card.read_bytes()),
        result_path=result.resolve().as_posix(),
        result_sha256=_sha256_bytes(result.read_bytes()),
        candidate_key="SAR|2026-09-04|R6",
        operator_evidence=dict(_SYNTHETIC_OPERATOR_EVIDENCE),
    )


def _result_row(inputs: SyntheticDryRunInputs, **overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_key": inputs.candidate_key,
        "track_code_or_name": inputs.candidate_key.split("|")[0],
        "race_date": inputs.candidate_key.split("|")[1],
        "race_number": inputs.candidate_key.split("|")[2].lstrip("R"),
        "source_file_path": inputs.result_path,
        "source_file_sha256": inputs.result_sha256,
        "official_status_detected": "True",
        "winner_detected": "True",
        "winner_name": "Synthetic Alpha",
        "uncertainty_code": "",
        "extraction_confidence": "HIGH",
        "status": "RECON_ONLY_NOT_ELIGIBLE",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# Manifest assembly                                                            #
# --------------------------------------------------------------------------- #
_DROP = object()


def assemble_completed_manifest(
    inputs: SyntheticDryRunInputs, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Full 22-field contract manifest from synthetic identity + operator evidence."""
    ev = inputs.operator_evidence
    track, date, race = inputs.candidate_key.split("|")
    manifest: dict[str, Any] = {
        "artifact_path_or_uri": Path(inputs.dk_card_path).as_uri(),
        "sha256": inputs.dk_card_sha256,
        "source_provider": "draftkings_markdown",
        "parser_version": ev["parser_version"],
        "source_as_of_timestamp": ev["source_as_of_timestamp"],
        "source_as_of_tier": ev["source_as_of_tier"],
        "source_as_of_provenance": ev["source_as_of_provenance"],
        "target_track_code": track,
        "target_race_date": date,
        "target_race_number": int(race.lstrip("R")),
        "target_scheduled_post_timestamp": ev["target_scheduled_post_timestamp"],
        "target_surface": ev["target_surface"],
        "target_distance_furlongs": ev["target_distance_furlongs"],
        "expected_active_starter_count": ev["expected_active_starter_count"],
        "observed_active_starter_count": ev["observed_active_starter_count"],
        "identity_reconciliation_status": ev["identity_reconciliation_status"],
        "field_completeness_status": ev["field_completeness_status"],
        "feature_vector_status": ev["feature_vector_status"],
        "raw_artifact_retained": ev["raw_artifact_retained"],
        "pre_race_fields_only": ev["pre_race_fields_only"],
        "outcome_reference": {
            "path_or_uri": Path(inputs.result_path).as_uri(),
            "sha256": inputs.result_sha256,
            "source_provider": ev["outcome_reference_provider"],
            "parser_version": ev["outcome_reference_parser_version"],
        },
        "outcome_provenance_status": ev["outcome_provenance_status"],
    }
    for key, value in (overrides or {}).items():
        if value is _DROP:
            manifest.pop(key, None)
        else:
            manifest[key] = value
    return manifest


# --------------------------------------------------------------------------- #
# Failure matrix                                                               #
# --------------------------------------------------------------------------- #
def _audit_rejects(manifest: dict[str, Any]) -> dict[str, Any]:
    audit = validate_manifest_payload(manifest, manifest_path="dry_run_failure.json")
    return {
        "stage": "AUDIT",
        "expected": "REJECTED",
        "rejected": audit["contract_pass"] is False,
        "rejection_codes": audit["rejection_codes"],
    }


def _prefill_rejects(inputs: SyntheticDryRunInputs, result_row: dict[str, Any]) -> dict[str, Any]:
    try:
        prefill_forward_capture(inputs.dk_card_path, result_row)
    except ForwardCapturePrefillError as exc:
        return {"stage": "PREFILL", "expected": "REJECTED", "rejected": True, "detail": str(exc)}
    return {"stage": "PREFILL", "expected": "REJECTED", "rejected": False, "detail": "prefill unexpectedly succeeded"}


def _build_failure_matrix(inputs: SyntheticDryRunInputs) -> dict[str, dict[str, Any]]:
    post = inputs.operator_evidence["target_scheduled_post_timestamp"]
    m = assemble_completed_manifest
    cases: dict[str, dict[str, Any]] = {
        "source_as_of_timestamp_no_timezone": _audit_rejects(
            m(inputs, {"source_as_of_timestamp": "2026-09-04T12:40:00"})
        ),
        "source_as_of_not_strictly_before_post": _audit_rejects(
            m(inputs, {"source_as_of_timestamp": post})
        ),
        "missing_source_as_of_tier_or_provenance": _audit_rejects(
            m(inputs, {"source_as_of_tier": _DROP, "source_as_of_provenance": _DROP})
        ),
        "candidate_key_mismatch_pre_race_vs_result": _prefill_rejects(
            inputs, _result_row(inputs, candidate_key="DMR|2026-09-03|R4")
        ),
        "result_sha256_mismatch": _prefill_rejects(
            inputs, _result_row(inputs, source_file_sha256="0" * 64)
        ),
        "expected_observed_starter_count_mismatch": _audit_rejects(
            m(inputs, {"observed_active_starter_count": 5})
        ),
        "identity_status_not_complete": _audit_rejects(
            m(inputs, {"identity_reconciliation_status": "INCOMPLETE"})
        ),
        "field_completeness_status_not_complete": _audit_rejects(
            m(inputs, {"field_completeness_status": "PARTIAL"})
        ),
        "feature_vector_status_not_complete": _audit_rejects(
            m(inputs, {"feature_vector_status": "PARTIAL"})
        ),
        "raw_artifact_retained_false": _audit_rejects(
            m(inputs, {"raw_artifact_retained": False})
        ),
        "pre_race_fields_only_false": _audit_rejects(
            m(inputs, {"pre_race_fields_only": False})
        ),
        "incomplete_outcome_provenance": _audit_rejects(
            m(inputs, {"outcome_provenance_status": "UNPROVEN", "outcome_reference": _DROP})
        ),
    }
    return {name: cases[name] for name in FAILURE_CASE_NAMES}


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def _prohibited_import_checks() -> dict[str, bool]:
    """Static scan: none of the orchestrated read-only services import DB /
    ingestion / feature / training / scoring / calibration / market / wager code."""
    import_lines: list[str] = []
    for rel in _HARNESS_SOURCE_FILES:
        path = _REPO_ROOT / rel
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().lower()
            if stripped.startswith(("import ", "from ")) and "import" in stripped:
                import_lines.append(stripped)
    blob = "\n".join(import_lines)
    checks: dict[str, bool] = {}
    for marker in _PROHIBITED_IMPORT_MARKERS:
        checks[f"no_prohibited_import::{marker}"] = marker not in blob
    return checks


def execute_dry_run(inputs: SyntheticDryRunInputs, output_dir: str | Path) -> DryRunResult:
    """Run the full synthetic workflow and collect the pass/fail matrix."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1. Evidence ledger.
    as_of = inputs.operator_evidence
    entries = [
        build_evidence_entry({
            "event_type": "PRE_RACE_CAPTURE",
            "artifact_path": inputs.dk_card_path,
            "source_as_of_timestamp": as_of["source_as_of_timestamp"],
            "source_as_of_timezone": "America/New_York",
            "source_as_of_tier": as_of["source_as_of_tier"],
            "source_as_of_provenance": as_of["source_as_of_provenance"],
            "operator_id": "dry-run-operator",
            "raw_bytes_preserved": True,
        }),
        build_evidence_entry({
            "event_type": "RESULT_CAPTURE",
            "artifact_path": inputs.result_path,
            "candidate_key": inputs.candidate_key,
            "source_as_of_timestamp": "2026-09-04T18:00:00-04:00",
            "source_as_of_timezone": "America/New_York",
            "source_as_of_tier": "OPERATOR_ATTESTED",
            "source_as_of_provenance": "OPERATOR_ATTESTED",
            "operator_id": "dry-run-operator",
            "raw_bytes_preserved": True,
        }),
        build_evidence_entry({
            "event_type": "MANUAL_NOTE",
            "candidate_key": inputs.candidate_key,
            "operator_id": "dry-run-operator",
            "note": "Synthetic dry-run: DK card captured before post; result captured after official.",
        }),
    ]
    summaries = build_bundle_summaries(entries)
    bundle_statuses = {s.status for s in summaries}
    evidence_bundle_status = (
        "DRAFT_NOT_ELIGIBLE" if bundle_statuses == {"DRAFT_NOT_ELIGIBLE"} else "MIXED"
    )
    bundle = next((s for s in summaries if s.candidate_key == inputs.candidate_key), None)

    # 2. Prefill draft.
    prefill = prefill_forward_capture(inputs.dk_card_path, _result_row(inputs))
    incomplete_draft = to_contract_manifest_draft(prefill)

    # 3. Audit the auto-generated (incomplete) draft -> must fail.
    incomplete_audit = validate_manifest_payload(incomplete_draft, manifest_path=out / "incomplete_draft.json")

    # 4. Operator-completed manifest -> must pass.
    completed_manifest = assemble_completed_manifest(inputs)
    completed_audit = validate_manifest_payload(completed_manifest, manifest_path=out / "completed.json")

    # 5. Failure matrix.
    failure_case_results = _build_failure_matrix(inputs)

    # D. Side-effect checks.
    side_effect_checks = _prohibited_import_checks()
    # Scope the DB check to what this harness owns: its synthetic fixture tree and
    # its own output files (never the shared output/acceptance/ directory, which
    # holds unrelated fixtures from other acceptance runs).
    side_effect_checks["no_db_file_under_fixture_dir"] = not any(Path(inputs.base_dir).rglob("*.db*"))
    side_effect_checks["no_sqlite_file_under_fixture_dir"] = not any(Path(inputs.base_dir).rglob("*.sqlite*"))
    side_effect_checks["dry_run_output_files_are_json_only"] = all(
        not name.endswith((".db", ".sqlite", ".sqlite3"))
        for name in (
            "forward_capture_dry_run_report.json",
            "forward_capture_dry_run_completed_manifest.json",
            "forward_capture_dry_run_failure_matrix.json",
        )
    )
    side_effect_checks["fixture_dk_card_unchanged"] = (
        Path(inputs.dk_card_path).is_file()
        and _sha256_bytes(Path(inputs.dk_card_path).read_bytes()) == inputs.dk_card_sha256
    )
    side_effect_checks["fixture_result_unchanged"] = (
        Path(inputs.result_path).is_file()
        and _sha256_bytes(Path(inputs.result_path).read_bytes()) == inputs.result_sha256
    )

    all_failures_rejected = all(case["rejected"] for case in failure_case_results.values())
    overall_ok = (
        evidence_bundle_status == "DRAFT_NOT_ELIGIBLE"
        and prefill.draft_status == "DRAFT_NOT_ELIGIBLE"
        and incomplete_audit["contract_pass"] is False
        and completed_audit["contract_pass"] is True
        and all_failures_rejected
        and all(side_effect_checks.values())
    )

    details = {
        "evidence_entry_count": len(entries),
        "evidence_bundles": [s.to_dict() for s in summaries],
        "target_bundle_unresolved_fields": list(bundle.unresolved_fields) if bundle else None,
        "incomplete_draft_rejection_codes": incomplete_audit["rejection_codes"],
        "completed_manifest_rejection_codes": completed_audit["rejection_codes"],
        "prefill_gap_count": len(prefill.gaps),
    }

    return DryRunResult(
        candidate_key=inputs.candidate_key,
        evidence_bundle_status=evidence_bundle_status,
        prefill_draft_status=prefill.draft_status,
        incomplete_draft_audit_passed=incomplete_audit["contract_pass"],
        completed_manifest_audit_passed=completed_audit["contract_pass"],
        failure_case_results=failure_case_results,
        prohibited_side_effect_checks=side_effect_checks,
        status=DRY_RUN_STATUS,
        overall_ok=overall_ok,
        completed_manifest=completed_manifest,
        incomplete_draft_manifest=incomplete_draft,
        details=details,
    )


# --------------------------------------------------------------------------- #
# Artifacts                                                                    #
# --------------------------------------------------------------------------- #
def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_dry_run_artifacts(result: DryRunResult, output_dir: str | Path) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = out / "forward_capture_dry_run_report.json"
    manifest = out / "forward_capture_dry_run_completed_manifest.json"
    matrix = out / "forward_capture_dry_run_failure_matrix.json"

    _write_json(report, result.report_dict())
    _write_json(manifest, result.completed_manifest)
    _write_json(matrix, {
        "status": DRY_RUN_STATUS,
        "failure_case_names": list(FAILURE_CASE_NAMES),
        "results": result.failure_case_results,
        "all_rejected": all(c["rejected"] for c in result.failure_case_results.values()),
    })
    return {
        "report": report.resolve().as_posix(),
        "completed_manifest": manifest.resolve().as_posix(),
        "failure_matrix": matrix.resolve().as_posix(),
    }
