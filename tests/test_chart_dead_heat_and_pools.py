"""Equibase chart parser: dead heats and fields too small for a show pool (MNR / SA 10-11-26)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.ingest.equibase_chart import parse_chart_pdf

FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def mnr():
    return {r.race_number: r for r in parse_chart_pdf(FIX / "eqb_MNR_2026-10-11_fullcard.pdf").races}


@pytest.fixture(scope="module")
def sa():
    return {r.race_number: r for r in parse_chart_pdf(FIX / "eqb_SA_2026-10-11_fullcard.pdf").races}


def test_every_race_on_both_cards_is_valid(mnr, sa):
    assert len(mnr) == 8 and len(sa) == 10
    assert [r.race_key for r in [*mnr.values(), *sa.values()] if not r.valid] == []


def test_a_dead_heat_lists_a_place_twice_and_the_purse_still_adds_up(sa):
    r = sa[7]                                                   # "3rd $5,850, 3rd $5,850" in the Value of Race line
    assert "DEAD_HEAT" in r.flags
    assert r.value_of_race == 67000 and r.purse_listed_total == 67000
    assert r.problems == []


def test_dead_heat_horses_share_the_place_and_the_next_finisher_is_fifth(sa):
    r = sa[7]
    assert [(s.program, s.finish_order) for s in r.starters[:5]] == [("5", 1), ("6", 2), ("1", 3), ("3", 3), ("2", 5)]
    both_third = [s for s in r.starters if s.finish_order == 3]
    assert [s.show_payoff for s in both_third] == [2.10, 3.80]  # each tied horse has its own show price
    assert all(r.purse_by_place[3] == 5850 for _ in both_third)


def test_mnr_r5_dead_heat_for_third(mnr):
    r = mnr[5]
    assert "DEAD_HEAT" in r.flags and r.purse_listed_total == r.value_of_race == 8820
    assert [(s.program, s.finish_order) for s in r.starters[:5]] == [("8", 1), ("5", 2), ("6", 3), ("9", 3), ("1", 5)]


def test_a_five_horse_field_pays_only_win_and_place_and_the_third_finisher_has_no_amounts(mnr):
    r = mnr[6]
    assert r.pools_paid == 2 and r.problems == []
    s = {x.program: x for x in r.starters}
    assert (s["1"].win_payoff, s["1"].place_payoff, s["1"].show_payoff) == (7.00, 3.20, None)
    assert (s["4"].win_payoff, s["4"].place_payoff, s["4"].show_payoff) == (None, 3.60, None)
    assert (s["5"].win_payoff, s["5"].place_payoff, s["5"].show_payoff) == (None, None, None)
    assert [p.wager_type for p in r.payoffs if p.parsed] == ["Perfecta", "Superfecta", "Trifecta"]


def test_a_normal_three_pool_race_is_unchanged(mnr):
    r = mnr[7]
    assert r.pools_paid == 3 and "DEAD_HEAT" not in r.flags
    s = r.starters[0]
    assert (s.win_payoff, s.place_payoff, s.show_payoff) == (17.6, 7.0, 2.4)
