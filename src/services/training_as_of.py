"""Classify whether a persisted PP row can evidence its historical target race.

``horse_starts.card_id`` identifies the card whose PP document supplied a
row. ``horse_starts.start_date`` identifies the earlier race described by the
row. A later parent-card artifact can never become a pre-race snapshot for
that historical target.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


PRE_RACE_TARGET_PROVEN = "PRE_RACE_TARGET_PROVEN"
PRE_RACE_TARGET_OPERATOR_ATTESTED = "PRE_RACE_TARGET_OPERATOR_ATTESTED"
POST_RACE_TARGET_SNAPSHOT = "POST_RACE_TARGET_SNAPSHOT"
MISSING_TARGET_DECISION_TIME = "MISSING_TARGET_DECISION_TIME"
MISSING_SOURCE_AS_OF = "MISSING_SOURCE_AS_OF"
MISSING_SOURCE_ARTIFACT = "MISSING_SOURCE_ARTIFACT"
VALID_TRAINING_AS_OF_STATUSES = frozenset({
    PRE_RACE_TARGET_PROVEN,
    PRE_RACE_TARGET_OPERATOR_ATTESTED,
})
_VALIDATION_STATUSES = frozenset({"PASS", "VALID", "VALIDATED"})


def _parse(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_time(value: Any) -> bool:
    text = str(value or "").strip()
    return "T" in text or (" " in text and len(text) > 10)


def _source_tier(validation_status: Any, as_of_status: Any) -> str | None:
    """Return a tier candidate without relaxing source-validation policy."""
    validation = str(validation_status or "").upper()
    as_of = str(as_of_status or "").upper()
    if validation == "OPERATOR_ATTESTED":
        return PRE_RACE_TARGET_OPERATOR_ATTESTED
    if validation in _VALIDATION_STATUSES and as_of == "PROVEN":
        return PRE_RACE_TARGET_PROVEN
    return None


def training_as_of_status(
    *,
    source_artifact_id: Any,
    source_as_of_timestamp: Any,
    target_decision_timestamp: Any,
    artifact_validation_status: Any = None,
    artifact_as_of_status: Any = None,
) -> str:
    """Return the strict historical-target as-of classification for one PP row.

    A date-only target is conservative: an earlier source date proves order, a
    later source date proves leakage, and same-day values are explicitly
    unproven because the target post time was not persisted.
    """
    if source_artifact_id is None:
        return MISSING_SOURCE_ARTIFACT
    source = _parse(source_as_of_timestamp)
    if source is None:
        return MISSING_SOURCE_AS_OF
    target = _parse(target_decision_timestamp)
    if target is None:
        return MISSING_TARGET_DECISION_TIME
    if source.date() > target.date():
        return POST_RACE_TARGET_SNAPSHOT
    if source.date() < target.date():
        return _source_tier(artifact_validation_status, artifact_as_of_status) or MISSING_SOURCE_AS_OF
    if not _has_time(source_as_of_timestamp) or not _has_time(target_decision_timestamp):
        return MISSING_TARGET_DECISION_TIME
    if (source.tzinfo is None) != (target.tzinfo is None):
        return MISSING_TARGET_DECISION_TIME
    if source >= target:
        return POST_RACE_TARGET_SNAPSHOT
    return _source_tier(artifact_validation_status, artifact_as_of_status) or MISSING_SOURCE_AS_OF
