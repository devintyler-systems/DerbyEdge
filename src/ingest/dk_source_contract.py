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
  - assigned weight
  - target surface
  - target distance
  - target race date
  - target track identity

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
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


# ---------------------------------------------------------------------------
# Required identity fields per active runner
# ---------------------------------------------------------------------------

RUNNER_REQUIRED_FIELDS: tuple[str, ...] = (
    "horse_name",
    "post_position",
    "morning_line",
    "jockey",
    "trainer",
    "weight",
)
"""Entry-level fields that must be non-null for every active runner."""

RACE_REQUIRED_FIELDS: tuple[str, ...] = (
    "surface",
    "distance",
    "race_date",
    "track",
)
"""Race-level fields that must be non-null for the card to be eligible."""


# ---------------------------------------------------------------------------
# Acquisition rule evaluation
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class AcquisitionRuleResult:
    """Outcome of evaluating the source-contract acquisition rule on one card."""
    source_path: str
    race_identifier: str
    contract_label: str           # SOURCE_CONTRACT_COMPLETE | SOURCE_CONTRACT_INCOMPLETE
    score_candidate_eligible: bool
    active_runner_count: int
    runners_missing_weight: int
    runners_missing_any_required: int
    race_fields_missing: list[str]
    runner_field_gaps: list[dict[str, Any]]  # [{"runner": str, "missing": [str]}]
    acquisition_requirements: list[str]      # canonical requirement names blocking eligibility
    evaluated_at: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def evaluate_acquisition_rule(
    card: Any,  # DraftKingsMarkdownCard — typed as Any to avoid circular imports
) -> AcquisitionRuleResult:
    """Evaluate the acquisition eligibility rule against a parsed card.

    The card is accepted as-parsed; this function does not call the validator.
    It operates on the structural fields defined in RUNNER_REQUIRED_FIELDS and
    RACE_REQUIRED_FIELDS and returns an AcquisitionRuleResult regardless of
    validation state.

    Parameters
    ----------
    card:
        A ``DraftKingsMarkdownCard`` instance (imported at call site to avoid
        circular dependencies).

    Returns
    -------
    AcquisitionRuleResult
    """
    race = card.race
    race_missing: list[str] = [
        field for field in RACE_REQUIRED_FIELDS
        if not getattr(race, field, None)
    ]

    runner_gaps: list[dict[str, Any]] = []
    runners_missing_weight = 0
    active_count = 0

    for entry in card.entries:
        if getattr(entry, "is_scratched", False):
            continue
        active_count += 1
        missing = [
            field for field in RUNNER_REQUIRED_FIELDS
            if not getattr(entry, field, None)
        ]
        if missing:
            runner_gaps.append({
                "runner": entry.horse_name or entry.program_number or "unknown",
                "post_position": entry.post_position,
                "missing": missing,
            })
        if getattr(entry, "weight", None) is None:
            runners_missing_weight += 1

    runners_missing_any = len(runner_gaps)
    acquisition_requirements: list[str] = []
    if runners_missing_weight > 0:
        acquisition_requirements.append(ACQUISITION_REQUIREMENT_WEIGHT)

    eligible = (
        not race_missing
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
        runners_missing_weight=runners_missing_weight,
        runners_missing_any_required=runners_missing_any,
        race_fields_missing=race_missing,
        runner_field_gaps=runner_gaps,
        acquisition_requirements=acquisition_requirements,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Source manifest schema
# ---------------------------------------------------------------------------

# Canonical values for manifest fields.
FIELD_STATUS_PRE_RACE = "pre_race"
SOURCE_TIER_OPERATOR_ATTESTED = "OPERATOR_ATTESTED"
SOURCE_PROVIDER_DK_MARKDOWN = "draftkings_markdown"
WEIGHT_STATUS_PRESENT = "PRESENT_FOR_ALL_ACTIVE_RUNNERS"
WEIGHT_STATUS_ABSENT = "ABSENT_FOR_ONE_OR_MORE_ACTIVE_RUNNERS"


@dataclasses.dataclass
class SourceManifest:
    """Pre-race source manifest that accompanies a DK Markdown card file.

    This is the minimum artifact set required alongside the raw card file before
    it can advance to scoring readiness:

        {TRACK}_DK_Horse_R{n}_{M-D-YY}.md
        {TRACK}_R{n}_{YYYY-MM-DD}_source_manifest.json

    All timestamps must be ISO 8601 with UTC offset.
    """
    track_code: str
    race_number: int
    race_date: str                   # YYYY-MM-DD
    scheduled_post_timestamp: str    # YYYY-MM-DDTHH:MM:SS+HH:MM
    source_as_of_timestamp: str      # YYYY-MM-DDTHH:MM:SS+HH:MM
    source_provider: str             # draftkings_markdown
    source_tier: str                 # OPERATOR_ATTESTED
    raw_file_name: str
    raw_file_sha256: str
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
# Manifest builder helper
# ---------------------------------------------------------------------------

def build_manifest_from_card(
    card: Any,
    *,
    scheduled_post_timestamp: str,
    source_as_of_timestamp: str | None = None,
) -> SourceManifest:
    """Build a SourceManifest from a parsed DraftKingsMarkdownCard.

    Parameters
    ----------
    card:
        Parsed ``DraftKingsMarkdownCard``.
    scheduled_post_timestamp:
        ISO 8601 string with UTC offset for the scheduled post time.
    source_as_of_timestamp:
        ISO 8601 string with UTC offset for when the source was captured.
        Defaults to ``card.as_of``.
    """
    race = card.race
    result = evaluate_acquisition_rule(card)

    as_of_str = source_as_of_timestamp or card.as_of.isoformat()
    raw_sha = card.source_sha256
    raw_name = Path(card.source_path).name

    weight_status = (
        WEIGHT_STATUS_PRESENT
        if result.runners_missing_weight == 0
        else WEIGHT_STATUS_ABSENT
    )

    return SourceManifest(
        track_code=(race.track or "UNKNOWN").strip()[:4].upper(),
        race_number=race.race_number or 0,
        race_date=race.race_date.isoformat() if race.race_date else "UNKNOWN",
        scheduled_post_timestamp=scheduled_post_timestamp,
        source_as_of_timestamp=as_of_str,
        source_provider=SOURCE_PROVIDER_DK_MARKDOWN,
        source_tier=SOURCE_TIER_OPERATOR_ATTESTED,
        raw_file_name=raw_name,
        raw_file_sha256=raw_sha,
        field_status=FIELD_STATUS_PRE_RACE,
        assigned_weight_status=weight_status,
    )


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def validate_manifest(manifest: SourceManifest) -> list[str]:
    """Return a list of errors; empty list means the manifest is well-formed."""
    errors: list[str] = []
    if not manifest.track_code:
        errors.append("track_code is required")
    if not manifest.race_date or manifest.race_date == "UNKNOWN":
        errors.append("race_date is required (YYYY-MM-DD)")
    if not manifest.scheduled_post_timestamp:
        errors.append("scheduled_post_timestamp is required")
    if not manifest.raw_file_name:
        errors.append("raw_file_name is required")
    if not manifest.raw_file_sha256 or len(manifest.raw_file_sha256) != 64:
        errors.append("raw_file_sha256 must be a 64-character hex SHA-256")
    if manifest.field_status != FIELD_STATUS_PRE_RACE:
        errors.append(f"field_status must be '{FIELD_STATUS_PRE_RACE}'; got '{manifest.field_status}'")
    if manifest.source_provider != SOURCE_PROVIDER_DK_MARKDOWN:
        errors.append(f"source_provider must be '{SOURCE_PROVIDER_DK_MARKDOWN}'")
    if manifest.assigned_weight_status not in (WEIGHT_STATUS_PRESENT, WEIGHT_STATUS_ABSENT):
        errors.append(
            f"assigned_weight_status must be '{WEIGHT_STATUS_PRESENT}' or '{WEIGHT_STATUS_ABSENT}'"
        )
    return errors


# ---------------------------------------------------------------------------
# Manifest SHA-256 convenience
# ---------------------------------------------------------------------------

def sha256_of_file(path: str | Path) -> str:
    """Return lowercase hex SHA-256 of a file, for manifest population."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
