"""TwinSpires SPEED / CLASS / PACE tabs: strict parse, optional bundle sections, raw + parsed storage, Profit Line."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest.race_bundle import locate_sections, parse_race_bundle
from src.ingest.twinspires_markdown import parse_twinspires_markdown
from src.ingest.twinspires_tabs import TwinSpiresTabError, distance_category, parse_twinspires_tab
from src.services.draftkings_markdown_intake import persist_validated_draftkings_markdown
from src.services.race_bundle_intake import bundle_summary, persist_race_bundle_extras

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
MNR = FIX / "MNR_Full_Race_Data_R5_10-12-26.md"
TABS = (FIX / "TS_Tabs_MNR_R5_10-12-26.md").read_text(encoding="utf-8")
AS_OF = datetime(2026, 10, 13, 0, 32, tzinfo=timezone.utc)
CAPTURED = datetime(2026, 10, 13, 0, 32, 35, tzinfo=timezone.utc)      # after the 5:40 PM ET post: a late capture


def _split_tabs() -> dict[str, str]:
    """The fixture holds the three tabs back to back; cut it at each '#' header row."""
    parts = TABS.split("#\nAll\n")
    return {k: "#\nAll\n" + p for k, p in zip(("ts_speed", "ts_class", "ts_pace"), parts[1:])}


def _bundle(extra: str = "", base: str | None = None):
    text = (base if base is not None else MNR.read_text(encoding="utf-8")) + "\n" + extra
    return parse_race_bundle(text, source_path=MNR.name, as_of=AS_OF, captured_at=CAPTURED)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("\n".join(
        l for l in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in l))
    return conn


# ---- parser -----------------------------------------------------------------------------------------------
def test_each_tab_parses_values_ranks_and_style_as_printed():
    tabs = {k: parse_twinspires_tab(t, k) for k, t in _split_tabs().items()}
    speed = {r.program_number: r for r in tabs["ts_speed"].runners}
    assert len(speed) == 12
    g = speed["2"]
    assert (g.horse_name, g.post_position, g.morning_line, g.run_style_raw, g.run_style_code, g.run_style_points) == \
        ("Greyhound Station", 2, "7", "E 6", "E", 6)
    assert g.values == {"average_speed": 55.0, "average_distance": 41.0, "best_speed": 37.0}
    assert g.ranks == {"average_speed": 7, "average_distance": 10, "best_speed": 10}
    assert speed["7"].run_style_code == "E/P" and speed["11"].scratched and speed["12"].scratched and not g.scratched
    assert speed["11"].ranks["average_speed"] is None and speed["11"].values["average_speed"] == 48.0
    cls = {r.program_number: r for r in tabs["ts_class"].runners}
    assert (cls["3"].days_off, cls["3"].values) == (12, {"prime_power": 107.0, "last_class": 111.0, "average_class": 110.0})
    assert cls["3"].run_style_code is None
    pace = {r.program_number: r for r in tabs["ts_pace"].runners}
    assert pace["7"].values == {"early_pace_1": 78.0, "early_pace_2": 73.0, "late_pace": 83.0}
    assert pace["7"].ranks["late_pace"] == 1


def test_an_unrated_runner_keeps_na_and_dashes_as_printed_never_as_zero():
    text = _split_tabs()["ts_speed"] + (
        "13\n\nPP 13\n8\nM: 8\nPure Electrum\nBay, Mare, 4 yrs (KY) L\nNA 0\n-\n-\n-\n-\n-\n-\n")
    (row,) = [r for r in parse_twinspires_tab(text, "ts_speed").runners if r.program_number == "13"]
    assert (row.run_style_raw, row.run_style_code, row.run_style_points) == ("NA 0", "NA", 0)
    assert row.values == {"average_speed": None, "average_distance": None, "best_speed": None}
    assert row.ranks == {"average_speed": None, "average_distance": None, "best_speed": None}


def test_the_column_header_row_identifies_the_tab_and_a_wrong_one_is_named():
    with pytest.raises(TwinSpiresTabError, match="expected the column header row"):
        parse_twinspires_tab(_split_tabs()["ts_speed"], "ts_pace")


def test_a_row_with_a_missing_cell_names_the_program_and_what_was_found():
    broken = _split_tabs()["ts_class"].replace("107\n1st\n", "107\n", 1)       # program 3 loses a rank cell
    with pytest.raises(TwinSpiresTabError, match=r"program 3 \(Hyper Awakening\): expected 7 cells"):
        parse_twinspires_tab(broken, "ts_class")


def test_a_malformed_cell_is_named():
    broken = _split_tabs()["ts_pace"].replace("74\n4th", "74\nfourth", 1)
    with pytest.raises(TwinSpiresTabError, match="early_pace_1 rank"):
        parse_twinspires_tab(broken, "ts_pace")


def test_pace_figures_are_tagged_sprint_or_route():
    assert [distance_category(d) for d in ("1 M", "1m 70 Y", "1 1/16 M", "6 F", "5 1/2 F", None)] == \
        ["route", "route", "route", "sprint", "sprint", None]


# ---- bundle sections ----------------------------------------------------------------------------------------
def test_the_three_tabs_are_found_by_their_headers_in_any_order():
    tabs = _split_tabs()
    base = MNR.read_text(encoding="utf-8")
    for order in (("ts_speed", "ts_class", "ts_pace"), ("ts_pace", "ts_speed", "ts_class"), ("ts_class", "ts_pace", "ts_speed")):
        sections, errors, _ = locate_sections(base + "\n" + "\n".join(tabs[k] for k in order))
        assert not errors and {"ts_speed", "ts_class", "ts_pace", "twinspires", "dk_basic", "dk_advanced"} <= set(sections)
        b = _bundle("\n".join(tabs[k] for k in order))
        assert b.validation.passed, b.validation.errors
        assert sorted(b.ts_tabs) == ["ts_class", "ts_pace", "ts_speed"]


def test_tabs_are_optional_and_do_not_disturb_the_existing_summary_tab():
    plain = _bundle()
    with_tabs = _bundle(TABS)
    assert plain.validation.passed and plain.ts_tabs == {} and bundle_summary(plain)["twinspires_tabs_present"] == []
    assert len(plain.twinspires.records) == len(with_tabs.twinspires.records) == 10
    assert bundle_summary(with_tabs)["twinspires_tabs_present"] == ["ts_class", "ts_pace", "ts_speed"]


def test_a_single_tab_alone_is_accepted():
    b = _bundle(_split_tabs()["ts_pace"])
    assert b.validation.passed and sorted(b.ts_tabs) == ["ts_pace"]


def test_a_malformed_tab_blocks_the_import_with_a_named_reason():
    b = _bundle(_split_tabs()["ts_class"].replace("107\n1st\n", "107\n", 1))
    assert not b.validation.passed
    assert any("TwinSpires Class tab is malformed (remove the tab to import without it)" in e for e in b.validation.errors)


def test_a_tab_from_another_race_blocks_the_import():
    other = _split_tabs()["ts_speed"].replace("Prussian Blue", "Some Other Horse")
    b = _bundle(other)
    assert not b.validation.passed
    assert any("CONFLICT program 1 horse" in e and "TwinSpires Speed" in e for e in b.validation.errors)


def test_a_tab_whose_scratches_disagree_with_the_card_blocks_the_import():
    wrong = _split_tabs()["ts_pace"].replace("SCR", "9", 1)       # program 11 is scratched on the card, live on the tab
    b = _bundle(wrong)
    assert not b.validation.passed
    assert any("CONFLICT program 11 scratched" in e and "TwinSpires Pace" in e for e in b.validation.errors)


def test_a_duplicated_tab_is_an_error():
    sp = _split_tabs()["ts_speed"]
    assert any("2 ts_speed sections" in e for e in _bundle(sp + "\n" + sp).validation.errors)


# ---- storage ------------------------------------------------------------------------------------------------
def test_tabs_are_stored_raw_and_parsed_once_with_capture_time_and_distance_category():
    b = _bundle(TABS)
    conn = _db()
    r = persist_validated_draftkings_markdown(conn, b.card, b.validation, source_filename=MNR.name,
                                              scheduled_post_utc=b.post_utc, captured_at=b.captured_at)
    first = persist_race_bundle_extras(conn, b, r.card_id)
    assert first.ts_tab_status == {"ts_speed": "STORED", "ts_class": "STORED", "ts_pace": "STORED"}
    assert persist_race_bundle_extras(conn, b, r.card_id).ts_tab_status == {
        "ts_speed": "ALREADY_STORED", "ts_class": "ALREADY_STORED", "ts_pace": "ALREADY_STORED"}
    rows = conn.execute("SELECT * FROM twinspires_tab_stats WHERE card_id=? ORDER BY tab_kind", (r.card_id,)).fetchall()
    assert [x["tab_kind"] for x in rows] == ["ts_class", "ts_pace", "ts_speed"]
    pace = next(x for x in rows if x["tab_kind"] == "ts_pace")
    assert (pace["late_capture"], pace["race_distance"], pace["distance_category"]) == (1, "1 M", "route")
    assert pace["captured_at"] == CAPTURED.isoformat() and "early pace 1" in pace["raw_text"]
    parsed = json.loads(pace["parsed_json"])
    assert parsed["runners"][1]["values"]["early_pace_1"] == 82.0 and "raw_text" not in parsed


def test_a_bundle_without_tabs_creates_no_tab_rows():
    b = _bundle()
    conn = _db()
    r = persist_validated_draftkings_markdown(conn, b.card, b.validation, source_filename=MNR.name,
                                              scheduled_post_utc=b.post_utc, captured_at=b.captured_at)
    assert persist_race_bundle_extras(conn, b, r.card_id).ts_tab_status == {}


# ---- Profit Line on the existing summary tab -----------------------------------------------------------------
def test_profit_line_is_parsed_in_every_printed_format_and_a_dash_is_none():
    header = "#\nAll\nODDS\nPL\nRunner\nDays Off\nRun Style\nAvg Spd\nBack Spd\nSpd LR\nAvg Cls\nPrm Pwr\nW% Jky\nW% Trn\n$ Won\n"
    def row(n, pl, name):
        return f"{n}\n8\nM: 8\n{pl}\n{name}\n21\nP1\n40\n44\n67\n101.9\n96\n11.1%\n15.5%\n$2913\n"
    card = parse_twinspires_markdown(
        (header + row(1, "7/2", "A") + row(2, "Even", "B") + row(3, "20", "C") + row(4, "-", "D")).encode(),
        source_filename="x.md")
    got = {r.horse_name: (r.profit_line, r.profit_line_decimal) for r in card.records}
    assert got == {"A": ("7/2", 4.5), "B": ("Even", 2.0), "C": ("20", 21.0), "D": (None, None)}
