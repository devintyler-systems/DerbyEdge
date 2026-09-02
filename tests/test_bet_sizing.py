"""Tests for recommend_bet_size and BET_DENOMINATION table.

Covers the exact stake examples from the task spec plus denomination edge cases.
"""
from __future__ import annotations

import pytest

from src.derbyedge.odds_math import BET_DENOMINATION, recommend_bet_size


class TestBetDenomination:
    def test_denomination_table_keys_present(self):
        required = {"WIN", "PLACE", "SHOW", "EXACTA", "TRIFECTA", "SUPERFECTA", "DEFAULT"}
        assert required <= set(BET_DENOMINATION.keys())

    def test_win_min_and_step_are_two(self):
        min_bet, step = BET_DENOMINATION["WIN"]
        assert min_bet == 2.00
        assert step == 2.00

    def test_exacta_min_is_one(self):
        min_bet, step = BET_DENOMINATION["EXACTA"]
        assert min_bet == 1.00

    def test_trifecta_min_is_fifty_cents(self):
        min_bet, step = BET_DENOMINATION["TRIFECTA"]
        assert min_bet == 0.50

    def test_superfecta_min_is_ten_cents(self):
        min_bet, step = BET_DENOMINATION["SUPERFECTA"]
        assert min_bet == 0.10


class TestRecommendBetSize:
    # ── WIN bet type ────────────────────────────────────────────────────────────

    def test_win_below_minimum_is_pass(self):
        rec = recommend_bet_size(1.04, "WIN")
        assert rec["is_bettable"] is False
        assert rec["recommendation"] == "PASS"
        assert rec["rounded_stake"] == 0.0
        assert rec["min_bet"] == 2.00

    def test_win_2_30_rounds_to_2(self):
        rec = recommend_bet_size(2.30, "WIN")
        assert rec["is_bettable"] is True
        assert rec["rounded_stake"] == 2.00
        assert rec["recommendation"] == "$2.00"

    def test_win_5_90_rounds_down_to_4(self):
        # Conservative floor: 5.90 / 2 = 2 full steps → $4
        rec = recommend_bet_size(5.90, "WIN")
        assert rec["is_bettable"] is True
        assert rec["rounded_stake"] == 4.00

    def test_win_exactly_at_minimum(self):
        rec = recommend_bet_size(2.00, "WIN")
        assert rec["is_bettable"] is True
        assert rec["rounded_stake"] == 2.00

    def test_win_exactly_at_step_boundary(self):
        rec = recommend_bet_size(6.00, "WIN")
        assert rec["rounded_stake"] == 6.00

    def test_win_just_below_step_boundary(self):
        rec = recommend_bet_size(5.99, "WIN")
        assert rec["rounded_stake"] == 4.00

    def test_win_zero_stake_is_pass(self):
        rec = recommend_bet_size(0.0, "WIN")
        assert rec["is_bettable"] is False
        assert rec["recommendation"] == "PASS"

    # ── EXACTA bet type ─────────────────────────────────────────────────────────

    def test_exacta_1_41_rounds_to_1(self):
        rec = recommend_bet_size(1.41, "EXACTA")
        assert rec["is_bettable"] is True
        assert rec["rounded_stake"] == 1.00
        assert rec["recommendation"] == "$1.00"

    def test_exacta_below_minimum_is_pass(self):
        rec = recommend_bet_size(0.75, "EXACTA")
        assert rec["is_bettable"] is False
        assert rec["recommendation"] == "PASS"

    # ── TRIFECTA bet type ───────────────────────────────────────────────────────

    def test_trifecta_0_62_rounds_to_0_50(self):
        rec = recommend_bet_size(0.62, "TRIFECTA")
        assert rec["is_bettable"] is True
        assert rec["rounded_stake"] == 0.50

    def test_trifecta_below_minimum_is_pass(self):
        rec = recommend_bet_size(0.25, "TRIFECTA")
        assert rec["is_bettable"] is False
        assert rec["recommendation"] == "PASS"

    def test_trifecta_1_75_rounds_to_1_50(self):
        rec = recommend_bet_size(1.75, "TRIFECTA")
        assert rec["rounded_stake"] == 1.50

    # ── Case insensitivity ──────────────────────────────────────────────────────

    def test_bet_type_is_case_insensitive(self):
        rec_upper = recommend_bet_size(3.50, "WIN")
        rec_lower = recommend_bet_size(3.50, "win")
        assert rec_upper["rounded_stake"] == rec_lower["rounded_stake"]

    # ── Unknown bet type falls back to DEFAULT ──────────────────────────────────

    def test_unknown_bet_type_uses_default(self):
        rec = recommend_bet_size(1.50, "DAILY_DOUBLE")
        min_bet, step = BET_DENOMINATION["DEFAULT"]
        assert rec["min_bet"] == min_bet
        assert rec["step"] == step

    # ── Return dict structure ───────────────────────────────────────────────────

    def test_return_dict_has_all_keys(self):
        rec = recommend_bet_size(5.00, "WIN")
        required_keys = {"raw_stake", "rounded_stake", "min_bet", "step",
                         "is_bettable", "recommendation"}
        assert required_keys <= set(rec.keys())

    def test_raw_stake_is_preserved(self):
        rec = recommend_bet_size(3.14159, "WIN")
        assert rec["raw_stake"] == pytest.approx(3.1416, abs=1e-3)

    # ── Bankroll default → stake examples ──────────────────────────────────────

    def test_bankroll_100_default_examples(self):
        """Demonstrate expected outputs with $100 bankroll, 5/25 Kelly fraction."""
        # raw_stake = kelly_dollar; these match the live board examples in the spec
        assert recommend_bet_size(1.04, "WIN")["recommendation"] == "PASS"
        assert recommend_bet_size(2.30, "WIN")["rounded_stake"] == 2.00
        assert recommend_bet_size(1.41, "EXACTA")["rounded_stake"] == 1.00
