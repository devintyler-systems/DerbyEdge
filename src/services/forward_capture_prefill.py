"""Read-only forward-capture manifest prefill helper.

Given a *future* DraftKings pre-race card file path and an Equibase result-index
row (or JSON), this produces a **draft worksheet**:

* deterministic identity fields (track, date, race number, candidate key, local
  paths, SHA-256s, provider candidates) are prefilled;
* every contract field that still needs operator-supplied evidence
  (as-of timestamp/tier/provenance, scheduled post timestamp, surface, distance,
  starter counts, identity/field/vector reconciliation, parser version, outcome
  provenance) is listed explicitly as a gap;
* the whole artifact is labelled ``DRAFT_NOT_ELIGIBLE``.

It writes no database rows, opens no SQLite connection, and never calls
ingestion / readiness / training / scoring code.  The draft it emits cannot pass
``historical_snapshot_contract_audit`` without the operator gaps being filled.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from src.services.historical_source_recon import (
    CLASS_PRE_RACE,
    _inventory_one,
)

DRAFT_LABEL = "DRAFT_NOT_ELIGIBLE"
PREFILL_VERSION = "1.0"

_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_KEY_RE = re.compile(r"^(?P<track>[A-Za-z0-9_-]{2,12})\|(?P<date>\d{4}-\d{2}-\d{2})\|R(?P<race>\d+)$")

# Contract fields that reconnaissance/prefill can never satisfy on its own.
_OPERATOR_GAPS: tuple[tuple[str, str, str], ...] = (
    ("source_as_of_timestamp", "REQUIRES_OPERATOR",
     "timezone-qualified capture/as-of time from provider, HTTP/download log, or dated attestation"),
    ("source_as_of_tier", "REQUIRES_OPERATOR",
     "declare PROVEN or OPERATOR_ATTESTED; a filename date is never PROVEN"),
    ("source_as_of_provenance", "REQUIRES_OPERATOR",
     "declare PUBLISHER_TIMESTAMP / SYSTEM_CAPTURED / OPERATOR_ATTESTED"),
    ("target_scheduled_post_timestamp", "REQUIRES_OPERATOR",
     "timezone-qualified scheduled post datetime; a card wall-clock string is insufficient"),
    ("target_surface", "REQUIRES_OPERATOR_OR_PARSE",
     "confirm surface enum from a deterministic parse or operator entry"),
    ("target_distance_furlongs", "REQUIRES_OPERATOR_OR_PARSE",
     "confirm distance in furlongs from a deterministic parse or operator entry"),
    ("expected_active_starter_count", "REQUIRES_OPERATOR_OR_PARSE",
     "enumerate the complete non-scratched starter field (>= 2)"),
    ("observed_active_starter_count", "REQUIRES_OPERATOR_OR_PARSE",
     "observed non-scratched starter count must equal expected"),
    ("identity_reconciliation_status", "REQUIRES_OPERATOR_OR_PARSE",
     "every active starter must reconcile to a stable identity (COMPLETE)"),
    ("field_completeness_status", "REQUIRES_OPERATOR_OR_PARSE",
     "every active starter field must be complete (COMPLETE)"),
    ("feature_vector_status", "REQUIRES_OPERATOR_OR_PARSE",
     "every active starter must have a complete pre-race feature vector (COMPLETE)"),
    ("pre_race_fields_only", "REQUIRES_OPERATOR_OR_PARSE",
     "confirm the pre-race artifact contains no post-race content"),
    ("raw_artifact_retained", "REQUIRES_OPERATOR_ATTESTATION",
     "operator affirms the immutable raw bytes are retained"),
    ("parser_version", "REQUIRES_PARSE",
     "a deterministic parser/extraction version has not been run against this artifact"),
    ("outcome_provenance_status", "REQUIRES_OPERATOR",
     "declare result provenance PROVEN or OPERATOR_ATTESTED"),
)

_OPERATOR_PLACEHOLDER = "__OPERATOR_REQUIRED__"


class ForwardCapturePrefillError(ValueError):
    """Raised for missing/invalid inputs or a card/result identity mismatch."""


@dataclasses.dataclass(frozen=True)
class Gap:
    field: str
    status: str
    reason: str


@dataclasses.dataclass(frozen=True)
class PrefillResult:
    candidate_key: str
    draft_status: str
    prefill_version: str
    prefilled: dict[str, Any]
    gaps: tuple[Gap, ...]
    sources: dict[str, Any]


# --------------------------------------------------------------------------- #
# Input loading                                                                #
# --------------------------------------------------------------------------- #
def load_result_row(source: Any) -> dict:
    """Accept a mapping, a ``{"rows": [...]}`` wrapper, a list, or a JSON path."""
    if isinstance(source, (str, Path)) and not isinstance(source, dict):
        path = Path(source)
        if not path.is_file():
            raise ForwardCapturePrefillError(f"result-row JSON not found: {source}")
        try:
            source = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ForwardCapturePrefillError(f"result-row JSON is not valid JSON: {exc}") from exc

    if isinstance(source, dict) and "rows" in source and isinstance(source["rows"], list):
        rows = source["rows"]
    elif isinstance(source, list):
        rows = source
    elif isinstance(source, dict):
        rows = [source]
    else:
        raise ForwardCapturePrefillError("result row must be a mapping, list, or JSON path")

    candidates = [r for r in rows if isinstance(r, dict) and (r.get("candidate_key") or "").strip()]
    if not candidates:
        raise ForwardCapturePrefillError("no result row with a candidate_key was supplied")
    keys = {r["candidate_key"].strip() for r in candidates}
    if len(keys) != 1:
        raise ForwardCapturePrefillError(f"ambiguous result rows span multiple candidate keys: {sorted(keys)}")
    return candidates[0]


# --------------------------------------------------------------------------- #
# Core prefill                                                                 #
# --------------------------------------------------------------------------- #
def _dk_card_identity(dk_card_path: str | Path) -> Any:
    path = Path(dk_card_path)
    if not path.is_file():
        raise ForwardCapturePrefillError(f"DK pre-race card not found: {dk_card_path}")
    artifact = _inventory_one(path, path.parent)
    if artifact.classification != CLASS_PRE_RACE or not (artifact.provider_candidate or "").startswith("draftkings"):
        raise ForwardCapturePrefillError(
            f"file is not a recognizable DraftKings pre-race card: {path.name} "
            f"(classification={artifact.classification}, provider={artifact.provider_candidate})"
        )
    if artifact.candidate_key is None:
        raise ForwardCapturePrefillError(
            f"DK card has insufficient identity (need track+date+race number): {path.name}"
        )
    return artifact


def _result_identity(row: dict) -> dict:
    key = (row.get("candidate_key") or "").strip()
    match = _KEY_RE.match(key)
    if not match:
        raise ForwardCapturePrefillError(f"result row candidate_key is malformed: {key!r}")
    result_path = str(row.get("source_file_path") or "").strip()
    if not result_path:
        raise ForwardCapturePrefillError("result row is missing source_file_path")
    declared_sha = str(row.get("source_file_sha256") or "").strip().lower()
    if not _SHA256_RE.match(declared_sha):
        raise ForwardCapturePrefillError("result row is missing a valid source_file_sha256")

    local = Path(result_path)
    locally_verified = False
    if local.is_file():
        actual = hashlib.sha256(local.read_bytes()).hexdigest()
        if actual != declared_sha:
            raise ForwardCapturePrefillError(
                f"result artifact SHA-256 mismatch for {local.name}: row={declared_sha} local={actual}"
            )
        locally_verified = True

    return {
        "candidate_key": key,
        "track": match.group("track").upper(),
        "race_date": match.group("date"),
        "race_number": int(match.group("race")),
        "path": local.resolve().as_posix() if local.is_absolute() or local.exists() else result_path,
        "sha256": declared_sha,
        "locally_verified": locally_verified,
        "official_status_detected": str(row.get("official_status_detected", "")).lower() == "true",
        "winner_detected": str(row.get("winner_detected", "")).lower() == "true",
        "uncertainty_code": str(row.get("uncertainty_code") or ""),
    }


def prefill_forward_capture(dk_card_path: str | Path, result_row: Any) -> PrefillResult:
    """Build a ``DRAFT_NOT_ELIGIBLE`` prefill worksheet for one future race pair."""
    if isinstance(result_row, (str, Path)) and not isinstance(result_row, dict):
        row = load_result_row(result_row)
    elif isinstance(result_row, (dict, list)):
        row = load_result_row(result_row)
    else:
        raise ForwardCapturePrefillError("result row must be a mapping, list, or JSON path")

    card = _dk_card_identity(dk_card_path)
    result = _result_identity(row)

    if card.candidate_key != result["candidate_key"]:
        raise ForwardCapturePrefillError(
            f"candidate_key mismatch: DK card {card.candidate_key!r} != result {result['candidate_key']!r}"
        )

    provider = card.provider_candidate or "draftkings"
    prefilled: dict[str, Any] = {
        "candidate_key": card.candidate_key,
        "pre_race_artifact_path_or_uri": card.path,
        "pre_race_sha256": card.sha256,
        "result_artifact_path_or_uri": result["path"],
        "result_sha256": result["sha256"],
        "result_sha256_locally_verified": result["locally_verified"],
        "source_provider": provider,
        "target_track_code": card.track_code,
        "target_track_name": card.track_name,
        "target_race_date": card.race_date,
        "target_race_date_provenance": "FILENAME_DERIVED",
        "target_race_number": card.race_number,
        "card_scheduled_post_wall_clock": card.scheduled_post_time_raw,
        "card_scheduled_post_status": card.scheduled_post_time_status,
        "outcome_reference": {
            "path_or_uri": result["path"],
            "sha256": result["sha256"],
            "source_provider": "equibase_results_pdf",
            "parser_version": _OPERATOR_PLACEHOLDER,
        },
        "result_official_status_detected": result["official_status_detected"],
        "result_winner_detected": result["winner_detected"],
    }

    gaps = [Gap(field, status, reason) for field, status, reason in _OPERATOR_GAPS]
    if not result["official_status_detected"]:
        gaps.append(Gap("outcome_reference.official_status", "REQUIRES_OPERATOR",
                        "result slice did not detect official status; confirm the race went official"))
    if not result["winner_detected"]:
        gaps.append(Gap("outcome_reference.winner", "REQUIRES_OPERATOR",
                        "result slice did not detect a winner; confirm the official result"))

    sources = {
        "dk_card": {
            "path": card.path,
            "sha256": card.sha256,
            "provider_candidate": card.provider_candidate,
            "filename_date_status": card.filename_date_status,
            "header_signature": card.header_signature,
        },
        "result_row": {
            "candidate_key": result["candidate_key"],
            "path": result["path"],
            "sha256": result["sha256"],
            "locally_verified": result["locally_verified"],
            "uncertainty_code": result["uncertainty_code"],
        },
    }

    return PrefillResult(
        candidate_key=card.candidate_key,
        draft_status=DRAFT_LABEL,
        prefill_version=PREFILL_VERSION,
        prefilled=prefilled,
        gaps=tuple(gaps),
        sources=sources,
    )


# --------------------------------------------------------------------------- #
# Contract-manifest draft (guaranteed non-passing)                             #
# --------------------------------------------------------------------------- #
def to_contract_manifest_draft(result: PrefillResult) -> dict[str, Any]:
    """Map the prefill onto the contract manifest shape with operator placeholders.

    Prefilled identity values are carried through; every operator gap becomes an
    ``__OPERATOR_REQUIRED__`` placeholder (or a deliberately non-passing default),
    so the result can never pass ``historical_snapshot_contract_audit``.
    """
    pf = result.prefilled
    return {
        "draft_status": DRAFT_LABEL,
        "artifact_path_or_uri": pf["pre_race_artifact_path_or_uri"],
        "sha256": pf["pre_race_sha256"],
        "source_provider": pf["source_provider"],
        "parser_version": _OPERATOR_PLACEHOLDER,
        "source_as_of_timestamp": _OPERATOR_PLACEHOLDER,
        "source_as_of_tier": _OPERATOR_PLACEHOLDER,
        "source_as_of_provenance": _OPERATOR_PLACEHOLDER,
        "target_track_code": pf["target_track_code"] or _OPERATOR_PLACEHOLDER,
        "target_race_date": pf["target_race_date"] or _OPERATOR_PLACEHOLDER,
        "target_race_number": pf["target_race_number"],
        "target_scheduled_post_timestamp": _OPERATOR_PLACEHOLDER,
        "target_surface": _OPERATOR_PLACEHOLDER,
        "target_distance_furlongs": 0,
        "expected_active_starter_count": 0,
        "observed_active_starter_count": 0,
        "identity_reconciliation_status": _OPERATOR_PLACEHOLDER,
        "field_completeness_status": _OPERATOR_PLACEHOLDER,
        "feature_vector_status": _OPERATOR_PLACEHOLDER,
        "raw_artifact_retained": False,
        "pre_race_fields_only": False,
        "outcome_reference": dict(pf["outcome_reference"]),
        "outcome_provenance_status": _OPERATOR_PLACEHOLDER,
    }


# --------------------------------------------------------------------------- #
# Artifacts                                                                    #
# --------------------------------------------------------------------------- #
def _slug(candidate_key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", candidate_key).strip("_") or "candidate"


def _prefill_document(result: PrefillResult) -> dict[str, Any]:
    return {
        "draft_status": result.draft_status,
        "prefill_version": result.prefill_version,
        "candidate_key": result.candidate_key,
        "prefilled": result.prefilled,
        "operator_required": [
            {"field": g.field, "status": g.status, "reason": g.reason} for g in result.gaps
        ],
        "sources": result.sources,
        "contract_manifest_draft": to_contract_manifest_draft(result),
        "note": (
            "Reconnaissance scaffold only. Not ingested, not scored, not training "
            "eligible. Fill every operator_required field and run "
            "scripts/audit_historical_snapshot_contract.py before any ingestion."
        ),
    }


def _gaps_document(result: PrefillResult) -> dict[str, Any]:
    return {
        "draft_status": result.draft_status,
        "candidate_key": result.candidate_key,
        "gap_count": len(result.gaps),
        "gaps": [
            {"field": g.field, "status": g.status, "reason": g.reason} for g in result.gaps
        ],
        "note": "Contract cannot pass until every gap is resolved with operator-supplied evidence.",
    }


def write_prefill_artifacts(result: PrefillResult, output_dir: str | Path) -> dict[str, str]:
    """Write the deterministic prefill + gaps JSON artifacts under ``output_dir``."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    slug = _slug(result.candidate_key)
    prefill_json = out / f"forward_capture_manifest_prefill_{slug}.json"
    gaps_json = out / f"forward_capture_manifest_prefill_{slug}_gaps.json"

    prefill_json.write_text(
        json.dumps(_prefill_document(result), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    gaps_json.write_text(
        json.dumps(_gaps_document(result), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "prefill_json": prefill_json.resolve().as_posix(),
        "gaps_json": gaps_json.resolve().as_posix(),
    }
