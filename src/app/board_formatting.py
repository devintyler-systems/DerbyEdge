"""Null-safe, contract-aware formatting for the Race Board."""

from __future__ import annotations

import math

import pandas as pd


LEGACY_MISSING_DATA_FLAG_NOTICE = (
    "Legacy completeness flag: this value is not evidence that a specific "
    "required or optional input was missing. Review the run's source audit."
)


def missing_data_flag_notice(value: object) -> str | None:
    """Render the historical flag without inventing a missing-input reason."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(numeric) and numeric == 1.0:
        return LEGACY_MISSING_DATA_FLAG_NOTICE
    return None


def market_probability_display(
    live_market_prob: object,
    persisted_market_prob: object,
) -> tuple[str, str, bool]:
    """Select the display market without inventing or conflating a price source."""
    for value, label in (
        (live_market_prob, "Live Mkt %"),
        (persisted_market_prob, "ML-Implied %"),
    ):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            return f"{numeric * 100:.1f}%", label, True
    return "MISSING", "ML-Implied %", False


def _edge_str(value: object) -> str:
    """Format a usable numeric edge, otherwise render an explicit unavailable mark."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(numeric):
        return "—"
    return f"+{numeric:.3f}" if numeric > 0 else f"{numeric:.3f}"


def morning_line_str(value: object) -> str:
    """Format valid morning-line odds without crashing on sparse source values."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{numeric:.0f}-1" if math.isfinite(numeric) else "—"


def pace_fit_str(value: object) -> str:
    """Render pace fit only when it is genuinely runner-specific evidence."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{numeric:.3f}" if math.isfinite(numeric) else "—"


def prepare_probability_display_columns(
    table: pd.DataFrame,
    *,
    show_edge: bool,
) -> pd.DataFrame:
    """Add only display fields the board contract permits.

    In particular, a limited no-live-odds board never reads or formats
    ``value_score``.  This keeps unavailable persisted values out of both the
    rendered table and its pre-render transformation path.
    """
    out = table.copy()
    out["Win%"] = (pd.to_numeric(out["win_probability"], errors="coerce") * 100).round(2)
    out["ML"] = out["morning_line_odds"].apply(morning_line_str)
    out["ML-Implied %"] = (
        pd.to_numeric(out["market_implied_prob"], errors="coerce") * 100
    ).round(2)
    if "pace_fit_score" in out.columns:
        out["Pace Fit"] = out["pace_fit_score"].apply(pace_fit_str)
    if show_edge:
        out["Edge"] = out["value_score"].apply(_edge_str)
    return out
