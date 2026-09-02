from __future__ import annotations

import ast
from pathlib import Path

from src.app.board_state import race_board_contract, source_feature_inventory
from src.ingest.run_state import RunMode, market_baseline_scores


ENTRIES = [
    {"horse_key": "A", "morning_line_decimal": 3.0},
    {"horse_key": "B", "morning_line_decimal": 5.0},
]


def test_baseline_scores_never_contain_model_or_betting_outputs():
    scores = market_baseline_scores(ENTRIES)
    assert abs(sum(score.p_ml_implied or 0 for score in scores) - 1.0) < 1e-12
    assert all(score.p_model is None for score in scores)
    assert all(score.fair_odds_decimal is None for score in scores)
    assert all(score.edge_vs_live_market is None for score in scores)
    assert all(score.bet_tag is None for score in scores)


def test_baseline_chart_has_exactly_one_orange_reference_series():
    contract = race_board_contract(RunMode.MARKET_BASELINE_ONLY)
    assert contract.chart_series == ("Morning-Line Implied Win Probability",)
    assert not contract.show_model_probability
    assert not contract.show_edge
    assert not contract.show_bet_tags
    assert not contract.show_stakes
    assert not contract.scoring_controls_enabled


def test_no_live_market_means_no_edge_tag_or_stake_controls():
    limited = race_board_contract(RunMode.MODEL_READY_LIMITED, has_live_odds=False)
    ready_without_market = race_board_contract(RunMode.MODEL_READY, has_live_odds=False)
    for contract in (limited, ready_without_market):
        assert not contract.show_edge
        assert not contract.show_bet_tags
        assert not contract.show_stakes


def test_pending_features_cannot_render_stale_model_artifacts():
    contract = race_board_contract(RunMode.PP_PARSED_FEATURES_PENDING)
    assert not contract.show_model_probability
    assert not contract.show_fair_odds
    assert not contract.scoring_controls_enabled
    assert contract.chart_series == ()


def test_source_coverage_is_inventory_not_an_average():
    inventory = source_feature_inventory({
        "recent_form": 1.0,
        "trip_flags": 0.89,
        "speed_figures": 0.0,
    })
    assert inventory == {
        "Available": ("Recent Form",),
        "Partial": ("Trip Flags (89%)",),
        "Missing": ("Speed Figures",),
    }


def test_no_direct_model_assignment_from_market_fields():
    source = Path("src/ingest/run_state.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        target_text = " ".join(ast.unparse(target) for target in targets)
        value_text = ast.unparse(value) if value is not None else ""
        if "p_model" in target_text and any(
            name in value_text for name in ("p_ml_implied", "p_market_live", "morning_line")
        ):
            forbidden.append((target_text, value_text))
    assert forbidden == []
