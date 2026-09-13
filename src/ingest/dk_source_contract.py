"""Source-contract acquisition rule for DraftKings Markdown race cards.

This module defines the canonical eligibility rule, the SOURCE_CONTRACT_INCOMPLETE
label, and the manifest schema that must accompany any card before it can advance
to scoring readiness.  It does not perform parsing, persistence, or scoring.

Rule
----
A DK Markdown card is score-candidate eligible only when every active runner has:

  - horse identity
  - post position
  - morning line
  - jockey
  - trainer
  - assigned weight   (parser field: entry.weight; contract boundary name: assigned_weight)
  - target surface    (race.surface)
  - target distance   (race.distance; normalised: race.normalized_distance_furlongs = distance_furlongs)
  - target race date  (race.race_date)
  - target track      (race.track; contract boundary name: track_code)

If assigned weight is absent for one or more active runners:
  - retain and audit the card
  - label it SOURCE_CONTRACT_INCOMPLETE
  - do not persist it to a live scoring path
  - do not construct a score artifact
  - do not produce probability, fair odds, market comparison, or wager tag

Acquisition instruction
-----------------------
Capture a source view that includes the program / weight column before post time,
or attach a second same-race pre-post source artifact containing assigned weight
and reconcile it by runner identity.  Do not derive weight from a later result
chart or post-race artifact.

Parser-to-contract field mapping
---------------------------------
The parser uses short generic names on Entry and Race.  The contract boundary
uses unambiguous names.  The mapping is documented here and applied in
``_contract_runner_fields`` and ``_contract_race_fields``:

  Entry.weight                        -> assigned_weight
  Race.track                          -> track_code
  Race.normalized_distance_furlongs   -> distance_furlongs
  Race.distance                       -> raw_distance (retained for display only)
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.ingest.draftkings_basic_csv import (
    DraftKingsBasicCSV,
    normalize_program_number,
    parse_draftkings_basic_csv,
)


# ---------------------------------------------------------------------------
# Contract label constants
# ---------------------------------------------------------------------------

SOURCE_CONTRACT_INCOMPLETE = "SOURCE_CONTRACT_INCOMPLETE"
"""Label applied to any card that fails the acquisition eligibility rule."""

SOURCE_CONTRACT_COMPLETE = "SOURCE_CONTRACT_COMPLETE"
"""Label applied to a card that satisfies all acquisition eligibility fields."""

# Weight-specific acquisition constant referenced by audit tests and decision gate.
ACQUISITION_REQUIREMENT_WEIGHT = "ASSIGNED_WEIGHT_BY_DK_LAYOUT"
"""Canonical name for the weight source-contract requirement."""

_VALID_PROVIDERS = frozenset({"draftkings_markdown"})
_VALID_TIERS = frozenset({"OPERATOR_ATTESTED"})
_VALID_FIELD_STATUSES = frozenset({"pre_race"})
_VALID_WEIGHT_STATUSES = frozenset({
    "PRESENT_FOR_ALL_ACTIVE_RUNNERS",
    "ABSENT_FOR_ONE_OR_MORE_ACTIVE_RUNNERS",
})
_HEX_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TZ_OFFSET_RE = re.compile(r"([+-]\d{2}:\d{2}|Z)$")


# ---------------------------------------------------------------------------
# Parser-to-contract field mapping
# ---------------------------------------------------------------------------

# Runner fields: contract_name -> parser attribute on Entry
_RUNNER_CONTRACT_MAP: tuple[tuple[str, str], ...] = (
    ("horse_name",    "horse_name"),
    ("post_position", "post_position"),
    ("morning_line",  "morning_line"),
    ("jockey",        "jockey"),
    ("trainer",       "trainer"),
    ("assigned_weight", "weight"),          # contract name != parser name
)

# Race fields: contract_name -> parser attribute on Race
_RACE_CONTRACT_MAP: tuple[tuple[str, str], ...] = (
    ("surface",          "surface"),
    ("distance_furlongs", "normalized_distance_furlongs"),  # contract name != parser name
    ("race_date",        "race_date"),
    ("track_code",       "track"),           # contract name != parser name
)

RUNNER_REQUIRED_FIELDS: tuple[str, ...] = tuple(c for c, _ in _RUNNER_CONTRACT_MAP)
"""Contract-boundary names of entry-level fields required for every active runner."""

RACE_REQUIRED_FIELDS: tuple[str, ...] = tuple(c for c, _ in _RACE_CONTRACT_MAP)
"""Contract-boundary names of race-level fields required for card eligibility."""


def _runner_missing(entry: Any) -> list[str]:
    """Return contract field names missing on an active runner."""
    return [
        contract_name
        for contract_name, parser_attr in _RUNNER_CONTRACT_MAP
        if not getattr(entry, parser_attr, None)
    ]


def _race_missing(race: Any) -> list[str]:
    """Return contract field names missing on the race."""
    return [
        contract_name
        for contract_name, parser_attr in _RACE_CONTRACT_MAP
        if not getattr(race, parser_attr, None)
    ]


# ---------------------------------------------------------------------------
# Acquisition rule evaluation  (deterministic — no wall-clock side effects)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class AcquisitionRuleResult:
    """Outcome of evaluating the source-contract acquisition rule on one card.

    This dataclass is deterministic: all fields are derived solely from the
    card content.  There is no wall-clock timestamp; call sites that need an
    audit trail must inject ``evaluated_at`` explicitly after construction.
    """
    source_path: str
    race_identifier: str
    contract_label: str           # SOURCE_CONTRACT_COMPLETE | SOURCE_CONTRACT_INCOMPLETE
    score_candidate_eligible: bool
    active_runner_count: int
    runners_missing_assigned_weight: int   # contract name: assigned_weight
    runners_missing_any_required: int
    race_fields_missing: list[str]         # contract names
    runner_field_gaps: list[dict[str, Any]]  # [{"runner": str, "missing": [contract_name, ...]}
    acquisition_requirements: list[str]    # canonical requirement names blocking eligibility

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def merge_draftkings_basic_weights(
    card: Any,
    basic_tab_csv: DraftKingsBasicCSV | str | bytes | Path,
) -> int:
    """Fill only absent entry weights from an explicit DK Basic-tab CSV source.

    Matching is exact on DraftKings program number (the ``#`` column).  This
    deliberately does not reconcile by runner name or modify any field other
    than an absent ``entry.weight``.  A partial or non-matching Basic export
    simply leaves the remaining contract gaps in place for evaluation.
    """
    overlay = (
        basic_tab_csv
        if isinstance(basic_tab_csv, DraftKingsBasicCSV)
        else parse_draftkings_basic_csv(basic_tab_csv)
    )
    weights = overlay.weights_by_program_number
    merged = 0
    for entry in card.entries:
        if getattr(entry, "is_scratched", False) or getattr(entry, "weight", None) is not None:
            continue
        program_key = normalize_program_number(getattr(entry, "program_number", None))
        weight = weights.get(program_key) if program_key else None
        if weight is not None:
            entry.weight = weight
            merged += 1
    return merged


def _active_runners_missing_weight(card: Any) -> bool:
    return any(
        not getattr(entry, "is_scratched", False) and getattr(entry, "weight", None) is None
        for entry in card.entries
    )


def evaluate_acquisition_rule(
    card: Any,
    *,
    basic_tab_csv: DraftKingsBasicCSV | str | bytes | Path | None = None,
) -> AcquisitionRuleResult:
    """Evaluate the acquisition eligibility rule against a parsed card.

    Deterministic: produces identical output for identical card content across
    repeated calls.  No wall-clock timestamp is captured here.

    Parameters
    ----------
    card:
        A ``DraftKingsMarkdownCard`` instance (imported at call site to avoid
        circular dependencies).
    basic_tab_csv:
        An explicit same-race DraftKings Basic-tab CSV export (or an already
        parsed :class:`DraftKingsBasicCSV`).  It is consulted only if active
        runners are missing assigned weight, and may populate only that field.
    """
    if basic_tab_csv is not None and _active_runners_missing_weight(card):
        merge_draftkings_basic_weights(card, basic_tab_csv)

    race = card.race
    missing_race: list[str] = _race_missing(race)

    runner_gaps: list[dict[str, Any]] = []
    runners_missing_weight = 0
    active_count = 0

    for entry in card.entries:
        if getattr(entry, "is_scratched", False):
            continue
        active_count += 1
        missing = _runner_missing(entry)
        if missing:
            runner_gaps.append({
                "runner": entry.horse_name or entry.program_number or "unknown",
                "post_position": entry.post_position,
                "missing": missing,
            })
        if getattr(entry, "weight", None) is None:   # parser attr; contract name: assigned_weight
            runners_missing_weight += 1

    runners_missing_any = len(runner_gaps)
    acquisition_requirements: list[str] = []
    if runners_missing_weight > 0:
        acquisition_requirements.append(ACQUISITION_REQUIREMENT_WEIGHT)

    eligible = (
        not missing_race
        and runners_missing_any == 0
        and active_count > 0
    )
    label = SOURCE_CONTRACT_COMPLETE if eligible else SOURCE_CONTRACT_INCOMPLETE

    race_id = (
        f"{race.track or '?'}-"
        f"{race.race_date.isoformat() if race.race_date else '?'}-"
        f"R{race.race_number or '?'}"
    )

    return AcquisitionRuleResult(
        source_path=card.source_path,
        race_identifier=race_id,
        contract_label=label,
        score_candidate_eligible=eligible,
        active_runner_count=active_count,
        runners_missing_assigned_weight=runners_missing_weight,
        runners_missing_any_required=runners_missing_any,
        race_fields_missing=missing_race,
        runner_field_gaps=runner_gaps,
        acquisition_requirements=acquisition_requirements,
    )


# ---------------------------------------------------------------------------
# Source manifest schema
# ---------------------------------------------------------------------------

FIELD_STATUS_PRE_RACE = "pre_race"
SOURCE_TIER_OPERATOR_ATTESTED = "OPERATOR_ATTESTED"
SOURCE_PROVIDER_DK_MARKDOWN = "draftkings_markdown"
WEIGHT_STATUS_PRESENT = "PRESENT_FOR_ALL_ACTIVE_RUNNERS"
WEIGHT_STATUS_ABSENT = "ABSENT_FOR_ONE_OR_MORE_ACTIVE_RUNNERS"


@dataclasses.dataclass
class SourceManifest:
    """Pre-race source manifest that accompanies a DK Markdown card file.

    Minimum artifact set required before a card can advance to scoring readiness:

        {TRACK}_DK_Horse_R{n}_{M-D-YY}.md
        {TRACK}_R{n}_{YYYY-MM-DD}_source_manifest.json

    All timestamps must be ISO 8601 with explicit UTC or local offset (+HH:MM / Z).
    Naive timestamps (no offset) are rejected by validate_manifest_schema.
    """
    track_code: str
    race_number: int
    race_date: str                   # YYYY-MM-DD
    scheduled_post_timestamp: str    # ISO 8601 with offset
    source_as_of_timestamp: str      # ISO 8601 with offset; must be < scheduled_post_timestamp
    source_provider: str             # draftkings_markdown
    source_tier: str                 # OPERATOR_ATTESTED
    raw_file_name: str
    raw_file_sha256: str             # 64-char lowercase hex
    field_status: str                # pre_race
    assigned_weight_status: str      # PRESENT_FOR_ALL_ACTIVE_RUNNERS | ABSENT_FOR_ONE_OR_MORE_ACTIVE_RUNNERS

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceManifest":
        return cls(**{k: v for k, v in data.items() if k in {f.name for f in dataclasses.fields(cls)}})

    @classmethod
    def from_json(cls, text: str) -> "SourceManifest":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_file(cls, path: str | Path) -> "SourceManifest":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Internal timestamp parser
# ---------------------------------------------------------------------------

def _parse_iso_with_offset(value: str, field_name: str) -> tuple[datetime | None, list[str]]:
    """Parse an ISO 8601 string that must carry an explicit UTC/local offset.

    Returns (parsed_datetime, errors).  Errors is non-empty on any failure.
    """
    if not value:
        return None, [f"{field_name} is required"]
    if not _TZ_OFFSET_RE.search(value):
        return None, [f"{field_name} must include a timezone offset (e.g. +00:00 or Z); got {value!r}"]
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return None, [f"{field_name} parsed as naive datetime despite offset pattern; got {value!r}"]
        return parsed, []
    except ValueError as exc:
        return None, [f"{field_name} is not a valid ISO 8601 timestamp: {exc}"]


# ---------------------------------------------------------------------------
# 1. Schema validation (structure only, no card content)
# ---------------------------------------------------------------------------

def validate_manifest_schema(manifest: SourceManifest) -> list[str]:
    """Validate manifest structure and value constraints independent of any card.

    Checks: required fields, enum membership, SHA-256 format, date format,
    timestamp format, timezone offset presence, source_as_of < scheduled_post.

    Returns a list of error strings; empty list = schema is well-formed.
    """
    errors: list[str] = []

    # track_code
    if not manifest.track_code:
        errors.append("track_code is required")

    # race_number
    if manifest.race_number < 1:
        errors.append(f"race_number must be >= 1; got {manifest.race_number}")

    # race_date
    if not manifest.race_date or not _DATE_RE.match(manifest.race_date):
        errors.append(f"race_date must be YYYY-MM-DD; got {manifest.race_date!r}")
    else:
        try:
            date.fromisoformat(manifest.race_date)
        except ValueError:
            errors.append(f"race_date is not a valid calendar date: {manifest.race_date!r}")

    # scheduled_post_timestamp
    post_dt, post_errors = _parse_iso_with_offset(manifest.scheduled_post_timestamp, "scheduled_post_timestamp")
    errors.extend(post_errors)

    # source_as_of_timestamp
    as_of_dt, as_of_errors = _parse_iso_with_offset(manifest.source_as_of_timestamp, "source_as_of_timestamp")
    errors.extend(as_of_errors)

    # source_as_of must be strictly before scheduled_post
    if post_dt is not None and as_of_dt is not None:
        if as_of_dt >= post_dt:
            errors.append(
                f"source_as_of_timestamp ({manifest.source_as_of_timestamp}) must be "
                f"strictly before scheduled_post_timestamp ({manifest.scheduled_post_timestamp})"
            )

    # source_provider
    if manifest.source_provider not in _VALID_PROVIDERS:
        errors.append(
            f"source_provider must be one of {sorted(_VALID_PROVIDERS)}; "
            f"got {manifest.source_provider!r}"
        )

    # source_tier
    if manifest.source_tier not in _VALID_TIERS:
        errors.append(
            f"source_tier must be one of {sorted(_VALID_TIERS)}; "
            f"got {manifest.source_tier!r}"
        )

    # raw_file_name
    if not manifest.raw_file_name:
        errors.append("raw_file_name is required")

    # raw_file_sha256 — must be exactly 64 hex characters
    sha = manifest.raw_file_sha256 or ""
    if not _HEX_RE.match(sha):
        errors.append(
            f"raw_file_sha256 must be exactly 64 lowercase hex characters; got {sha!r}"
        )

    # field_status
    if manifest.field_status not in _VALID_FIELD_STATUSES:
        errors.append(
            f"field_status must be one of {sorted(_VALID_FIELD_STATUSES)}; "
            f"got {manifest.field_status!r}"
        )

    # assigned_weight_status
    if manifest.assigned_weight_status not in _VALID_WEIGHT_STATUSES:
        errors.append(
            f"assigned_weight_status must be one of {sorted(_VALID_WEIGHT_STATUSES)}; "
            f"got {manifest.assigned_weight_status!r}"
        )

    return errors


# ---------------------------------------------------------------------------
# 2. Evidence validation against parsed card
# ---------------------------------------------------------------------------

def validate_manifest_against_card(
    manifest: SourceManifest,
    card: Any,
) -> list[str]:
    """Confirm that manifest identity and weight status agree with the parsed card.

    Does not perform schema validation; call validate_manifest_schema first.

    Checks:
    - track_code matches card.race.track (first 4 chars, upper)
    - race_number matches card.race.race_number
    - race_date matches card.race.race_date
    - assigned_weight_status agrees with actual active runner weight counts
    - raw_file_sha256 matches card.source_sha256
    """
    errors: list[str] = []
    race = card.race

    # track_code
    card_track = ((race.track or "").strip()[:4].upper()) or None
    manifest_track = (manifest.track_code or "").strip().upper()
    if card_track and manifest_track and card_track != manifest_track:
        errors.append(
            f"manifest track_code {manifest_track!r} does not match "
            f"parsed card track {card_track!r}"
        )

    # race_number
    if race.race_number is not None and manifest.race_number != race.race_number:
        errors.append(
            f"manifest race_number {manifest.race_number} does not match "
            f"parsed card race_number {race.race_number}"
        )

    # race_date
    if race.race_date is not None:
        card_date_str = race.race_date.isoformat()
        if manifest.race_date != card_date_str:
            errors.append(
                f"manifest race_date {manifest.race_date!r} does not match "
                f"parsed card race_date {card_date_str!r}"
            )

    # raw_file_sha256
    if manifest.raw_file_sha256 and card.source_sha256:
        if manifest.raw_file_sha256.lower() != card.source_sha256.lower():
            errors.append(
                f"manifest raw_file_sha256 does not match card source_sha256"
            )

    # assigned_weight_status vs actual runners
    active_missing_weight = sum(
        1 for e in card.entries
        if not getattr(e, "is_scratched", False) and getattr(e, "weight", None) is None
    )
    actual_status = (
        WEIGHT_STATUS_PRESENT if active_missing_weight == 0 else WEIGHT_STATUS_ABSENT
    )
    if manifest.assigned_weight_status != actual_status:
        errors.append(
            f"manifest assigned_weight_status {manifest.assigned_weight_status!r} "
            f"does not agree with parsed card "
            f"({active_missing_weight} active runner(s) missing assigned_weight; "
            f"expected {actual_status!r})"
        )

    return errors


# ---------------------------------------------------------------------------
# 3. Evidence validation against raw source file
# ---------------------------------------------------------------------------

def validate_manifest_against_raw_file(
    manifest: SourceManifest,
    raw_path: str | Path,
) -> list[str]:
    """Confirm that manifest raw_file_sha256 matches the bytes at raw_path.

    Does not perform schema validation or card parsing.
    """
    errors: list[str] = []
    path = Path(raw_path)
    if not path.exists():
        errors.append(f"raw_path does not exist: {path}")
        return errors
    actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if manifest.raw_file_sha256.lower() != actual_sha.lower():
        errors.append(
            f"manifest raw_file_sha256 {manifest.raw_file_sha256!r} does not match "
            f"actual file SHA-256 {actual_sha!r} at {path}"
        )
    return errors


# ---------------------------------------------------------------------------
# Backwards-compatible alias (keep existing callers working)
# ---------------------------------------------------------------------------

def validate_manifest(manifest: SourceManifest) -> list[str]:
    """Alias for validate_manifest_schema; retained for backwards compatibility.

    New call sites should use validate_manifest_schema directly.
    """
    return validate_manifest_schema(manifest)


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------

def build_manifest_from_card(
    card: Any,
    *,
    scheduled_post_timestamp: str,
    source_as_of_timestamp: str,
) -> SourceManifest:
    """Build a SourceManifest from a parsed DraftKingsMarkdownCard.

    ``source_as_of_timestamp`` is required and must be an ISO 8601 string with
    an explicit UTC or local offset.  ``card.as_of`` is NOT used as a fallback
    because it may be a parser default or filename-derived value, neither of
    which constitutes operator-attested provenance.

    Parameters
    ----------
    card:
        Parsed ``DraftKingsMarkdownCard``.
    scheduled_post_timestamp:
        ISO 8601 string with UTC offset for the scheduled post time.
    source_as_of_timestamp:
        ISO 8601 string with UTC offset for when the source was captured.
        Required.  Must be strictly before ``scheduled_post_timestamp``.
    """
    race = card.race
    result = evaluate_acquisition_rule(card)
    raw_name = Path(card.source_path).name

    weight_status = (
        WEIGHT_STATUS_PRESENT
        if result.runners_missing_assigned_weight == 0
        else WEIGHT_STATUS_ABSENT
    )

    return SourceManifest(
        track_code=(race.track or "UNKNOWN").strip()[:4].upper(),
        race_number=race.race_number or 0,
        race_date=race.race_date.isoformat() if race.race_date else "UNKNOWN",
        scheduled_post_timestamp=scheduled_post_timestamp,
        source_as_of_timestamp=source_as_of_timestamp,
        source_provider=SOURCE_PROVIDER_DK_MARKDOWN,
        source_tier=SOURCE_TIER_OPERATOR_ATTESTED,
        raw_file_name=raw_name,
        raw_file_sha256=card.source_sha256,
        field_status=FIELD_STATUS_PRE_RACE,
        assigned_weight_status=weight_status,
    )


# ---------------------------------------------------------------------------
# SHA-256 convenience
# ---------------------------------------------------------------------------

def sha256_of_file(path: str | Path) -> str:
    """Return lowercase hex SHA-256 of a file, for manifest population."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
