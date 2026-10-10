from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from src.app.board_formatting import (
    _edge_str,
    confidence_select_fragment,
    market_probability_display,
    missing_data_flag_notice,
    morning_line_str,
    pace_fit_str,
    prepare_probability_display_columns,
    resolve_confidence_badge_key,
)
from src.app.board_state import race_board_contract
from src.ingest.run_state import RunMode


def _table(value_score: object, morning_line_odds: object = 8.0) -> pd.DataFrame:
    return pd.DataFrame({
        "win_probability": [0.42],
        "morning_line_odds": [morning_line_odds],
        "market_implied_prob": [0.36],
        "value_score": [value_score],
        "pace_fit_score": [None],
    })


def test_limited_board_does_not_format_or_create_edge_without_live_odds():
    contract = race_board_contract(RunMode.MODEL_READY_LIMITED, has_live_odds=False)

    rendered = prepare_probability_display_columns(
        _table(None), show_edge=contract.show_edge
    )

    assert contract.show_model_probability
    assert contract.show_morning_line_reference
    assert not contract.show_fair_odds
    assert not contract.show_edge
    assert not contract.show_bet_tags
    assert not contract.show_stakes
    assert "Edge" not in rendered.columns
    assert rendered.loc[0, "ML"] == "8-1"
    assert rendered.loc[0, "Pace Fit"] == "—"


def test_edge_formatter_returns_unavailable_for_null_or_nonfinite_values():
    assert _edge_str(None) == "—"
    assert _edge_str(float("nan")) == "—"
    assert _edge_str("not-a-number") == "—"
    assert _edge_str(float("inf")) == "—"


def test_ready_board_with_complete_live_market_formats_valid_edge():
    contract = race_board_contract(RunMode.MODEL_READY, has_live_odds=True)

    rendered = prepare_probability_display_columns(
        _table(0.061), show_edge=contract.show_edge
    )

    assert contract.show_edge
    assert rendered.loc[0, "Edge"] == "+0.061"


def test_morning_line_formatter_handles_missing_odds():
    assert morning_line_str(None) == "—"
    assert morning_line_str(np.nan) == "—"
    assert morning_line_str("bad") == "—"


def test_pace_fit_formatter_handles_unavailable_pace():
    assert pace_fit_str(None) == "—"
    assert pace_fit_str(np.nan) == "—"
    assert pace_fit_str(0.72) == "0.720"


def test_market_probability_display_preserves_precedence_and_blocks_missing_tag():
    assert market_probability_display(0.22, 0.31) == ("22.0%", "Live Mkt %", True)
    assert market_probability_display(None, 0.31) == ("31.0%", "ML-Implied %", True)

    display, label, allow_bet_tag = market_probability_display(None, None)

    assert display == "MISSING"
    assert label == "ML-Implied %"
    assert not allow_bet_tag


def test_legacy_missing_data_flag_does_not_claim_specific_missing_inputs():
    notice = missing_data_flag_notice(1)

    assert notice is not None
    assert "not evidence" in notice
    assert "required or optional input" in notice
    assert "Missing data flags" not in notice
    assert missing_data_flag_notice(0) is None
    assert missing_data_flag_notice(None) is None


# ---------------------------------------------------------------------------
# resolve_confidence_badge_key — issue #28 regression coverage
# ---------------------------------------------------------------------------

def test_persisted_high_bucket_with_flag_1_renders_high_not_medium():
    """A modern, genuinely persisted HIGH bucket must not collapse to MED."""
    assert resolve_confidence_badge_key("HIGH", 1, 1) == "high"


def test_persisted_high_bucket_is_trusted_via_explicit_indicator_not_via_score_nullness():
    """Essential counterexample: the decision must key off the explicit
    confidence_bucket_is_persisted indicator, not off confidence_score being
    non-null. A persisted HIGH bucket with flag=1 must render HIGH even in a
    row shape where confidence_score itself would be NULL."""
    assert resolve_confidence_badge_key("HIGH", 1, 1) == "high"


def test_persisted_medium_bucket_with_flag_1_renders_medium():
    assert resolve_confidence_badge_key("MEDIUM", 1, 1) == "medium"


def test_persisted_low_bucket_with_flag_0_renders_low():
    assert resolve_confidence_badge_key("LOW", 1, 0) == "low"


def test_legacy_synthesized_medium_with_flag_1_is_ambiguous_not_asserted():
    """Legacy schema synthesizes a 'MEDIUM' bucket from flag=1 alone
    (confidence_bucket_is_persisted=0). That synthesized value must not be
    trusted as a real MEDIUM — it could really be HIGH — so the badge key
    must be an explicit ambiguous label."""
    synthesized_legacy_bucket = "MEDIUM"  # what the legacy SQL CASE would produce
    assert resolve_confidence_badge_key(synthesized_legacy_bucket, 0, 1) == "ambiguous"


def test_legacy_flag_0_without_persisted_bucket_is_low():
    assert resolve_confidence_badge_key("LOW", 0, 0) == "low"


def test_legacy_flag_1_with_no_bucket_value_at_all_is_ambiguous():
    assert resolve_confidence_badge_key(None, 0, 1) == "ambiguous"


def test_modern_row_without_valid_stored_bucket_does_not_fabricate_high_or_medium():
    """A row claiming confidence_bucket_is_persisted=1 but carrying no valid
    bucket value (defensive edge case) must not fabricate HIGH or MEDIUM —
    it falls back to the same flag-based rule as a legacy row."""
    assert resolve_confidence_badge_key(None, 1, 1) == "ambiguous"
    assert resolve_confidence_badge_key(None, 1, 0) == "low"
    assert resolve_confidence_badge_key("", 1, 1) == "ambiguous"


def test_confidence_badge_resolution_does_not_touch_wagering_fields():
    """Guard: resolving the confidence badge key must be display-only — it must
    never read or mutate scoring/wagering fields on the horse row."""
    row = pd.Series({
        "win_probability": 0.42,
        "fair_odds": 3.5,
        "value_score": 0.061,
        "morning_line_odds": 8.0,
        "market_implied_prob": 0.36,
        "confidence_bucket": "HIGH",
        "confidence_bucket_is_persisted": 1,
        "confidence_flag": 1,
    })
    before = row.copy(deep=True)

    key = resolve_confidence_badge_key(
        row["confidence_bucket"],
        row["confidence_bucket_is_persisted"],
        row["confidence_flag"],
    )

    assert key == "high"
    pd.testing.assert_series_equal(row, before)


# ---------------------------------------------------------------------------
# confidence_select_fragment — isolated query-path provenance assertion
# ---------------------------------------------------------------------------
#
# This exercises the exact SQL fragment text load_board embeds, executed
# against an in-memory sqlite3 connection standing in for entry_scores.
# No app.py import, no production database.

def test_confidence_select_fragment_modern_projects_persisted_bucket_via_sqlite():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE entry_scores ("
            "confidence_flag INTEGER, confidence_bucket TEXT, "
            "confidence_score REAL, confidence_reasons TEXT)"
        )
        conn.execute(
            "INSERT INTO entry_scores VALUES (1, 'HIGH', 0.82, 'clear separation')"
        )
        fragment = confidence_select_fragment(confidence_score_column_present=True)
        row = conn.execute(
            f"SELECT es.confidence_flag, {fragment} 1 AS dummy FROM entry_scores AS es"
        ).fetchone()
    finally:
        conn.close()

    confidence_flag, confidence_score, confidence_bucket, confidence_reasons, is_persisted, dummy = row
    assert confidence_bucket == "HIGH"
    assert is_persisted == 1
    assert confidence_score == 0.82


def test_confidence_select_fragment_legacy_synthesizes_bucket_and_marks_not_persisted():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE entry_scores (confidence_flag INTEGER)")
        conn.execute("INSERT INTO entry_scores VALUES (1)")
        fragment = confidence_select_fragment(confidence_score_column_present=False)
        row = conn.execute(
            f"SELECT es.confidence_flag, {fragment} 1 AS dummy FROM entry_scores AS es"
        ).fetchone()
    finally:
        conn.close()

    confidence_flag, confidence_score, confidence_bucket, confidence_reasons, is_persisted, dummy = row
    assert confidence_bucket == "MEDIUM"  # synthesized guess, not a real MEDIUM
    assert is_persisted == 0
    assert confidence_score is None


def test_confidence_select_fragment_legacy_flag_0_synthesizes_low():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE entry_scores (confidence_flag INTEGER)")
        conn.execute("INSERT INTO entry_scores VALUES (0)")
        fragment = confidence_select_fragment(confidence_score_column_present=False)
        row = conn.execute(
            f"SELECT es.confidence_flag, {fragment} 1 AS dummy FROM entry_scores AS es"
        ).fetchone()
    finally:
        conn.close()

    confidence_flag, confidence_score, confidence_bucket, confidence_reasons, is_persisted, dummy = row
    assert confidence_bucket == "LOW"
    assert is_persisted == 0


# ---- diagnostic forecast display (stored probability behind a blocked eligibility gate) -----------------
from src.app.board_formatting import (  # noqa: E402
    DIAGNOSTIC_NOTICE, diagnostic_forecast_frame, diagnostic_reason_codes, is_diagnostic_only, win_pct_text,
)


def _blocked_board():
    return pd.DataFrame({
        "horse_name": ["A", "B", "C"], "post_position": [1, 2, 3], "morning_line_odds": [2.0, 4.0, 9.0],
        "market_implied_prob": [0.3333, 0.2, 0.1],
        "win_probability": [np.nan, np.nan, np.nan],                 # blanked by the gate
        "diagnostic_win_probability": [0.20, 0.50, 0.30],
        "score_eligibility_reason_codes": ["calibration_unavailable_or_unaudited;unsupported_or_unknown_runtime_source"] * 3,
    })


def test_win_pct_text_prefers_the_valid_value_then_the_labelled_diagnostic():
    assert win_pct_text(0.285, 0.31) == ("Win %", "28.5%")
    label, text = win_pct_text(float("nan"), 0.285)
    assert text == "28.5%" and "Diagnostic" in label and "not valid for betting" in label
    assert win_pct_text(None, None) == ("Win %", "unavailable") and win_pct_text(float("nan"), "x")[1] == "unavailable"


def test_diagnostic_frame_only_when_every_valid_win_pct_is_blank():
    board = _blocked_board()
    assert is_diagnostic_only(board)
    frame = diagnostic_forecast_frame(board)
    assert list(frame["Horse"]) == ["B", "C", "A"] and list(frame["Rank"]) == [1, 2, 3]            # ranked by the diagnostic value
    assert list(frame["Diagnostic Win %"]) == [50.0, 30.0, 20.0]
    assert list(frame["Diff vs ML (pts)"]) == [30.0, 20.0, -13.3]
    assert list(frame.columns) == ["Rank", "Horse", "Post", "Morning Line", "Diagnostic Win %", "ML-Implied %", "Diff vs ML (pts)"]
    valid = board.assign(win_probability=[0.2, 0.5, 0.3])
    assert not is_diagnostic_only(valid) and diagnostic_forecast_frame(valid) is None            # a valid board is never relabelled
    assert diagnostic_forecast_frame(board.drop(columns="diagnostic_win_probability")) is None   # nothing stored -> nothing shown
    assert diagnostic_forecast_frame(None) is None and diagnostic_forecast_frame(board.iloc[0:0]) is None
    nothing = board.assign(diagnostic_win_probability=np.nan)
    assert diagnostic_forecast_frame(nothing) is None                                            # no fabricated numbers


def test_reason_codes_are_listed_once_in_first_seen_order_and_the_notice_says_not_for_betting():
    assert diagnostic_reason_codes(_blocked_board()) == ["calibration_unavailable_or_unaudited", "unsupported_or_unknown_runtime_source"]
    assert diagnostic_reason_codes(None) == [] and diagnostic_reason_codes(pd.DataFrame({"a": [1]})) == []
    assert "not valid for betting" in DIAGNOSTIC_NOTICE
