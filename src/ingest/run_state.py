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


def resolve_run_mode(q: DataQuality) -> tuple[RunMode, list[str]]:
    """Resolve the only product state that the supplied data can support."""
    errors = list(q.blocking_errors)

    if q.entries_parsed < 2:
        errors.append("Fewer than two valid entries were parsed.")

    if q.field_size_declared and q.entries_parsed != q.field_size_declared:
        errors.append(
            f"Declared field size is {q.field_size_declared}; "
            f"only {q.entries_parsed} entries were parsed."
        )

    if not q.race_metadata_complete:
        errors.append("Required race metadata is incomplete.")

    if errors:
        return RunMode.BLOCKED, _dedupe(errors)

    if q.entries_with_pp_history == 0:
        if q.has_morning_lines:
            return RunMode.MARKET_BASELINE_ONLY, [
                "No past-performance rows attached to entered horses.",
                "Morning-line implied probabilities are reference only, not model output.",
            ]
        return RunMode.BLOCKED, [
            "No PP history and no usable morning-line odds were parsed."
        ]

    if q.starter_match_rate < 0.90:
        return RunMode.BLOCKED, [
            f"Starter-to-PP match rate is {q.starter_match_rate:.0%}; minimum is 90%."
        ]

    pp_coverage = q.entries_with_pp_history / q.entries_parsed
    if pp_coverage < 0.80:
        return RunMode.BLOCKED, [
            f"PP coverage is {pp_coverage:.0%}; minimum is 80% of parsed entries."
        ]

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
