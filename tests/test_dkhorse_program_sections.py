"""Text fixture regression coverage for DK Horse classic web-program PDFs.

The uploaded PDF is not stored in the repository because licensed program PDFs
are not test assets.  This fixture preserves the page, runner, PP, workout,
and navigation boundaries relevant to the parser contract.
"""
from datetime import date

from src.ingest.draftkings_pdf import (
    canonical_horse_name,
    detect_draftkings_source,
    extract_runner_sections,
    _section_pp_rows,
)
from src.ingest.run_state import DataQuality, RunMode, resolve_run_mode


PAGES = [
    """dkhorse.com/bet/program/classic/ind/r5
HORSESHOE INDIANAPOLIS PROGRAM RACE 5 12 HORSES
1 Blazing Bay, Gelding, 3 yrs (KY) M: 4/1
ALL RACES DIST RESULTS
Aug 20, '26 IND MSW 6 F DIRT FAST 4 3
dkhorse.com/bet/program/classic/ind/r5
""",
    """Blazing Bay, Gelding, 3 yrs (KY)
WORKOUTS
Aug 28, '26 IND 4 F 48.20 B
ALL RACES DIST RESULTS
Aug 02, '26 IND MSW 6 F DIRT FAST SCR
""",
    """2 Don't U Shush Me Gray or Roan, Filly, 3 yrs (IN) M: 6/1
ALL RACES DIST RESULTS
Jul 10, '26 IND MCL 6 F DIRT FAST 2 1
9 The Blue\nFactor Chestnut, Colt, 3 yrs (KY) M: 8/1
ALL RACES DIST RESULTS
Aug 10, '26 IND MSW 6 F DIRT FAST 5 4
3 McGinnis Bay, Gelding, 3 yrs (IN) M: 5/1
4 Fast Jimmy Bay, Colt, 3 yrs (KY) M: 7/1
5 Chatterfield Chestnut, Filly, 3 yrs (IN) M: 10/1
7 Copal Gray or Roan, Gelding, 3 yrs (KY) M: 12/1
9 Hot Ice Bay, Gelding, 3 yrs (IN) M: 9/1
12 Handsome Jimmy Bay, Colt, 3 yrs (KY) M: 15/1
13 Pezzonovante Chestnut, Gelding, 3 yrs (KY) M: 20/1
14 Greystar Gray or Roan, Gelding, 3 yrs (IN) M: 30/1
""",
]


def test_source_detection_requires_multiple_dk_signals():
    detected = detect_draftkings_source(PAGES[0] + "\nWORKOUTS", "IND_DK_Horse_R5_9-3-26.pdf")
    assert detected["detected"] is True
    assert detected["source_format"] == "dkhorse_program_pdf"
    assert "dkhorse_classic_url" in detected["source_detection_signals"]
    assert detect_draftkings_source("DK Horse", "upload.pdf")["detected"] is False
    assert detect_draftkings_source("1/ST BET PROGRAM WORKOUTS", "normal.pdf")["detected"] is False


def test_sections_span_pages_and_do_not_split_on_navigation_or_workouts():
    sections = extract_runner_sections(PAGES)
    assert [s.horse_name_raw for s in sections] == [
        "Blazing", "Don't U Shush Me", "The Blue Factor", "McGinnis", "Fast Jimmy",
        "Chatterfield", "Copal", "Hot Ice", "Handsome Jimmy", "Pezzonovante", "Greystar",
    ]
    assert sections[0].source_page_start == 1
    assert sections[0].source_page_end == 2
    assert sections[0].raw_text.count("dkhorse.com") == 0
    assert canonical_horse_name("Don't U Shush Me") == "dontushushme"
    assert canonical_horse_name("The Blue\nFactor") == "thebluefactor"
    assert canonical_horse_name("Greystar\nGray or Roan, Gelding") == "greystar"


def test_pps_are_linked_per_section_and_workouts_do_not_inflate_starts():
    sections = extract_runner_sections(PAGES)
    starts, workouts, scratches = _section_pp_rows(sections[0], date(2026, 9, 3), "fixture")
    assert len(starts) == 2
    assert len(workouts) == 1
    assert starts[0].horse_name == "Blazing"
    assert starts[1].is_scratch is True
    assert starts[1].finish_position is None
    assert len(scratches) == 1


def test_confirmed_late_scratch_uses_active_count_without_field_block():
    quality = DataQuality(11, 12, 9, 10 / 11, True, True, False, False,
                          active_entry_count=11, entries_scratched=1,
                          nonstarter_count=1, field_reconciliation_status="late_scratch_explained")
    assert resolve_run_mode(quality)[0] == RunMode.PP_PARSED_FEATURES_PENDING


def test_unexplained_field_mismatch_and_zero_experienced_pps_block():
    quality = DataQuality(11, 12, 0, 0.0, True, True, False, False,
                          active_entry_count=11, field_reconciliation_status="unexplained")
    mode, reasons = resolve_run_mode(quality)
    assert mode == RunMode.BLOCKED
    assert any("Declared field size" in reason for reason in reasons)


def test_skipped_program_number_is_not_inferred_as_a_scratch():
    quality = DataQuality(11, 12, 9, 10 / 11, True, True, False, False,
                          active_entry_count=11, nonstarter_count=0,
                          field_reconciliation_status="unexplained")
    mode, reasons = resolve_run_mode(quality)
    assert mode == RunMode.BLOCKED
    assert any("Declared field size" in reason for reason in reasons)
