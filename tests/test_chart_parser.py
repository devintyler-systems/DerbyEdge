"""P1 gate: Equibase full-card chart parser (28 races / 3 cards of retained Churchill Downs charts)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.ingest.equibase_chart import (
    PARSER_VERSION, _parse_row, distance_to_furlongs, extract_chart_text, parse_chart_pdf, parse_chart_text,
)
from src.services.equibase_result_slicer import slice_result_pdf

ROOT = Path(__file__).resolve().parents[1]
PDFS = sorted((ROOT / "data" / "raw" / "historical_results").glob("2026/04/*/eqb_CD_*_fullcard.pdf"))
EXPECTED_RACES = {"2026-04-25": 10, "2026-04-26": 9, "2026-04-28": 9}


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@pytest.fixture(scope="module")
def cards():
    assert len(PDFS) == 3, PDFS
    return {p.name: (p, parse_chart_pdf(p)) for p in PDFS}


@pytest.fixture(scope="module")
def texts():
    return {p.name: extract_chart_text(p.read_bytes()) for p in PDFS}


def _all_races(cards):
    return [r for _p, c in cards.values() for r in c.races]


# ---- the gate ------------------------------------------------------------------------------------

def test_all_28_races_are_found_and_valid(cards):
    for name, (_p, card) in cards.items():
        day = re.search(r"(\d{4}-\d{2}-\d{2})", name).group(1)
        assert len(card.races) == EXPECTED_RACES[day], name
        assert card.errors == [], card.errors
    races = _all_races(cards)
    assert len(races) == 28
    bad = {r.race_key: r.problems for r in races if not r.valid}
    assert not bad, bad


def test_race_count_and_winner_agree_with_the_independent_slicer_28_of_28(cards):
    agree = 0
    for _name, (path, card) in cards.items():
        refs = {r.race_number: r for r in slice_result_pdf(path)}
        assert sorted(refs) == [r.race_number for r in card.races]
        for race in card.races:
            ref = refs[race.race_number]
            assert ref.candidate_key == race.race_key
            assert _squash(ref.winner_name) == _squash(race.starters[0].horse_name), race.race_key
            agree += 1
    assert agree == 28


def test_winner_odds_equal_the_win_payoff_over_two_minus_one_28_of_28(cards):
    for race in _all_races(cards):
        w = race.starters[0]
        assert w.win_payoff is not None, race.race_key
        assert w.win_payoff / 2 - 1 == pytest.approx(w.odds_to_one, abs=0.011), race.race_key
        assert w.decimal_odds == pytest.approx(w.odds_to_one + 1)
        assert w.decimal_odds == pytest.approx(w.win_payoff / 2, abs=0.011)


def test_payoff_table_lists_the_first_three_finishers_in_official_order(cards):
    for race in _all_races(cards):
        s = race.starters
        assert (s[0].win_payoff, s[0].place_payoff, s[0].show_payoff) == tuple(
            x for x in (s[0].win_payoff, s[0].place_payoff, s[0].show_payoff)) and None not in (
            s[0].win_payoff, s[0].place_payoff, s[0].show_payoff), race.race_key
        assert s[1].win_payoff is None and s[1].place_payoff is not None and s[1].show_payoff is not None, race.race_key
        if len(s) > 2:
            assert s[2].place_payoff is None and s[2].show_payoff is not None, race.race_key
        assert all(x.show_payoff is None for x in s[3:]), race.race_key


def test_every_starter_has_unique_program_trainer_and_a_positive_to_one_price(cards):
    for race in _all_races(cards):
        programs = [s.program for s in race.starters]
        assert len(set(programs)) == len(programs) >= 4, race.race_key
        assert [s.finish_order for s in race.starters] == list(range(1, len(programs) + 1))
        assert all(s.trainer and s.odds_to_one > 0 and s.weight >= 100 for s in race.starters), race.race_key


def test_conditions_distance_surface_time_are_populated_on_every_race(cards):
    furlongs = set()
    for race in _all_races(cards):
        assert race.distance_furlongs and race.surface in {"dirt", "turf"}, race.race_key
        assert race.final_time_seconds and 45 < race.final_time_seconds < 220, race.race_key
        assert race.track_condition in {"Fast", "Firm"} and race.purse and race.race_type
        assert race.track_code == "CD" and race.race_date is not None
        furlongs.add(race.distance_furlongs)
    assert furlongs == {4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 10.0}


# ---- spot checks against the printed charts -------------------------------------------------------

def test_cd_april_25_race_2_matches_the_printed_chart(cards):
    race = next(r for r in cards["eqb_CD_2026-04-25_fullcard.pdf"][1].races if r.race_number == 2)
    w = race.starters[0]
    assert (w.program, w.horse_name, w.jockey, w.weight, w.post_position) == ("1", "Sargent Bilko", "Ortiz, Jose", 119, 1)
    assert (w.odds_to_one, w.favorite, w.win_payoff, w.place_payoff, w.show_payoff) == (1.01, True, 4.02, 2.42, 2.12)
    assert w.trainer == "Kenneally, Eddie" and w.owner == "Red Gate Racing"
    assert race.starters[2].last_raced == "9Apr26 1KEE3" and race.starters[1].last_raced is None
    assert (race.distance_furlongs, race.surface, race.purse, race.final_time, race.final_time_seconds) == (
        4.5, "dirt", 92000, "51.50", 51.5)
    assert race.fractional_times == ["22.74", "45.52"] and race.win_pool_total == 291142
    assert [(x.horse_name, x.reason, x.also_eligible) for x in race.scratches] == [("Be My Pal", "PrivVet-Illness", False)]
    exacta = next(p for p in race.payoffs if p.wager_type == "Exacta")
    assert (exacta.winning_numbers, exacta.payoff, exacta.pool) == ("1-5", 9.64, 178314)
    assert next(p for p in race.payoffs if p.wager_type == "Superfecta").winning_numbers == "1-5-7-4"


def test_purse_by_place_adds_up_to_the_race_value_on_every_race_including_wrapped_lines(cards):
    for race in _all_races(cards):
        assert race.value_of_race and sum(race.purse_by_place.values()) == race.value_of_race, race.race_key
    race = next(r for r in cards["eqb_CD_2026-04-25_fullcard.pdf"][1].races if r.race_number == 6)   # the line wraps here
    assert race.value_of_race == 34510 and len(race.purse_by_place) > 8 and race.purse_by_place[1] == 17808
    r2 = next(r for r in cards["eqb_CD_2026-04-25_fullcard.pdf"][1].races if r.race_number == 2)
    assert r2.purse_by_place == {1: 52096, 2: 18400, 3: 9200, 4: 4600, 5: 2760, 6: 1308, 7: 1164}


def test_horse_names_keep_their_spaces(cards):
    names = {s.horse_name for r in _all_races(cards) for s in r.starters}
    assert {"Sargent Bilko", "Uncle With Money", "Michael's Cove", "Plaza Athenee (GB)"} <= names
    # a run-together name would show a lower->upper join inside a single word ("UncleWithMoney")
    assert not [n for n in names if re.search(r"[a-z][A-Z]", n) and " " not in n and not re.match(r"(?:Mc|Mac)", n)]


def test_a_disqualification_moves_the_horse_and_the_payoffs_follow_the_official_order(cards):
    race = next(r for r in cards["eqb_CD_2026-04-28_fullcard.pdf"][1].races if r.race_number == 8)
    assert [s.program for s in race.starters] == ["7", "5", "4", "1", "6", "2", "8", "3"]
    uscar = next(s for s in race.starters if s.horse_name == "This Is Uscar")
    assert (uscar.disqualified, uscar.run_order, uscar.placed_from, uscar.finish_order) == (True, 3, 3, 4)
    assert "ORDER_CHANGED_BY_STEWARDS" in race.flags
    sf = next(p for p in race.payoffs if p.wager_type == "Superfecta")
    assert sf.winning_numbers == "7-5-4-1"                       # official order, not as run
    assert race.starters[2].horse_name == "Furio" and race.starters[2].show_payoff == 4.94
    pick3 = next(p for p in race.payoffs if p.wager_type == "Pick 3")
    assert (pick3.winning_numbers, pick3.payoff, pick3.pool) == ("7-13-7", 116.68, 34987)


def test_also_eligibles_and_wrapped_scratch_lines_are_read(cards):
    all_scr = [(r.race_key, s) for r in _all_races(cards) for s in r.scratches]
    ae = [s.horse_name for _k, s in all_scr if s.also_eligible]
    assert {"Birkin Elegance", "Click", "Alter Boy"} <= set(ae)
    wrapped = next(s for _k, s in all_scr if s.horse_name == "Dewy's Denali")   # name broken across two printed lines
    assert wrapped.reason == "Trainer" and not wrapped.also_eligible
    assert next(s for _k, s in all_scr if s.horse_name == "Stefan's Title").reason == "PrivVet-Illness"


def test_foreign_last_start_without_a_race_number_is_read(cards):
    race = next(r for r in cards["eqb_CD_2026-04-26_fullcard.pdf"][1].races if r.race_number == 9)
    s = next(x for x in race.starters if x.horse_name == "Plaza Athenee (GB)")
    assert s.last_raced == "13Aug25 GOW1" and s.jockey == "Curtis, Ben"


def test_a_coupled_entry_program_is_kept():
    row = _parse_row("--- 1A Foo Bar (Smith, John) 120 L b 2 3 11 1/2 21 2.50* chased, held", 2)
    assert (row.program, row.horse_name, row.jockey, row.weight, row.post_position) == ("1A", "Foo Bar", "Smith, John", 120, 2)
    assert (row.odds_to_one, row.favorite, row.medication_equipment, row.comment) == (2.5, True, "Lb", "chased, held")


@pytest.mark.parametrize("words,furlongs", [
    ("Six Furlongs", 6.0), ("Four And One Half Furlongs", 4.5), ("One Mile", 8.0),
    ("One And One Sixteenth Miles", 8.5), ("One And One Fourth Miles", 10.0), ("One And One Eighth Miles", 9.0),
    ("Seven And One Half Furlongs", 7.5), ("Five And One Half Furlongs", 5.5),
    ("One Mile And Seventy Yards", 8 + 70 / 220), ("Six And Three Fourths Furlongs", 6.75),
    ("About Six Furlongs", 6.0),
])
def test_distance_words_to_furlongs(words, furlongs):
    assert distance_to_furlongs(words) == pytest.approx(furlongs, abs=1e-3)


@pytest.mark.parametrize("words", ["", "Several Furlongs", "Six", "Six Parsecs", "Half Furlongs"])
def test_unrecognised_distances_are_none_not_guessed(words):
    assert distance_to_furlongs(words) is None


# ---- the self-checks must fire --------------------------------------------------------------------

def _first_day(texts):
    return texts["eqb_CD_2026-04-25_fullcard.pdf"]


def test_deterministic_and_provenance(cards):
    path, card = cards["eqb_CD_2026-04-25_fullcard.pdf"]
    again = parse_chart_pdf(path)
    assert again == card and card.parser_version == PARSER_VERSION and len(card.source_sha256) == 64


def test_a_tampered_winner_price_is_caught(texts):
    t = _first_day(texts).replace("1.01* took", "3.01* took", 1)
    race = next(r for r in parse_chart_text(t).races if r.race_number == 2)
    assert not race.valid and any("disagree with the" in p for p in race.problems)


def test_a_tampered_winner_line_is_caught(texts):
    t = _first_day(texts).replace("Winner: Sargent Bilko", "Winner: Happy Away", 1)
    race = next(r for r in parse_chart_text(t).races if r.race_number == 2)
    assert any("is not the chart winner" in p for p in race.problems)


def test_a_payoff_table_out_of_order_is_caught(texts):
    t = _first_day(texts)
    t = t.replace("1 Sargent Bilko 4.02 2.42 2.12", "9 Sargent Bilko 4.02 2.42 2.12", 1)
    race = next(r for r in parse_chart_text(t).races if r.race_number == 2)
    assert any("payoff table row 1" in p for p in race.problems)


def test_a_garbled_finish_row_is_reported_not_skipped(texts):
    t = _first_day(texts).replace("--- 8 Mo Surprises (Sheehy, Danny) 119 b 7 6 41 1/2 52 1/2 52 1/4 18.54", "--- 8 Mo Surprises Sheehy 119", 1)
    race = next(r for r in parse_chart_text(t).races if r.race_number == 2)
    assert any("unparsed finish-table line" in p for p in race.problems)


def test_a_missing_trainer_line_is_caught(texts):
    t = _first_day(texts).replace("Trainers: 1 - Kenneally, Eddie;", "Trainers:", 1)
    race = next(r for r in parse_chart_text(t).races if r.race_number == 2)
    assert any("no trainer for program" in p for p in race.problems)


def test_unknown_track_text_and_empty_input_fail_loudly(texts):
    t = _first_day(texts).replace("CHURCHILL DOWNS - April 25, 2026 - Race 2", "ZZYZX DOWNS - April 25, 2026 - Race 2", 1)
    race = next(r for r in parse_chart_text(t).races if r.race_number == 2)
    assert any("not in the track registry" in p for p in race.problems)
    assert parse_chart_text("not a chart").errors == ["no race headers found (expected 'TRACK - Month D, YYYY - Race N')"]


def test_a_duplicated_race_and_a_missing_race_are_card_errors(texts):
    t = _first_day(texts)
    block = t[t.index("CHURCHILL DOWNS - April 25, 2026 - Race 3"):t.index("CHURCHILL DOWNS - April 25, 2026 - Race 4")]
    assert any("appears more than once" in e for e in parse_chart_text(t + "\n" + block).errors)
    skipped = t.replace(block, "")
    assert any("not consecutive" in e for e in parse_chart_text(skipped).errors)
