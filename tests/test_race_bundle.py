"""One-file race bundle (DK Advanced + TwinSpires + DK Basic): the CT R7 acceptance race."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.app.race_import import dispatch_primary_race_card_import, markdown_import_summary
from src.ingest.draftkings_basic_grid import BasicGridError, parse_basic_grid
from src.ingest.race_bundle import is_race_bundle, locate_sections, parse_race_bundle
from src.services.draftkings_markdown_intake import (
    markdown_card_score_readiness, persist_validated_draftkings_markdown,
)
from src.services.race_bundle_intake import persist_race_bundle_extras
from src.services.twinspires_intake import twinspires_pace_for_card

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "CT_Full_Race_Data_R7_10-8-26.md"
NAME = FIXTURE.name
AS_OF = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
BEFORE_POST = datetime(2026, 10, 8, 22, 30, tzinfo=timezone.utc)   # 6:30 PM ET, post 7:02 PM ET
AFTER_POST = datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc)

SCRATCHES = {"3": "Run of the House", "6": "Sharpasadiamond", "8": "Glint", "11": "We Ready"}
WEIGHTS = {"1": 122, "2": 119, "5": 117}


def _text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _bundle(text: str | None = None, captured_at: datetime = BEFORE_POST, **kw):
    return parse_race_bundle(text or _text(), source_path=NAME, as_of=AS_OF, captured_at=captured_at, **kw)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = "\n".join(
        ln for ln in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in ln
    )
    conn.executescript(schema)
    return conn


def _persist(bundle):
    conn = _db()
    result = persist_validated_draftkings_markdown(
        conn, bundle.card, bundle.validation, source_filename=NAME,
        scheduled_post_utc=bundle.post_utc, captured_at=bundle.captured_at,
    )
    return conn, result


def test_three_sections_are_located_by_their_headers():
    sections, errors, _ = locate_sections(_text())
    assert not errors
    assert set(sections) == {"dk_advanced", "twinspires", "dk_basic"}
    assert is_race_bundle(_text())


def test_ct_race_identity_comes_from_the_pasted_text_and_filename():
    race = _bundle().card.race
    assert (race.track, race.race_number, str(race.race_date)) == ("Charles Town", 7, "2026-10-08")


def test_ct_bundle_passes_with_twelve_entries_eight_starters_and_four_scratches():
    b = _bundle()
    assert b.validation.passed, b.validation.errors
    assert len(b.card.entries) == 12
    assert b.validation.parsed_unique_runner_count == 8
    assert b.validation.scratched_runner_count == 4
    assert {e.program_number: e.horse_name for e in b.card.entries if not e.is_active} == SCRATCHES


def test_weights_come_from_the_basic_grid():
    b = _bundle()
    by_program = {e.program_number: e.weight for e in b.card.entries}
    assert all(by_program[p] == w for p, w in WEIGHTS.items())
    assert all(w is not None for w in by_program.values())
    assert b.reconciliation.weights_merged == 12
    assert not b.reconciliation.conflicts


def test_post_time_is_utc_and_capture_is_pre_post():
    b = _bundle()
    assert (b.post_utc, b.post_source) == ("2026-10-08T23:02:00+00:00", "CLOCK")
    assert not b.late_capture
    assert _bundle(captured_at=AFTER_POST).late_capture


def test_persist_marks_scratches_and_counts_only_starters():
    b = _bundle()
    conn, result = _persist(b)
    rows = conn.execute("SELECT program_number, scratch_flag FROM entries ORDER BY post_position").fetchall()
    assert len(rows) == 12
    assert {r["program_number"] for r in rows if r["scratch_flag"] == 1} == set(SCRATCHES)
    assert result.persisted_runner_count == 8
    assert conn.execute("SELECT field_size FROM race_cards").fetchone()[0] == 8
    assert conn.execute("SELECT scheduled_post_time_utc FROM race_cards").fetchone()[0] == b.post_utc
    assert markdown_card_score_readiness(conn, result.card_id).score_eligible


def test_twinspires_lane_and_pre_post_market_snapshot_are_admitted():
    b = _bundle()
    conn, result = _persist(b)
    extras = persist_race_bundle_extras(conn, b, result.card_id)
    assert extras.twinspires_status == "PASS"
    assert extras.market_snapshot_status == "CAPTURED" and extras.market_snapshot_rows == 8
    records, as_of, error = twinspires_pace_for_card(conn, result.card_id)
    assert error is None and len(records) == 8 and as_of == b.captured_at.isoformat()


def test_late_capture_is_flagged_and_excluded_from_market_and_pace():
    b = _bundle(captured_at=AFTER_POST)
    assert b.validation.passed
    assert any("LATE_CAPTURE" in w for w in b.validation.warnings)
    conn, result = _persist(b)
    extras = persist_race_bundle_extras(conn, b, result.card_id)
    assert extras.market_snapshot_status == "SKIPPED_LATE_CAPTURE"
    assert twinspires_pace_for_card(conn, result.card_id)[2] == "TwinSpires source is not proven pre-post"


def test_section_order_and_line_endings_do_not_matter():
    text = _text()
    sections, _, lines = locate_sections(text)
    chunk = {k: "\n".join(lines[a:b]) for k, (a, b) in sections.items()}
    shuffled = "\n".join([chunk["dk_basic"], chunk["twinspires"], chunk["dk_advanced"]]) + "\n"
    for variant in (shuffled, shuffled.replace("\n", "\r\n")):
        b = _bundle(variant)
        assert b.validation.passed, b.validation.errors
        assert b.validation.parsed_unique_runner_count == 8


@pytest.mark.parametrize("kind", ["twinspires", "dk_basic"])
def test_a_missing_section_fails_loudly(kind):
    sections, _, lines = locate_sections(_text())
    a, e = sections[kind]
    text = "\n".join(lines[:a] + lines[e:])
    result = dispatch_primary_race_card_import(NAME, text.encode(), as_of=AS_OF, captured_at=BEFORE_POST)
    assert result.bundle is not None and not result.feature_staging_allowed
    assert any(f"missing the {kind} section" in err for err in result.validation.errors)


def test_a_duplicated_section_fails_loudly():
    sections, _, lines = locate_sections(_text())
    a, e = sections["twinspires"]
    b = _bundle("\n".join(lines + lines[a:e]))
    assert not b.validation.passed
    assert any("2 twinspires sections" in err for err in b.validation.errors)


def _mutated(old: str, new: str, section: str) -> str:
    sections, _, lines = locate_sections(_text())
    a, e = sections[section]
    out = list(lines)
    hits = [i for i in range(a, e) if out[i].strip() == old]
    assert hits, old
    out[hits[0]] = new
    return "\n".join(out)


def test_a_tampered_morning_line_in_twinspires_is_a_named_conflict():
    b = _bundle(_mutated("M: 9/2", "M: 5", "twinspires"))
    assert not b.validation.passed
    assert any("program 4 morning_line" in err and "TwinSpires" in err for err in b.validation.errors)
    assert b.reconciliation.conflicts[0]["field"] == "morning_line"


def test_a_horse_swapped_in_the_basic_grid_is_a_named_conflict():
    b = _bundle(_mutated("Fortunate Son", "Some Other Horse", "dk_basic"))
    assert not b.validation.passed
    assert any("program 7 horse" in err for err in b.validation.errors)


def test_a_scratch_disagreement_is_a_named_conflict():
    b = _bundle(_mutated("SCR", "5", "dk_basic"))
    assert any("scratched" in err and "DK Basic" in err for err in b.validation.errors)


def test_a_race_pasted_from_elsewhere_is_blocked():
    other = (ROOT / "draftkings_racedata_pdfs" / "fixtures" / "PRM_TwinSpires__R7_Summary_9-13-26.md").read_text()
    sections, _, lines = locate_sections(_text())
    a, e = sections["twinspires"]
    b = _bundle("\n".join(lines[:a] + other.splitlines() + lines[e:]))
    assert not b.validation.passed
    assert any("missing from" in err or "differs" in err for err in b.validation.errors)


def test_dispatch_routes_a_bundle_through_the_markdown_lane():
    d = dispatch_primary_race_card_import(NAME, FIXTURE.read_bytes(), as_of=AS_OF, captured_at=BEFORE_POST)
    assert d.feature_staging_allowed and d.bundle is not None
    summary = markdown_import_summary(d)
    assert summary["parsed_runner_count"] == 8 and summary["scratched_runner_count"] == 4
    assert summary["bundle"]["post_time_utc"] == "2026-10-08T23:02:00+00:00"


def test_basic_grid_rejects_a_missing_cell():
    sections, _, lines = locate_sections(_text())
    a, e = sections["dk_basic"]
    body = lines[a:e]
    victim = next(i for i, l in enumerate(body) if l.strip() == "Shane Meyers")
    with pytest.raises(BasicGridError):
        parse_basic_grid("\n".join(body[:victim] + body[victim + 1:]))


# ---- BEL R5 10-9-26: a TwinSpires "expert 1st pick" tag above a scratched runner's name ------------------
BEL = ROOT / "tests" / "fixtures" / "BEL_Full_Race_Data_R5_10-9-26.md"


def test_expert_pick_tag_is_not_read_as_the_horse_name_of_a_scratched_runner():
    captured = datetime(2026, 10, 9, 19, 10, 39, tzinfo=timezone.utc)
    b = parse_race_bundle(BEL.read_text(encoding="utf-8"), source_path=BEL.name, as_of=captured.replace(second=0, microsecond=0),
                          captured_at=captured)
    assert not b.reconciliation.conflicts and b.validation.passed, (b.reconciliation.conflicts, b.validation.errors)
    assert b.validation.parsed_unique_runner_count == 9 and b.validation.scratched_runner_count == 1
    sc = b.twinspires.scratched
    assert [(r.program_number, r.horse_name) for r in sc] == [("9", "Pineapple Man")]
    assert {r.program_number: r.horse_name for r in b.twinspires.records}["10"] == "Peek"
    assert len(b.twinspires.records) == 9


def test_expert_tag_on_a_scratch_row_with_and_without_a_place_cell():
    from src.ingest.twinspires_markdown import _record_from_chunk
    tagged = _record_from_chunk("9", ["SCR", "M: 7/2", "-", "expert 1st pick", "Pineapple Man", "7", "E8"])
    plain = _record_from_chunk("9", ["SCR", "M: 7/2", "-", "Pineapple Man", "7", "E8"])
    assert tagged.horse_name == plain.horse_name == "Pineapple Man" and tagged.scratched
