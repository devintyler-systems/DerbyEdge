"""PP rows are read by their tab-separated cells where the flattened-text pattern cannot (KEE R11 10-31-26, Breeders' Cup day).

Overseas starts print rows the old pattern could not read (a bare "(4)" PRG cell, a blank ODDS cell, no distance cell) and, worse, read some
silently wrong: with program 0 and a printed price, the price was taken as the finish and the jockey as the lengths.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest import draftkings_markdown as dk
from src.ingest.draftkings_markdown import PP_NO_DISTANCE, _parse_history, _pp_row_from_cells
from src.ingest.race_bundle import parse_race_bundle
from src.derbyedge.tracks import resolve_track

ROOT = Path(__file__).resolve().parents[1]
KEE = ROOT / "tests" / "fixtures" / "KEE_Full_Race_Data_R11_10-31-26.md"


@pytest.fixture(scope="module")
def bundle():
    return parse_race_bundle(KEE.read_text(encoding="utf-8"), source_path="KEE_Full_Race-Data_R11_10-31-26.md",
                             as_of=datetime(2026, 10, 31, 21, 37, tzinfo=timezone.utc),
                             captured_at=datetime(2026, 10, 31, 21, 37, 49, tzinfo=timezone.utc))


def _row(horse_rows: str):
    """Parse one synthetic PP block through the real history parser."""
    entry = dk.Entry(program_number="1", horse_name="Test Horse", post_position=1, weight=126, jockey="J", trainer="T",
                     morning_line="5", medication_weight_equipment=None, horse_profile=dk.HorseProfile())
    block = "ALL RACES\tDIST\tSURF-COND\nREPLAY\nMay 1, '26\n\nACME TRACK\n\nSTK\n\n" + horse_rows + "\nWORKOUTS\n"
    pps, _w, _a, _b = _parse_history(entry, block)
    return pps[0]


def test_the_breeders_cup_card_now_validates_and_every_row_has_a_finish(bundle):
    assert bundle.validation.passed and not bundle.validation.errors, bundle.validation.errors
    assert all(p.finish_position is not None for p in bundle.card.past_performances)
    assert resolve_track(track_name="Keeneland Breeders Cup")["track_code"] == "KEE"


def test_rows_with_no_distance_cell_are_kept_named_in_a_warning_and_never_given_a_distance(bundle):
    omitted = [p for p in bundle.card.past_performances if p.distance_omitted_by_source]
    assert len(omitted) == 9 and all(p.distance is None and p.finish_position is not None for p in omitted)
    warning = next(w for w in bundle.validation.warnings if w.startswith(PP_NO_DISTANCE))
    assert "Ethical Diamond (1)" in warning and "Hotazhell (2)" in warning


def test_japanese_rows_with_a_bare_bracket_program_and_blank_odds_are_read(bundle):
    doha = next(p for p in bundle.card.past_performances if p.horse_name == "El Cordobes" and p.track == "DOHA")
    assert (doha.program_or_post, doha.odds, doha.finish_position, doha.beaten_lengths, doha.jockey) == ("(4)", None, 5, "4.25", "BuickW")
    kyoto = next(p for p in bundle.card.past_performances if p.track == "KYOTO" and p.beaten_lengths == "1.25")
    assert (kyoto.program_or_post, kyoto.odds, kyoto.finish_position, kyoto.jockey) == ("(3)", "9-2", 4, "Hamanaka S.")


def test_a_program_zero_row_with_a_printed_price_is_no_longer_misread(bundle):
    ascot = next(p for p in bundle.card.past_performances if p.horse_name == "Ethical Diamond" and p.track == "ASCOT" and p.finish_position == 9)
    assert (ascot.odds, ascot.finish_position, ascot.beaten_lengths, ascot.jockey) == ("9", 9, "9.25", "Browne McMonagleD")


def test_a_price_that_differs_from_the_finish_is_not_taken_for_the_finish():
    p = _row("1 1/2 M\tTURF-Good\t0\t7\t3\t2\tSmithJ\tKept on late")
    assert (p.odds, p.finish_position, p.beaten_lengths, p.jockey, p.comment) == ("7", 3, "2", "SmithJ", "Kept on late")


def test_ordinary_rows_and_scratch_rows_are_unchanged():
    ordinary = _row("6 F\tDIRT-Fast\t4 (3)\t8-5\t2\t1.25\tSmithJ\tLed, tired")
    assert (ordinary.program_or_post, ordinary.odds, ordinary.finish_position, ordinary.beaten_lengths) == ("4 (3)", "8-5", 2, "1.25")
    scratch = _row("6 F\tDIRT-Fast\tSCR\tSCR\tSCR\tSCR\tSmithJ\t")
    assert scratch.finish_position == "SCR" and scratch.program_or_post is None and scratch.jockey is None


def test_a_damaged_row_still_fails_loudly_instead_of_being_guessed():
    for damaged in ("6 F\tDIRT-Fast\t4\t8-5\tXX\t1.25\tSmithJ\tc",            # finish is not a number
                    "6 F\t\t4\t8-5\t2\t1.25\tSmithJ\tc",                       # surface cell blank
                    "6 F\tDIRT-Fast\t4"):                                      # too few cells
        assert _pp_row_from_cells(damaged) is None
    assert _row("6 F\tDIRT-Fast\t4\t8-5\tXX\t1.25\tSmithJ\tc").finish_position is None      # so validation reports it


def test_a_row_missing_its_distance_for_any_other_reason_is_still_an_error(bundle):
    text = KEE.read_text(encoding="utf-8").replace("1 1/2 M\tTURF-Firm\t6\t2\t2\t1.75\tMooreRL", "garbled\tTURF-Firm\t6\t2\t2\t1.75\tMooreRL", 1)
    b = parse_race_bundle(text, source_path="KEE_Full_Race-Data_R11_10-31-26.md", as_of=datetime(2026, 10, 31, 21, 37, tzinfo=timezone.utc),
                          captured_at=datetime(2026, 10, 31, 21, 37, 49, tzinfo=timezone.utc))
    assert not b.validation.passed
    assert any("materially incomplete PP row for Ethical Diamond" in e for e in b.validation.errors)
