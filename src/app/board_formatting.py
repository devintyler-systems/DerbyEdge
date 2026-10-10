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


def confidence_select_fragment(confidence_score_column_present: bool) -> str:
    """Build load_board's confidence SELECT fragment for either entry_scores schema.

    When the modern ``confidence_score`` column exists, project the genuinely
    persisted, scored three-way ``confidence_bucket`` as-is. Otherwise
    (pre-migration legacy schema) synthesize a bucket from the binary
    ``confidence_flag`` alone — this can only ever prove LOW or "not LOW", never
    a real MEDIUM or HIGH. Either branch projects an explicit
    ``confidence_bucket_is_persisted`` indicator (1 / 0) so downstream display
    code never has to guess provenance from whether some other column is null.
    """
    if confidence_score_column_present:
        return (
            "es.confidence_score,\n"
            "                   es.confidence_bucket,\n"
            "                   es.confidence_reasons,\n"
            "                   1 AS confidence_bucket_is_persisted,"
        )
    return (
        "NULL AS confidence_score,\n"
        "                   CASE WHEN es.confidence_flag = 0 THEN 'LOW'"
        " ELSE 'MEDIUM' END AS confidence_bucket,\n"
        "                   NULL AS confidence_reasons,\n"
        "                   0 AS confidence_bucket_is_persisted,"
    )


def resolve_confidence_badge_key(
    confidence_bucket: object,
    confidence_bucket_is_persisted: object,
    confidence_flag: object,
) -> str:
    """Resolve the horse-detail confidence badge key without overclaiming.

    ``confidence_bucket_is_persisted`` is an explicit indicator projected by
    the query itself — 1 when ``confidence_bucket`` came straight from a
    genuinely scored, persisted three-way value (LOW/MEDIUM/HIGH), 0 when the
    legacy-schema query instead synthesized it via
    ``CASE WHEN confidence_flag = 0 THEN 'LOW' ELSE 'MEDIUM' END``. It is not
    guessed from whether some other column happens to be null.

    A synthesized bucket is trustworthy for LOW (the flag says so exactly)
    but a synthesized MEDIUM only means "not LOW" — it could really be HIGH —
    so that case renders an explicit ambiguous label instead of asserting
    either level. A row claiming to be persisted but missing a valid bucket
    value must not fabricate HIGH or MEDIUM either; it falls back to the
    same flag-based rule as a legacy row.
    """
    bucket = (
        str(confidence_bucket).strip().upper()
        if confidence_bucket not in (None, "")
        else None
    )

    if bool(confidence_bucket_is_persisted) and bucket in ("LOW", "MEDIUM", "HIGH"):
        return bucket.lower()

    try:
        flag = int(confidence_flag)
    except (TypeError, ValueError):
        flag = None

    if flag == 0:
        return "low"
    return "ambiguous"


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


DIAGNOSTIC_NOTICE = (
    "DIAGNOSTIC FORECAST - not valid for betting. These are the stored model probabilities of an uncalibrated seed "
    "baseline. The eligibility gate withholds Win %, fair odds, edge and bet tags until the scoring context passes it."
)


def _finite(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def is_diagnostic_only(board: pd.DataFrame | None) -> bool:
    """True when the gated Win % is blank for every runner but a stored diagnostic probability exists."""
    if board is None or board.empty or "diagnostic_win_probability" not in board.columns:
        return False
    gated = pd.to_numeric(board.get("win_probability"), errors="coerce")
    diag = pd.to_numeric(board["diagnostic_win_probability"], errors="coerce")
    return not bool(gated.notna().any()) and bool(diag.notna().any())


def win_pct_text(win_probability: object, diagnostic_win_probability: object = None) -> tuple[str, str]:
    """(label, text) for a single Win % readout: the valid value, else the labelled diagnostic, else unavailable."""
    valid = _finite(win_probability)
    if valid is not None:
        return "Win %", f"{valid * 100:.1f}%"
    diag = _finite(diagnostic_win_probability)
    if diag is not None:
        return "Diagnostic Win % (not valid for betting)", f"{diag * 100:.1f}%"
    return "Win %", "unavailable"


def diagnostic_forecast_frame(board: pd.DataFrame | None) -> pd.DataFrame | None:
    """The labelled diagnostic table, or None when the board has valid Win % (or no stored probability)."""
    if not is_diagnostic_only(board):
        return None
    out = pd.DataFrame({
        "Horse": board["horse_name"],
        "Post": board["post_position"],
        "Morning Line": board["morning_line_odds"].apply(morning_line_str),
    })
    diag = pd.to_numeric(board["diagnostic_win_probability"], errors="coerce")
    ml = pd.to_numeric(board.get("market_implied_prob"), errors="coerce")
    out["Diagnostic Win %"] = (diag * 100).round(1)
    out["ML-Implied %"] = (ml * 100).round(1)
    out["Diff vs ML (pts)"] = ((diag - ml) * 100).round(1)
    out = out.assign(_k=diag).sort_values("_k", ascending=False, na_position="last").drop(columns="_k")
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out.reset_index(drop=True)


def diagnostic_reason_codes(board: pd.DataFrame | None) -> list[str]:
    """Distinct eligibility reason codes the gate recorded, in first-seen order."""
    if board is None or "score_eligibility_reason_codes" not in board.columns:
        return []
    seen: dict[str, None] = {}
    for cell in board["score_eligibility_reason_codes"].dropna():
        for code in str(cell).split(";"):
            if code.strip():
                seen.setdefault(code.strip(), None)
    return list(seen)
