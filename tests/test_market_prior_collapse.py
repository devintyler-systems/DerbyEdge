from __future__ import annotations

import numpy as np
import pandas as pd

from src.app.board_state import (
    apply_live_odds_overlay,
    effective_board_mode,
    race_board_contract,
)
from src.ingest.run_state import RunMode
from src.models.trainer import TRAIN_CONFIGS
from src.services.model_independence import (
    MODEL_COLLAPSED_TO_ML_PRIOR,
    detect_market_prior_collapse,
    pre_market_signal_probabilities,
)


def _non_market_feature_frame() -> pd.DataFrame:
    """Small valid turf-route field with deliberately nonconstant PP features."""
    return pd.DataFrame({
        "speed_best_3": [82.0, 76.0, 70.0],
        "speed_last": [80.0, 75.0, 71.0],
        "beyer_last": [79.0, 73.0, 68.0],
        "form_cycle_idx": [0.80, 0.50, 0.25],
        "class_delta": [0.30, 0.10, -0.20],
        "horses_beaten_pct_last": [0.80, 0.50, 0.20],
        "career_win_pct": [0.25, 0.15, 0.05],
        "distance_fit": [0.90, 0.55, 0.20],
        "surface_fit": [0.85, 0.50, 0.15],
        "pace_fit_score": [0.75, 0.45, 0.25],
        "traffic_resilience_proxy": [0.70, 0.50, 0.30],
        "work_readiness_score": [0.60, 0.50, 0.40],
        "trainer_intent_proxy": [0.60, 0.45, 0.35],
        "finish_energy_proxy": [0.65, 0.40, 0.20],
        "market_implied_prob": [0.10, 0.30, 0.60],
        "morning_line_rank": [3, 2, 1],
        "publicness_score": [0.10, 0.50, 0.90],
        "public_underlay_penalty": [0.00, 0.10, 0.20],
        "morning_line_delta": [-0.2, 0.0, 0.2],
    })


def test_equal_pre_market_and_ml_vectors_fail_closed_and_forbid_actions():
    ml = np.array([0.50, 0.30, 0.20])
    collapse = detect_market_prior_collapse(ml, ml)
    board = pd.DataFrame({
        "p_model_pre_market": ml,
        "p_ml_implied": ml,
    })

    assert collapse.status == MODEL_COLLAPSED_TO_ML_PRIOR
    assert collapse.max_abs_delta == 0.0
    assert collapse.mean_abs_delta == 0.0
    assert effective_board_mode(RunMode.MODEL_READY, board, {}) == (
        RunMode.MARKET_ANCHORED_NOT_ACTIONABLE
    )

    contract = race_board_contract(RunMode.MARKET_ANCHORED_NOT_ACTIONABLE)
    assert contract.chart_series == ("Morning-Line Implied Win Probability",)
    assert not contract.show_model_probability
    assert not contract.show_fair_odds
    assert not contract.show_edge
    assert not contract.show_bet_tags
    assert not contract.show_stakes
    assert not contract.scoring_controls_enabled

    direct_assignment = detect_market_prior_collapse(
        np.array([0.55, 0.25, 0.20]), ml,
        displayed_model_assigned_from_market=True,
    )
    assert direct_assignment.status == MODEL_COLLAPSED_TO_ML_PRIOR


def test_non_market_signal_ignores_all_morning_line_features_and_remains_eligible():
    frame = _non_market_feature_frame()
    config = TRAIN_CONFIGS["turf_route"]
    p_signal = pre_market_signal_probabilities(frame, config)

    market_changed = frame.copy()
    market_changed["market_implied_prob"] = [0.98, 0.01, 0.01]
    market_changed["morning_line_rank"] = [1, 3, 2]
    market_changed["publicness_score"] = [0.99, 0.01, 0.50]
    market_changed["public_underlay_penalty"] = [1.0, 0.0, 0.8]
    market_changed["morning_line_delta"] = [10.0, -10.0, 5.0]

    assert np.isclose(p_signal.sum(), 1.0)
    assert np.all(np.isfinite(p_signal))
    assert np.allclose(
        p_signal, pre_market_signal_probabilities(market_changed, config)
    )

    p_ml = np.array([0.10, 0.30, 0.60])
    collapse = detect_market_prior_collapse(p_signal, p_ml)
    board = pd.DataFrame({
        "p_model_pre_market": p_signal,
        "p_ml_implied": p_ml,
    })
    assert not collapse.collapsed
    assert effective_board_mode(RunMode.MODEL_READY_LIMITED, board, {}) == (
        RunMode.MODEL_READY_LIMITED
    )

    contract = race_board_contract(RunMode.MODEL_READY_LIMITED, has_live_odds=False)
    assert contract.chart_series == ("Model Win %", "Morning-Line Implied %")
    assert not contract.show_fair_odds
    assert not contract.show_edge
    assert not contract.show_bet_tags
    assert not contract.show_stakes


def test_complete_live_snapshot_uses_pre_market_model_for_edge_not_ml():
    board = pd.DataFrame({
        "entry_id": [101, 102],
        "post_position": [1, 2],
        "win_probability": [0.60, 0.40],
        "p_model_pre_market": [0.60, 0.40],
        "market_implied_prob": [0.45, 0.55],
        "value_score": [None, None],
        "bet_tag": [None, None],
        "confidence_flag": [1, 1],
    })
    live = {
        1: {"entry_id": 101, "decimal_odds": 3.0},
        2: {"entry_id": 102, "decimal_odds": 2.0},
    }

    result = apply_live_odds_overlay(
        board, live, bet_edge_threshold=0.025, underlay_edge_threshold=-0.015
    )

    assert result.available
    assert result.board["p_market_live"].round(6).tolist() == [0.4, 0.6]
    assert result.board["edge_vs_live_market"].round(6).tolist() == [0.2, -0.2]
    assert result.board["value_score"].round(6).tolist() == [0.2, -0.2]
