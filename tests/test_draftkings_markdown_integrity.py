"""Tests for the fail-closed DraftKings Markdown raw-card lane."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest


def dataclasses_asdict(obj):
    return dataclasses.asdict(obj)

from src.ingest.draftkings_markdown import (
    DraftKingsMarkdownValidationError,
    parse_draftkings_markdown,
    require_scoring_ready,
    validated_feature_staging_records,
    validate_draftkings_markdown_card,
    reconcile_draftkings_excel,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R6_9-4-26.md"
EXCEL_FIXTURE = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R6_9-4-26.xlsx"
AS_OF = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


def test_fixture_extracts_race_entries_pps_and_workouts():
    card = parse_draftkings_markdown(FIXTURE, as_of=AS_OF)
    result = validate_draftkings_markdown_card(card)

    assert card.race.track == "Saratoga"
    assert card.race.race_number == 6
    assert card.race.race_date.isoformat() == "2026-09-04"
    assert card.race.surface == "dirt"
    assert card.race.normalized_distance_furlongs == 8.0
    assert card.entries
    assert card.entries[0].horse_name and card.entries[0].jockey
    assert card.entries[0].trainer and card.entries[0].weight == 122
    assert card.past_performances
    assert card.workouts
    assert result.passed, result.errors
    assert result.past_performance_row_count == len(card.past_performances)
    assert result.workout_row_count == len(card.workouts)


def test_historical_rows_before_as_of_are_accepted():
    card = parse_draftkings_markdown(FIXTURE, as_of=AS_OF)
    result = validate_draftkings_markdown_card(card)

    assert all(row.start_date < AS_OF.date() for row in card.past_performances)
    assert all(row.work_date < AS_OF.date() for row in card.workouts)
    assert not any("after as_of" in error for error in result.errors)


def test_truncated_runner_block_fails_closed():
    source = """Saratoga
RACE 6
Purse: $42K
3YO+
1 M
Dirt: Fast
PROGRAM
1
20
20
Horse One
L122
Jockey One
Trainer One
ALL RACES DIST SURF-COND PRG (PP) ODDS FIN BL JOCKEY COMMENT
Aug 1, '26
SARATOGA
CLM20000
1 M DIRT-Fast 1 2 1 0.5 Rider Saved ground
WORKOUTS DIST SURF-COND TIME RANK
Aug 2, '26
SARATOGA
4 F DIRT-Fast 49.20 B 1 of 10
"""
    card = parse_draftkings_markdown(source, source_path="truncated.md", as_of=AS_OF)
    result = validate_draftkings_markdown_card(card)

    assert not result.passed
    assert any("truncat" in error.lower() or "boundary" in error.lower() for error in result.errors)


def test_post_as_of_history_fails_leakage_validation():
    source = """Saratoga
RACE 6
Purse: $42K
3YO+
1 M
Dirt: Fast
PROGRAM
1
20
20
Horse One
L122
Jockey One
Trainer One
ALL RACES DIST SURF-COND PRG (PP) ODDS FIN BL JOCKEY COMMENT
Sep 5, '26
SARATOGA
CLM20000
1 M DIRT-Fast 1 2 1 0.5 Rider Saved ground
WORKOUTS DIST SURF-COND TIME RANK
Sep 5, '26
SARATOGA
4 F DIRT-Fast 49.20 B 1 of 10
SEE LESS
"""
    card = parse_draftkings_markdown(source, source_path="leakage.md", as_of=AS_OF)
    result = validate_draftkings_markdown_card(card)

    assert not result.passed
    assert sum("after as_of" in error for error in result.errors) == 2


def test_failed_card_cannot_cross_feature_generation_gate():
    card = parse_draftkings_markdown("Saratoga\nRACE 6\n", source_path="bad.md", as_of=AS_OF)
    result = validate_draftkings_markdown_card(card)

    with pytest.raises(DraftKingsMarkdownValidationError):
        validated_feature_staging_records(card, result)


def test_excel_reconciliation_is_optional_and_non_production():
    result = reconcile_draftkings_excel(EXCEL_FIXTURE)

    assert result.source_sha256
    assert result.worksheets_scanned >= 0
    assert isinstance(result.to_dict(), dict)


# --------------------------------------------------------------------------- #
# Del Mar (PP {n}-anchored layout) — runner-block retention hardening          #
# --------------------------------------------------------------------------- #
DMR_FIXTURE = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "DMR_DK_Horse_R10_9-7-26.md"
DMR_AS_OF = datetime(2026, 9, 7, 19, tzinfo=timezone.utc)


def test_del_mar_race_header_is_parsed():
    card = parse_draftkings_markdown(DMR_FIXTURE, as_of=DMR_AS_OF)
    assert card.race.track == "Del Mar"
    assert card.race.race_number == 10
    assert card.race.race_date.isoformat() == "2026-09-07"
    assert card.race.distance == "6 1/2 F"
    assert card.race.surface == "dirt"
    assert card.race.surface_condition == "Fast"
    # Post is shown as a minutes-to-post countdown, not a clock time.
    assert card.race.card_status == "MTP"
    assert card.race.post_time_display and "MTP" in card.race.post_time_display


def test_del_mar_retains_every_detectable_runner_block():
    card = parse_draftkings_markdown(DMR_FIXTURE, as_of=DMR_AS_OF)
    # 13 PP {n} anchors in the fixture; every one must survive segmentation.
    assert len(card.entries) == 13
    names = [e.horse_name for e in card.entries]
    assert "Its a Cinch" in names
    assert "Hondo Crouch" in names  # PP 2 — scratched, no ALL RACES / WORKOUTS
    assert all(e.horse_name for e in card.entries)
    assert [e.program_number for e in card.entries] == [str(n) for n in range(1, 14)]


def test_del_mar_first_runner_identity_is_populated():
    card = parse_draftkings_markdown(DMR_FIXTURE, as_of=DMR_AS_OF)
    first = card.entries[0]
    assert first.horse_name == "Its a Cinch"
    assert first.program_number == "1"
    assert first.post_position == 1
    assert first.jockey == "Edwin A. Maldonado"
    assert first.trainer == "O. J. Jauregui"
    assert first.horse_profile.sire == "Om"
    assert first.horse_profile.dam == "Tahitian Lagoon"
    assert first.horse_profile.sex and first.horse_profile.sex.lower() == "gelding"
    assert first.horse_profile.color == "Chestnut"
    assert first.horse_profile.age == 4
    assert first.medication_weight_equipment  # "L"
    assert first.breeder and "Harris Farms" in first.breeder
    assert first.owner and "Siegel" in first.owner
    assert "life" in first.record_splits
    assert first.has_all_races_section is True


def test_del_mar_runner_missing_all_races_is_retained_with_flag():
    card = parse_draftkings_markdown(DMR_FIXTURE, as_of=DMR_AS_OF)
    scratched = next(e for e in card.entries if e.horse_name == "Hondo Crouch")
    assert scratched.is_scratched is True
    assert scratched.has_all_races_section is False
    assert scratched.runner_retained_without_history is True
    # Identity is still present despite the missing history subsection.
    assert scratched.jockey == "Joe Bravo"
    assert scratched.trainer == "Mark Glatt"
    assert scratched.horse_profile.sire == "Grazen"
    assert scratched.horse_profile.dam == "Cherry Gold"


def test_del_mar_runner_missing_workouts_is_retained():
    card = parse_draftkings_markdown(DMR_FIXTURE, as_of=DMR_AS_OF)
    no_workouts = [e for e in card.entries if not e.has_workouts_section]
    # At least the scratched runner has no WORKOUTS block, and it is retained.
    assert any(e.horse_name == "Hondo Crouch" for e in no_workouts)
    for entry in no_workouts:
        assert entry.horse_name  # retained, not dropped


def test_del_mar_block_source_spans_are_present_and_ordered():
    card = parse_draftkings_markdown(DMR_FIXTURE, as_of=DMR_AS_OF)
    spans = [e.block_source_span for e in card.entries]
    assert all(s is not None and s[0] < s[1] for s in spans)
    assert spans == sorted(spans)


def test_malformed_pp_history_rows_warn_without_dropping_runner():
    source = """Del Mar
RACE 10
9
MTP
$20K STARTER ALLOWANCE
Purse: $75K
 3YO+
 6 1/2 F
Dirt: Fast
PROGRAM
#
ODDS
Runner
Jockey
Trainer
Sire / Dam
1

PP 1
5
M: 2
Test Horse One
Bay, Gelding, 4 yrs (CA) L
Real Jockey
Real Trainer
Some Sire
Some Dam
Test Horse One
Some Sire - Some Dam
Bay, Gelding, 4
BREEDER
A Breeder (CA)
OWNER
An Owner
ALL RACES	DIST	SURF-COND	PRG (PP)	ODDS	FIN	BL	JOCKEY	COMMENT
REPLAY
Aug 16, '26

DEL MAR

@@@ totally malformed row that cannot be parsed as a running line @@@
2

PP 2
7
M: 3
Test Horse Two
Gray, Filly, 3 yrs (KY)
Second Jockey
Second Trainer
Other Sire
Other Dam
"""
    card = parse_draftkings_markdown(source, source_path="malformed.md", as_of=DMR_AS_OF)
    names = [e.horse_name for e in card.entries]
    assert "Test Horse One" in names
    assert "Test Horse Two" in names
    assert len(card.entries) == 2
    runner_one = next(e for e in card.entries if e.horse_name == "Test Horse One")
    assert runner_one.runner_parse_warnings  # warned, not dropped
    assert any("could not be parsed" in w for w in runner_one.runner_parse_warnings)


def test_del_mar_parse_is_deterministic():
    a = parse_draftkings_markdown(DMR_FIXTURE, as_of=DMR_AS_OF)
    b = parse_draftkings_markdown(DMR_FIXTURE, as_of=DMR_AS_OF)
    assert [dataclasses_asdict(e) for e in a.entries] == [dataclasses_asdict(e) for e in b.entries]
    assert a.source_sha256 == b.source_sha256
    assert a.race == b.race


def test_del_mar_parse_creates_no_database(tmp_path, monkeypatch):
    import glob
    monkeypatch.chdir(tmp_path)
    before = set(glob.glob(str(tmp_path / "**" / "*.db"), recursive=True))
    parse_draftkings_markdown(DMR_FIXTURE, as_of=DMR_AS_OF)
    after = set(glob.glob(str(tmp_path / "**" / "*.db"), recursive=True))
    assert before == after == set()


def test_saratoga_compact_layout_still_parses_after_hardening():
    card = parse_draftkings_markdown(FIXTURE, as_of=AS_OF)
    result = validate_draftkings_markdown_card(card)
    assert card.race.track == "Saratoga"
    assert card.entries[0].horse_name == "Magnum's Macrobrst"
    assert card.entries[0].weight == 122
    assert card.past_performances and card.workouts
    assert result.passed, result.errors
