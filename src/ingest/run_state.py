"""Deterministic data-quality and scoring-state rules.

This module is intentionally independent of Streamlit and the trained model.
Every scoring entry point must resolve a :class:`RunMode` before invoking a
model so a market baseline cannot be mislabeled as a forecast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence


class RunMode(StrEnum):
    BLOCKED = "BLOCKED"
    MARKET_BASELINE_ONLY = "MARKET_BASELINE_ONLY"
    MARKET_ANCHORED_NOT_ACTIONABLE = "MARKET_ANCHORED_NOT_ACTIONABLE"
    PP_PARSED_FEATURES_PENDING = "PP_PARSED_FEATURES_PENDING"
    MODEL_READY_LIMITED = "MODEL_READY_LIMITED"
    MODEL_READY = "MODEL_READY"


@dataclass(frozen=True)
class DataQuality:
    entries_parsed: int
    field_size_declared: int | None
    entries_with_pp_history: int
    starter_match_rate: float
    race_metadata_complete: bool
    has_morning_lines: bool
    has_live_odds: bool
    required_model_features_complete: bool
    blocking_errors: list[str] = field(default_factory=list)
    # Raw declared fields can include source-confirmed scratches. All gates use
    # active entries, while this count lets the field-size gate distinguish a
    # fully explained scratch reduction from a parser omission.
    entries_scratched: int = 0
    active_entry_count: int | None = None
    nonstarter_count: int = 0
    field_reconciliation_status: str = "unknown"
    experienced_field: bool = True
    workout_forward_low_history: bool = False
    source_format: str | None = None
    identity_resolution_rate: float | None = None
    starter_pp_link_rate: float | None = None
    experienced_field_pp_coverage: float | None = None
    resolved_no_history_count: int | None = None
    unresolved_identity_count: int | None = None
    unresolved_history_count: int | None = None


MIN_ACTIVE_ENTRIES = 4
MIN_STARTER_MATCH_RATE = 0.90
MIN_IDENTITY_RESOLUTION_RATE = 0.90
MIN_EXPERIENCED_FIELD_PP_COVERAGE = 0.70


def resolve_run_mode(q: DataQuality) -> tuple[RunMode, list[str]]:
    """Resolve the only product state that the supplied data can support."""
    errors = list(q.blocking_errors)

    active_entries = q.active_entry_count if q.active_entry_count is not None else q.entries_parsed
    if active_entries < MIN_ACTIVE_ENTRIES:
        errors.append(f"Fewer than {MIN_ACTIVE_ENTRIES} valid active entries were parsed.")

    expected_active = (
        q.field_size_declared - q.entries_scratched
        if q.field_size_declared is not None else None
    )
    if q.field_reconciliation_status == "unexplained" or (expected_active is not None and active_entries != expected_active and q.field_reconciliation_status != "late_scratch_explained"):
        errors.append(
            f"Declared field size is {q.field_size_declared}; "
            f"only {active_entries} active entries were parsed"
            + (
                f" after {q.entries_scratched} parsed scratches."
                if q.entries_scratched else "."
            )
        )

    if not q.race_metadata_complete:
        errors.append("Required race metadata is incomplete.")

    is_dk = bool(q.source_format and str(q.source_format).startswith("dkhorse"))
    if is_dk:
        if q.identity_resolution_rate is None:
            errors.append("DK Horse identity resolution metric is missing.")
        if q.experienced_field_pp_coverage is None:
            errors.append("DK Horse experienced field PP coverage metric is missing.")

    if q.unresolved_identity_count is not None and q.unresolved_identity_count > 0:
        errors.append(f"{q.unresolved_identity_count} active entries have unresolved runner identity.")

    if q.unresolved_history_count is not None and q.unresolved_history_count > 0:
        errors.append(f"{q.unresolved_history_count} runners expected to have history have unresolved history.")

    if q.entries_with_pp_history == 0:
        if q.experienced_field and not q.workout_forward_low_history:
            errors.append(
                "No usable past-performance rows are linked in an experienced field."
            )
        elif q.has_morning_lines:
            return RunMode.MARKET_BASELINE_ONLY, [
                "WORKOUT_FORWARD_LOW_HISTORY: source supports a debut-heavy field.",
                "Morning-line implied probabilities are reference only, not model output.",
            ]
        else:
            errors.append(
                "No PP history and no usable morning-line odds were parsed."
            )

    if q.identity_resolution_rate is not None:
        if q.identity_resolution_rate < MIN_IDENTITY_RESOLUTION_RATE:
            errors.append(
                f"Identity resolution rate is {q.identity_resolution_rate:.0%}; minimum is 90%."
            )
    elif q.starter_match_rate < MIN_STARTER_MATCH_RATE:
        errors.append(
            f"Starter-to-PP match rate is {q.starter_match_rate:.0%}; minimum is 90%."
        )

    if q.experienced_field and not q.workout_forward_low_history:
        if q.experienced_field_pp_coverage is not None:
            if q.experienced_field_pp_coverage < MIN_EXPERIENCED_FIELD_PP_COVERAGE:
                errors.append(
                    f"Experienced field PP coverage is {q.experienced_field_pp_coverage:.0%}; minimum is {MIN_EXPERIENCED_FIELD_PP_COVERAGE:.0%}."
                )
        else:
            pp_coverage = q.entries_with_pp_history / active_entries
            if pp_coverage < MIN_EXPERIENCED_FIELD_PP_COVERAGE:
                errors.append(
                    f"PP coverage is {pp_coverage:.0%}; minimum is {MIN_EXPERIENCED_FIELD_PP_COVERAGE:.0%} of active entries."
                )

    if errors:
        return RunMode.BLOCKED, _dedupe(errors)

    if not q.required_model_features_complete:
        return RunMode.PP_PARSED_FEATURES_PENDING, [
            "Past performances were parsed, but the model-required feature schema "
            "has not passed verification."
        ]

    if q.has_live_odds:
        return RunMode.MODEL_READY, []

    return RunMode.MODEL_READY_LIMITED, [
        "Forecast uses only 1/ST PDF-supported features.",
        "No live odds supplied; edge, fair odds comparison, and bet tags remain disabled.",
    ]


@dataclass(frozen=True)
class RaceScore:
    horse_key: str
    p_model: float | None
    p_ml_implied: float | None
    p_market_live: float | None
    fair_odds_decimal: float | None
    edge_vs_live_market: float | None
    bet_tag: str | None


def fair_odds_from_probability(p_model: float | None) -> float | None:
    if p_model is None or not 0 < p_model < 1:
        return None
    return round(1.0 / p_model, 2)


def edge_vs_market(
    p_model: float | None,
    p_market_live: float | None,
) -> float | None:
    if p_model is None or p_market_live is None:
        return None
    return p_model - p_market_live


def market_baseline_scores(entries: Sequence[Mapping[str, Any]]) -> list[RaceScore]:
    """Build an ML-only reference vector with all model fields forced to null."""
    raw: list[float | None] = []
    for entry in entries:
        try:
            decimal = float(entry.get("morning_line_decimal"))
        except (TypeError, ValueError):
            decimal = 0.0
        raw.append(1.0 / decimal if decimal > 1.0 else None)
    total = sum(value for value in raw if value is not None)

    scores: list[RaceScore] = []
    for entry, implied in zip(entries, raw):
        p_ml = implied / total if implied is not None and total > 0 else None
        scores.append(
            RaceScore(
                horse_key=str(entry.get("horse_key") or ""),
                p_model=None,
                p_ml_implied=p_ml,
                p_market_live=None,
                fair_odds_decimal=None,
                edge_vs_live_market=None,
                bet_tag=None,
            )
        )
    return scores


def feature_degeneracy_warnings(
    feature_rows: Sequence[Mapping[str, Any]],
    feature_names: Iterable[str],
) -> list[str]:
    """Flag nontrivial engineered features that are constant across a field."""
    warnings: list[str] = []
    for name in feature_names:
        values = {
            row.get(name)
            for row in feature_rows
            if row.get(name) is not None
        }
        if feature_rows and len(values) <= 1:
            warnings.append(f"FEATURE_DEGENERACY_WARNING: {name} is constant across entries.")
    return warnings


def resolve_mode_with_feature_checks(
    q: DataQuality,
    feature_rows: Sequence[Mapping[str, Any]],
) -> tuple[RunMode, list[str]]:
    """Apply the post-feature degeneracy gate without changing the base rules."""
    mode, reasons = resolve_run_mode(q)
    key_features = ("pace_fit", "form", "surface_distance_fit")
    warnings = feature_degeneracy_warnings(feature_rows, key_features)
    all_key_features_constant = len(warnings) == len(key_features)
    if mode in (
        RunMode.PP_PARSED_FEATURES_PENDING,
        RunMode.MODEL_READY_LIMITED,
        RunMode.MODEL_READY,
    ) and all_key_features_constant:
        return RunMode.BLOCKED, reasons + warnings + [
            "All core engineered features are degenerate; forecast scoring is blocked."
        ]
    return mode, reasons + warnings


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
