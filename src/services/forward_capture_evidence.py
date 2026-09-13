"""Read-only forward-capture evidence ledger.

Operators use this to record **immutable capture facts** for future paired races
*before* any manifest can become eligible:

* one :class:`ForwardCaptureEvidenceEntry` per artifact event
  (``PRE_RACE_CAPTURE`` / ``RESULT_CAPTURE`` / ``MANUAL_NOTE``);
* :class:`ForwardCaptureRaceBundleSummary` groups entries by ``candidate_key`` and
  reports what is still unresolved.

It is an audit aid only.  It never opens SQLite, never imports
ingestion / readiness / training / scoring / promotion modules, and cannot by
itself satisfy the historical snapshot contract.  Every record carries
``draft_status = DRAFT_NOT_ELIGIBLE`` and ``created_from = EVIDENCE_LEDGER_ONLY``.
Deterministic file identity is streamed read-only; operator as-of evidence is
never inferred from a filename or filesystem timestamp.
"""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.services.forward_capture_prefill import DRAFT_LABEL
from src.services.historical_source_recon import (
    CLASS_PRE_RACE,
    CLASS_RESULT,
    _inventory_one,
    _sha256,
)

CREATED_FROM = "EVIDENCE_LEDGER_ONLY"
EVIDENCE_VERSION = "1.0"

EVENT_TYPES = ("PRE_RACE_CAPTURE", "RESULT_CAPTURE", "MANUAL_NOTE")
_CAPTURE_EVENTS = frozenset({"PRE_RACE_CAPTURE", "RESULT_CAPTURE"})

_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_KEY_RE = re.compile(r"^(?P<track>[A-Za-z0-9_-]{2,12})\|(?P<date>\d{4}-\d{2}-\d{2})\|R(?P<race>\d+)$")

# Operator-supplied fields that a capture event needs for later contract work.
_CAPTURE_AS_OF_FIELDS = (
    "source_as_of_timestamp",
    "source_as_of_timezone",
    "source_as_of_tier",
    "source_as_of_provenance",
    "operator_id",
    "raw_bytes_preserved",
)

_ENTRY_FIELDS = (
    "event_type", "candidate_key", "artifact_path", "artifact_sha256",
    "source_provider_candidate", "target_track_code_or_name", "target_race_date",
    "target_race_number", "observed_wall_clock_post_display",
    "source_as_of_timestamp", "source_as_of_timezone", "source_as_of_tier",
    "source_as_of_provenance", "operator_id", "note", "raw_bytes_preserved",
)


class ForwardCaptureEvidenceError(ValueError):
    """Raised for a malformed entry or an identity conflict."""


@dataclasses.dataclass(frozen=True)
class ForwardCaptureEvidenceEntry:
    event_type: str
    draft_status: str
    candidate_key: str | None
    artifact_path: str | None
    artifact_sha256: str | None
    artifact_sha256_source: str
    source_provider_candidate: str | None
    target_track_code_or_name: str | None
    target_race_date: str | None
    target_race_date_provenance: str | None
    target_race_number: int | None
    observed_wall_clock_post_display: str | None
    source_as_of_timestamp: str | None
    source_as_of_timezone: str | None
    source_as_of_tier: str | None
    source_as_of_provenance: str | None
    operator_id: str | None
    note: str | None
    raw_bytes_preserved: bool | None
    created_from: str
    unresolved_fields: tuple[str, ...]
    enrichment: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["unresolved_fields"] = list(self.unresolved_fields)
        return payload


@dataclasses.dataclass(frozen=True)
class ForwardCaptureRaceBundleSummary:
    candidate_key: str | None
    pre_race_capture_present: bool
    result_capture_present: bool
    artifact_count: int
    entry_count: int
    unresolved_fields: tuple[str, ...]
    status: str

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["unresolved_fields"] = list(self.unresolved_fields)
        return payload


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("__") and text.endswith("__"):
        return None
    return text


def _clean_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clean_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0"):
        return False
    return None


def _validate_key(key: str | None) -> str | None:
    if key is None:
        return None
    if not _KEY_RE.match(key):
        raise ForwardCaptureEvidenceError(f"candidate_key is malformed (want TRACK|YYYY-MM-DD|Rn): {key!r}")
    return key


def _enrich_from_artifact(path: Path, event_type: str) -> dict[str, Any]:
    """Deterministic identity + streamed SHA-256 from a local artifact, read-only."""
    enrichment: dict[str, Any] = {"artifact_exists": path.is_file()}
    if not path.is_file():
        return enrichment
    enrichment["artifact_sha256"] = _sha256(path)
    try:
        artifact = _inventory_one(path, path.parent)
    except Exception:  # pragma: no cover - defensive; recon is read-only
        return enrichment
    enrichment["classification"] = artifact.classification
    enrichment["header_signature"] = artifact.header_signature
    is_expected_kind = (
        (event_type == "PRE_RACE_CAPTURE" and artifact.classification == CLASS_PRE_RACE)
        or (event_type == "RESULT_CAPTURE" and artifact.classification == CLASS_RESULT)
        or event_type == "MANUAL_NOTE"
    )
    if not is_expected_kind:
        enrichment["identity_note"] = (
            f"artifact classified {artifact.classification}, not the expected kind for {event_type}"
        )
        return enrichment
    enrichment["source_provider_candidate"] = artifact.provider_candidate
    enrichment["target_track_code_or_name"] = artifact.track_code
    if artifact.race_date:
        enrichment["target_race_date"] = artifact.race_date
        enrichment["target_race_date_provenance"] = "FILENAME_DERIVED"
    enrichment["target_race_number"] = artifact.race_number
    enrichment["candidate_key"] = artifact.candidate_key
    if artifact.scheduled_post_time_raw:
        enrichment["observed_wall_clock_post_display"] = artifact.scheduled_post_time_raw
    return enrichment


def _reconcile(operator: Any, derived: Any, field: str) -> Any:
    if operator is not None and derived is not None and str(operator) != str(derived):
        raise ForwardCaptureEvidenceError(
            f"{field} conflict: operator gave {operator!r}, artifact identity is {derived!r}"
        )
    return operator if operator is not None else derived


# --------------------------------------------------------------------------- #
# Entry construction                                                           #
# --------------------------------------------------------------------------- #
def build_evidence_entry(raw: Any) -> ForwardCaptureEvidenceEntry:
    if not isinstance(raw, Mapping):
        raise ForwardCaptureEvidenceError("evidence entry must be a JSON object / mapping")

    event_type = _clean_str(raw.get("event_type"))
    if event_type not in EVENT_TYPES:
        raise ForwardCaptureEvidenceError(
            f"event_type must be one of {EVENT_TYPES}; got {raw.get('event_type')!r}"
        )

    note = _clean_str(raw.get("note"))
    artifact_path_raw = _clean_str(raw.get("artifact_path"))

    if event_type == "MANUAL_NOTE" and not note:
        raise ForwardCaptureEvidenceError("MANUAL_NOTE entries require a non-empty note")
    if event_type in _CAPTURE_EVENTS and not artifact_path_raw:
        raise ForwardCaptureEvidenceError(f"{event_type} entries require an artifact_path")

    operator_key = _validate_key(_clean_str(raw.get("candidate_key")))

    enrichment: dict[str, Any] = {}
    artifact_path_out: str | None = None
    if artifact_path_raw:
        path = Path(artifact_path_raw)
        artifact_path_out = path.resolve().as_posix() if path.exists() else artifact_path_raw
        enrichment = _enrich_from_artifact(path, event_type)

    derived_key = _validate_key(enrichment.get("candidate_key"))
    candidate_key = _reconcile(operator_key, derived_key, "candidate_key")

    track = _reconcile(
        _clean_str(raw.get("target_track_code_or_name")),
        enrichment.get("target_track_code_or_name"),
        "target_track_code_or_name",
    )
    race_date = _reconcile(
        _clean_str(raw.get("target_race_date")),
        enrichment.get("target_race_date"),
        "target_race_date",
    )
    race_number = _reconcile(
        _clean_int(raw.get("target_race_number")),
        enrichment.get("target_race_number"),
        "target_race_number",
    )

    # Cross-check any candidate_key against the resolved parts, then back-fill
    # the still-unknown identity parts from the (structured) candidate_key.
    if candidate_key:
        parts = _KEY_RE.match(candidate_key)
        if race_date and parts.group("date") != race_date:
            raise ForwardCaptureEvidenceError(
                f"candidate_key date {parts.group('date')} != target_race_date {race_date}"
            )
        if race_number is not None and int(parts.group("race")) != race_number:
            raise ForwardCaptureEvidenceError(
                f"candidate_key race {parts.group('race')} != target_race_number {race_number}"
            )
        if track and parts.group("track").upper() != str(track).upper():
            raise ForwardCaptureEvidenceError(
                f"candidate_key track {parts.group('track')} != target_track_code_or_name {track}"
            )
        track = track or parts.group("track").upper()
        race_date = race_date or parts.group("date")
        race_number = race_number if race_number is not None else int(parts.group("race"))

    # SHA-256: streamed local file wins; otherwise an operator-declared 64-hex.
    streamed = enrichment.get("artifact_sha256")
    operator_sha = _clean_str(raw.get("artifact_sha256"))
    if streamed:
        artifact_sha256, sha_source = streamed, "STREAMED_LOCAL_FILE"
    elif operator_sha and _SHA256_RE.match(operator_sha):
        artifact_sha256, sha_source = operator_sha.lower(), "OPERATOR_DECLARED"
    elif artifact_path_raw:
        artifact_sha256, sha_source = None, "UNRESOLVED_ARTIFACT_NOT_LOCAL"
    else:
        artifact_sha256, sha_source = None, "NOT_APPLICABLE"

    provider = _reconcile(
        _clean_str(raw.get("source_provider_candidate")),
        enrichment.get("source_provider_candidate"),
        "source_provider_candidate",
    )
    post_display = (
        _clean_str(raw.get("observed_wall_clock_post_display"))
        or enrichment.get("observed_wall_clock_post_display")
    )

    as_of_ts = _clean_str(raw.get("source_as_of_timestamp"))
    as_of_tz = _clean_str(raw.get("source_as_of_timezone"))
    as_of_tier = _clean_str(raw.get("source_as_of_tier"))
    as_of_prov = _clean_str(raw.get("source_as_of_provenance"))
    operator_id = _clean_str(raw.get("operator_id"))
    raw_preserved = _clean_bool(raw.get("raw_bytes_preserved"))

    unresolved: list[str] = []
    if candidate_key is None:
        unresolved.append("candidate_key")
    if event_type in _CAPTURE_EVENTS:
        if artifact_sha256 is None:
            unresolved.append("artifact_sha256")
        supplied = {
            "source_as_of_timestamp": as_of_ts,
            "source_as_of_timezone": as_of_tz,
            "source_as_of_tier": as_of_tier,
            "source_as_of_provenance": as_of_prov,
            "operator_id": operator_id,
            "raw_bytes_preserved": raw_preserved,
        }
        for field in _CAPTURE_AS_OF_FIELDS:
            value = supplied[field]
            if value is None or value is False and field == "raw_bytes_preserved":
                unresolved.append(field)
        if track is None:
            unresolved.append("target_track_code_or_name")
        if race_date is None:
            unresolved.append("target_race_date")
        if race_number is None:
            unresolved.append("target_race_number")
    elif not operator_id:
        unresolved.append("operator_id")

    return ForwardCaptureEvidenceEntry(
        event_type=event_type,
        draft_status=DRAFT_LABEL,
        candidate_key=candidate_key,
        artifact_path=artifact_path_out,
        artifact_sha256=artifact_sha256,
        artifact_sha256_source=sha_source,
        source_provider_candidate=provider,
        target_track_code_or_name=track,
        target_race_date=race_date,
        target_race_date_provenance=enrichment.get("target_race_date_provenance")
        if race_date and race_date == enrichment.get("target_race_date")
        else ("OPERATOR_DECLARED" if race_date else None),
        target_race_number=race_number,
        observed_wall_clock_post_display=post_display,
        source_as_of_timestamp=as_of_ts,
        source_as_of_timezone=as_of_tz,
        source_as_of_tier=as_of_tier,
        source_as_of_provenance=as_of_prov,
        operator_id=operator_id,
        note=note,
        raw_bytes_preserved=raw_preserved,
        created_from=CREATED_FROM,
        unresolved_fields=tuple(dict.fromkeys(unresolved)),
        enrichment=enrichment,
    )


# --------------------------------------------------------------------------- #
# Bundle summaries                                                             #
# --------------------------------------------------------------------------- #
def build_bundle_summaries(
    entries: Sequence[ForwardCaptureEvidenceEntry],
) -> list[ForwardCaptureRaceBundleSummary]:
    groups: dict[str | None, list[ForwardCaptureEvidenceEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.candidate_key, []).append(entry)

    summaries: list[ForwardCaptureRaceBundleSummary] = []
    for key in sorted(groups, key=lambda k: (k is None, k or "")):
        bucket = groups[key]
        pre_present = any(e.event_type == "PRE_RACE_CAPTURE" for e in bucket)
        result_present = any(e.event_type == "RESULT_CAPTURE" for e in bucket)
        unresolved: set[str] = set()
        for entry in bucket:
            unresolved.update(entry.unresolved_fields)
        if not pre_present:
            unresolved.add("pre_race_capture")
        if not result_present:
            unresolved.add("result_capture")
        summaries.append(
            ForwardCaptureRaceBundleSummary(
                candidate_key=key,
                pre_race_capture_present=pre_present,
                result_capture_present=result_present,
                artifact_count=sum(1 for e in bucket if e.artifact_path),
                entry_count=len(bucket),
                unresolved_fields=tuple(sorted(unresolved)),
                status=DRAFT_LABEL,
            )
        )
    return summaries


# --------------------------------------------------------------------------- #
# IO                                                                           #
# --------------------------------------------------------------------------- #
def load_entry_documents(sources: Iterable[str | Path | Mapping]) -> list[dict]:
    docs: list[dict] = []
    for source in sources:
        if isinstance(source, Mapping):
            docs.append(dict(source))
            continue
        path = Path(source)
        if not path.is_file():
            raise ForwardCaptureEvidenceError(f"evidence entry JSON not found: {source}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ForwardCaptureEvidenceError(f"evidence entry JSON is invalid: {path} ({exc})") from exc
        if isinstance(payload, list):
            docs.extend(item for item in payload)
        else:
            docs.append(payload)
    if not docs:
        raise ForwardCaptureEvidenceError("no evidence entries supplied")
    return docs


def _entry_sort_key(entry: ForwardCaptureEvidenceEntry) -> tuple:
    return (
        entry.candidate_key or "~~~",
        entry.event_type,
        entry.artifact_path or "",
        entry.artifact_sha256 or "",
        entry.note or "",
        entry.operator_id or "",
    )


def write_evidence_artifacts(
    entries: Sequence[ForwardCaptureEvidenceEntry],
    summaries: Sequence[ForwardCaptureRaceBundleSummary],
    output_dir: str | Path,
) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "forward_capture_evidence_log.json"
    summary_path = out / "forward_capture_evidence_bundle_summary.json"

    ordered_entries = sorted(entries, key=_entry_sort_key)
    ordered_summaries = sorted(
        summaries, key=lambda s: (s.candidate_key is None, s.candidate_key or "")
    )

    log_doc = {
        "status": DRAFT_LABEL,
        "created_from": CREATED_FROM,
        "evidence_version": EVIDENCE_VERSION,
        "entry_count": len(ordered_entries),
        "note": (
            "Immutable operator capture facts for future paired races. Not ingestion, "
            "not a manifest, not training eligible. A bundle may still be incomplete."
        ),
        "entries": [e.to_dict() for e in ordered_entries],
    }
    summary_doc = {
        "status": DRAFT_LABEL,
        "created_from": CREATED_FROM,
        "evidence_version": EVIDENCE_VERSION,
        "bundle_count": len(ordered_summaries),
        "incomplete_bundle_count": sum(
            1 for s in ordered_summaries
            if not (s.pre_race_capture_present and s.result_capture_present) or s.unresolved_fields
        ),
        "bundles": [s.to_dict() for s in ordered_summaries],
    }

    log_path.write_text(json.dumps(log_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "evidence_log": log_path.resolve().as_posix(),
        "bundle_summary": summary_path.resolve().as_posix(),
    }
