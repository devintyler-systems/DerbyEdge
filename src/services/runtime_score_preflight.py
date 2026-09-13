"""Race-level, read-only eligibility preflight for runtime score generation.

This module deliberately does not score, normalize probabilities, or write any
database/artifact state.  It adapts the existing per-entry score-eligibility
evaluator into the field-level decision required before the runtime scorer may
begin inference.
"""
# TODO: DERBYEDGE_STRICT_PREFLIGHT=on is non-functional until
# src/services/score_eligibility.py (evaluate_score_eligibility,
# resolve_race_context, resolve_scoring_context) and
# src.models.trainer.load_model_artifact are implemented.
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
import sqlite3
from typing import Any

from src.utils.db import DB_PATH


class ExecutionMode(str, Enum):
    """Declared caller intent; only this mode may produce runtime scores."""

    RUNTIME_PRE_RACE = "RUNTIME_PRE_RACE"
    BACKTEST = "BACKTEST"
    TRAINING = "TRAINING"
    CALIBRATION = "CALIBRATION"
    RETROSPECTIVE_DIAGNOSTIC = "RETROSPECTIVE_DIAGNOSTIC"
    UNKNOWN = "UNKNOWN"


def normalize_execution_mode(value: ExecutionMode | str | None) -> ExecutionMode:
    if isinstance(value, ExecutionMode):
        return value
    try:
        return ExecutionMode(str(value))
    except (TypeError, ValueError):
        return ExecutionMode.UNKNOWN


@dataclass(frozen=True)
class RaceScorePreflightResult:
    """Structured, non-actionable result returned before runtime inference."""

    card_id: int | None
    race_identifier: str | None
    execution_mode: str
    race_score_valid: bool
    score_generated: bool = False
    score_persisted: bool = False
    artifact_written: bool = False
    score_valid: bool = False
    fair_odds_valid: bool = False
    market_comparison_valid: bool = False
    wager_valid: bool = False
    output_status: str = "BLOCKED_INELIGIBLE"
    active_runner_count: int = 0
    race_level_reason_codes: tuple[str, ...] = ()
    entry_level_reason_codes: dict[int, tuple[str, ...]] = field(default_factory=dict)
    artifact_id: Any = None
    artifact_version: Any = None
    calibration_identifier: Any = None
    calibration_status: Any = None
    calibration_audit_timestamp: Any = None
    decision_as_of_timestamp: Any = None
    feature_build_timestamp: Any = None
    per_entry_results: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def active_runtime_entry_ids(db_path: Path, card_id: int) -> list[int]:
    """Return the scorer's live field, excluding scratches via its existing view."""
    conn = _readonly_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT entry_id FROM v_entries_live WHERE card_id=? ORDER BY post_position",
            (card_id,),
        ).fetchall()
    finally:
        conn.close()
    return [int(row[0]) for row in rows]


def _blocked_unresolved(card_id: int | None, mode: ExecutionMode, reason: str) -> RaceScorePreflightResult:
    return RaceScorePreflightResult(
        card_id=card_id,
        race_identifier=None,
        execution_mode=mode.value,
        race_score_valid=False,
        race_level_reason_codes=(reason,),
    )


def preflight_runtime_race_score(
    *, card_id: int, db_path: Path = DB_PATH, execution_mode: ExecutionMode | str = ExecutionMode.RUNTIME_PRE_RACE,
) -> RaceScorePreflightResult:
    """Evaluate every live runner before any runtime score inference or write.

    Missing persisted context is a blocking condition.  This intentionally
    makes an unknown/new runtime caller fail closed rather than assuming a
    catalog, seed, or prior score context is safe.
    """
    from src.models.trainer import load_model_artifact
    from src.services.score_eligibility import (
        evaluate_score_eligibility,
        resolve_race_context,
        resolve_scoring_context,
    )

    mode = normalize_execution_mode(execution_mode)
    if mode is not ExecutionMode.RUNTIME_PRE_RACE:
        return _blocked_unresolved(card_id, mode, "runtime_preflight_not_applicable_to_execution_mode")

    try:
        entry_ids = active_runtime_entry_ids(db_path, card_id)
    except Exception as exc:
        return _blocked_unresolved(card_id, mode, f"runtime_field_context_unresolved:{exc}")
    if not entry_ids:
        return _blocked_unresolved(card_id, mode, "runtime_active_field_unresolved")

    per_entry: dict[int, dict[str, Any]] = {}
    entry_reasons: dict[int, tuple[str, ...]] = {}
    race_reasons: list[str] = []
    identity: dict[str, Any] = {}
    race_identifier: str | None = None

    for entry_id in entry_ids:
        try:
            race_context = resolve_race_context(db_path, card_id, entry_id)
            feature, decision, model = resolve_scoring_context(db_path, card_id, entry_id)
            artifact_path = Path(str(model.get("artifact_path") or ""))
            if not artifact_path.is_file():
                raise LookupError(f"active_model_artifact_unresolved:{artifact_path}")
            artifact = load_model_artifact(artifact_path)
            result = evaluate_score_eligibility(
                race_context=race_context,
                feature_row=feature,
                decision=decision,
                model=model,
                artifact=artifact,
            )
            per_entry[entry_id] = result
            reasons = tuple(result.get("reason_codes") or ())
            entry_reasons[entry_id] = reasons
            race_reasons.extend(reasons)
            race_identifier = race_identifier or (
                f"{race_context.get('card_date')}#{race_context.get('race_number')}"
            )
            if not identity:
                identity = {
                    "artifact_id": result["artifact"].get("model_id"),
                    "artifact_version": result["artifact"].get("version"),
                    "calibration_identifier": result["calibration"].get("identifier"),
                    "calibration_status": result["calibration"].get("status"),
                    "calibration_audit_timestamp": result["calibration"].get("audit_timestamp"),
                    "decision_as_of_timestamp": result.get("decision_timestamp"),
                    "feature_build_timestamp": race_context.get("build_ts"),
                }
            elif (identity["artifact_id"], identity["artifact_version"]) != (
                result["artifact"].get("model_id"), result["artifact"].get("version")
            ):
                entry_reasons[entry_id] = tuple(dict.fromkeys(reasons + ("runtime_field_model_context_mismatch",)))
                race_reasons.append("runtime_field_model_context_mismatch")
        except Exception as exc:
            reason = "score_eligibility_context_unresolved"
            # Keep machine-readable reason codes deterministic; preserve the
            # diagnostic only in the structured per-entry audit payload.
            per_entry[entry_id] = {
                "score_valid": False,
                "reason_codes": [reason],
                "context_error": str(exc),
            }
            entry_reasons[entry_id] = (reason,)
            race_reasons.append(reason)

    deduplicated_reasons = tuple(dict.fromkeys(race_reasons))
    valid = (
        bool(per_entry)
        and not any(reason == "runtime_field_model_context_mismatch" for reason in race_reasons)
        and all(item.get("score_valid") is True for item in per_entry.values())
    )
    return RaceScorePreflightResult(
        card_id=card_id,
        race_identifier=race_identifier,
        execution_mode=mode.value,
        race_score_valid=valid,
        score_valid=valid,
        fair_odds_valid=valid,
        market_comparison_valid=valid,
        wager_valid=valid,
        output_status="PREFLIGHT_PASSED" if valid else "BLOCKED_INELIGIBLE",
        active_runner_count=len(entry_ids),
        race_level_reason_codes=deduplicated_reasons,
        entry_level_reason_codes=entry_reasons,
        per_entry_results=per_entry,
        **identity,
    )
