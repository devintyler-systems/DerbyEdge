from __future__ import annotations

import numpy as np
import pandas as pd

from src.app.board_formatting import (
    _edge_str,
    morning_line_str,
    pace_fit_str,
    prepare_probability_display_columns,
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
