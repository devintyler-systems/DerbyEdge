"""Null-safe, contract-aware formatting for the Race Board."""

from __future__ import annotations

import math

import pandas as pd


DK_BLOCKED_GUIDANCE = (
    "DraftKings Horse program PDF detected. Runner headers were found, but "
    "past-performance sections could not yet be linked to runners. Inspect "
    "parser diagnostics or upload a supported native PP source."
)
GENERIC_BLOCKED_GUIDANCE = "Re-upload the original 1/ST PDF or inspect parser diagnostics."


def blocked_state_guidance(audit: object) -> str:
    """Choose safe, source-aware blocked-card guidance for partial UI state."""
    safe_audit = audit if isinstance(audit, dict) else {}
    if safe_audit.get("recommended_action"):
        action = str(safe_audit["recommended_action"])
        if safe_audit.get("field_reconciliation_status") == "exact" and "reconciliation" in action.lower():
            return DK_BLOCKED_GUIDANCE
        return action
    source_format = str(safe_audit.get("source_format") or "")
    if source_format.startswith("dkhorse"):
        total_linked = int(safe_audit.get("total_pp_records_linked") or 0)
        unresolved_id = int(safe_audit.get("unresolved_identity_count") or 0)
        if total_linked > 0 and unresolved_id > 0:
            return (
                "DraftKings Horse program PDF detected. Historical starts were linked for part of the field, "
                "but one or more runner identities are malformed or duplicated. Scoring remains blocked "
                "until active-entry identity is resolved."
            )
        return DK_BLOCKED_GUIDANCE
    return GENERIC_BLOCKED_GUIDANCE


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
