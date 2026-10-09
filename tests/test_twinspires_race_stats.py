"""TwinSpires RACE STATS block: strict parse, optional bundle section, raw + parsed storage linked to the race."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest.race_bundle import is_race_bundle, locate_sections, parse_race_bundle
from src.ingest.twinspires_race_stats import RaceStatsError, parse_race_stats
from src.services.draftkings_markdown_intake import persist_validated_draftkings_markdown
from src.services.race_bundle_intake import bundle_summary, persist_race_bundle_extras
from src.services.twinspires_intake import twinspires_pace_for_card

ROOT = Path(__file__).resolve().parents[1]
STATS = (ROOT / "tests" / "fixtures" / "TS_RaceStats_BEL_R5_10-9-26.md").read_text(encoding="utf-8")
BEL = ROOT / "tests" / "fixtures" / "BEL_Full_Race_Data_R5_10-9-26.md"
BEL_TEXT = BEL.read_text(encoding="utf-8")
CAPTURED = datetime(2026, 10, 9, 19, 10, 39, tzinfo=timezone.utc)


def _bundle(text: str):
    return parse_race_bundle(text, source_path=BEL.name, as_of=CAPTURED.replace(second=0, microsecond=0), captured_at=CAPTURED)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("\n".join(
        l for l in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in l))
    return conn


def _persist(b):
    conn = _db()
    r = persist_validated_draftkings_markdown(conn, b.card, b.validation, source_filename=BEL.name,
                                              scheduled_post_utc=b.post_utc, captured_at=b.captured_at)
    return conn, r.card_id


# ---- parser -----------------------------------------------------------------------------------------------
def test_sample_block_parses_into_pars_race_type_and_both_bias_windows():
    s = parse_race_stats(STATS)
    assert s.track == "BEL" and s.pars == {"E1": 85, "E2": 85, "LP": 83, "SPD": 85}
    assert s.header_lines[:2] == ["$16K CLAIMING", "$16,000"] and "Purse: $44K" in s.header_lines
    assert s.race_type_years == 3
    (rt,) = s.race_types
    assert (rt.race_type, rt.races, rt.fav_win_pct, rt.fav_roi_2) == ("BEL 3up CLM Dirt 1 1/16 M", 1, 0.0, -2.0)
    assert (rt.avg_field_size, rt.median_win_payoff, rt.pct_winners_under_5_1) == (11.0, 7.86, 100.0)
    meet, week = s.track_bias
    assert (meet.scope, meet.races, meet.period_start, meet.period_end, meet.distance) == ("Meet", 6, "09/18", "10/08", "8.5f")
    assert (week.scope, week.races, week.period_start, week.period_end) == ("Week", 3, "10/02", "10/08")
    assert (meet.wire_pct, meet.speed_bias_pct, meet.wnr_avg_bl_1st_call, meet.wnr_avg_bl_2nd_call) == (33.3, 100.0, 0.85, 0.37)
    assert meet.run_style_impact == {"E": 1.82, "E/P": 1.78, "P": 0.0, "S": 0.0}
    assert meet.run_style_pct_won == {"E": 50.0, "E/P": 50.0, "P": 0.0, "S": 0.0}
    assert meet.post_impact == {"RAIL": 1.31, "1-3": 0.44, "4-7": 1.04, "8+": 2.39}
    assert week.post_avg_win_pct == {"RAIL": 33.3, "1-3": 11.1, "4-7": 18.2, "8+": 0.0}
    assert week.run_style_pct_won["E/P"] == 66.7


def test_crlf_and_blank_lines_do_not_change_the_result():
    messy = STATS.replace("\n", "\r\n\r\n")
    a, b = parse_race_stats(STATS), parse_race_stats(messy)
    assert (a.pars, a.race_types, a.track_bias, a.header_lines) == (b.pars, b.race_types, b.track_bias, b.header_lines)


def _mutate(old: str, new: str, count: int = 1) -> str:
    assert old in STATS, old
    return STATS.replace(old, new, count)


@pytest.mark.parametrize("text, fragment", [
    ("", "must start with"),
    ("something else\n" + STATS, "must start with"),
    (STATS.split("Race Type Stats")[0], "no 'Race Type Stats"),                                  # truncated after the header
    (_mutate("| PARS: | E1: 85 | E2: 85 | LP: 83 | SPD: 85\n", ""), "exactly one PARS line"),
    (_mutate("E2: 85", "E2: eighty"), "PARS line is not"),
    (_mutate("E1: 85", "E2: 85", 1), "duplicate PARS key"),
    (_mutate("# Races\nFAV Win%", "Races\nFAV Win%"), "expected the column labels"),             # label drift
    (_mutate("0.0%\n0.0%\n-2.00", "0.0%\n0.0%"), "not a whole number"),                          # a missing cell
    (_mutate("-2.00", "n/a"), "not a valid float"),
    (_mutate("$7.86", "7.86"), "not a valid money"),
    (_mutate("100.0%\n0.0%\n0.0%\nDIRT", "100.0%\n0.0%\nDIRT"), "not a valid pct value"),           # short payoff row: the next heading lands in a value cell
    (_mutate("0.85", "x.85"), "not a valid float"),
    (_mutate("Impact Values\n1.82\n1.78\n0.00\n0.00", "Impact Values\n1.82\n1.78\n0.00"), "not a valid float"),   # 3 values for 4 columns
    (_mutate("DIRT 8.5f Track Bias Stats: Week (10/02 - 10/08)", "DIRT 8.5f Track Bias Stats: Meet (10/02 - 10/08)"),
     "appears twice"),
    (_mutate("DIRT 8.5f Track Bias Stats: Week (10/02 - 10/08)", "DIRT Track Bias Stats: Week"), "unrecognised line"),
    (STATS + "\nsurprise trailing line\n", "unrecognised line"),
])
def test_malformed_blocks_fail_with_a_named_reason(text, fragment):
    import re
    with pytest.raises(RaceStatsError) as err:
        parse_race_stats(text)
    assert re.search(fragment, str(err.value)), str(err.value)


# ---- bundle -----------------------------------------------------------------------------------------------
def test_bundle_passes_without_the_block_and_carries_none():
    b = _bundle(BEL_TEXT)
    assert b.validation.passed and b.race_stats is None
    assert "race_stats" not in locate_sections(BEL_TEXT)[0] and bundle_summary(b)["race_stats_present"] is False


@pytest.mark.parametrize("where", ["end", "start", "middle"])
def test_block_is_found_wherever_it_is_pasted_and_does_not_disturb_the_tabs(where):
    lines = BEL_TEXT.rstrip("\n").split("\n")
    if where == "end":
        text = BEL_TEXT.rstrip("\n") + "\n\n" + STATS
    elif where == "start":
        text = STATS + "\n\n" + BEL_TEXT
    else:
        sections, _e, _l = locate_sections(BEL_TEXT)
        cut = sorted(first for first, _ in sections.values())[1]
        text = "\n".join(lines[:cut]) + "\n\n" + STATS + "\n\n" + "\n".join(lines[cut:]) + "\n"
    base, b = _bundle(BEL_TEXT), _bundle(text)
    assert is_race_bundle(text) and "race_stats" in locate_sections(text)[0]
    assert b.validation.passed, b.validation.errors
    assert b.race_stats is not None and b.race_stats.pars["SPD"] == 85
    assert b.validation.parsed_unique_runner_count == base.validation.parsed_unique_runner_count
    assert [r.horse_name for r in b.twinspires.records] == [r.horse_name for r in base.twinspires.records]
    assert bundle_summary(b)["race_stats_present"] is True


def test_a_malformed_block_blocks_the_import_and_says_so():
    bad = STATS.replace("Speed Bias", "Speed Bias X", 1)
    b = _bundle(BEL_TEXT + "\n" + bad)
    assert not b.validation.passed and b.race_stats is None
    assert any(e.startswith("RACE STATS block is malformed") and "expected the column labels" in e for e in b.validation.errors)


def test_two_blocks_are_a_named_error():
    b = _bundle(BEL_TEXT + "\n" + STATS + "\n" + STATS)
    assert not b.validation.passed and any("2 race_stats sections" in e for e in b.validation.errors)


def test_a_dk_advanced_plus_the_block_alone_is_not_mistaken_for_a_bundle():
    sections, _e, lines = locate_sections(BEL_TEXT)
    first, end = sections["dk_advanced"]
    assert not is_race_bundle("\n".join(lines[first:end]) + "\n" + STATS)


# ---- storage ----------------------------------------------------------------------------------------------
def test_block_is_stored_raw_and_parsed_with_capture_time_linked_to_the_card():
    b = _bundle(BEL_TEXT + "\n" + STATS)
    conn, card_id = _persist(b)
    extras = persist_race_bundle_extras(conn, b, card_id)
    assert extras.race_stats_status == "STORED" and extras.twinspires_status == "PASS"
    row = conn.execute("SELECT * FROM twinspires_race_stats").fetchone()
    assert row["card_id"] == card_id and row["captured_at"] == CAPTURED.isoformat()
    assert row["raw_text"] == STATS.replace("\r\n", "\n").strip("\n") + "\n"           # as pasted, trailing spaces and all
    assert row["bundle_sha256"] == b.bundle_sha256 and row["parser_version"] == b.race_stats.parser_version
    parsed = json.loads(row["parsed_json"])
    assert parsed["pars"]["E1"] == 85 and [t["scope"] for t in parsed["track_bias"]] == ["Meet", "Week"]
    assert parsed["track_bias"][0]["races"] == 6 and parsed["race_types"][0]["races"] == 1
    assert "raw_text" not in parsed


def test_reimporting_the_same_block_keeps_one_row_and_its_first_capture_time():
    b = _bundle(BEL_TEXT + "\n" + STATS)
    conn, card_id = _persist(b)
    first = persist_race_bundle_extras(conn, b, card_id)
    later = parse_race_bundle(BEL_TEXT + "\n" + STATS, source_path=BEL.name, as_of=CAPTURED,
                              captured_at=CAPTURED.replace(minute=40))
    again = persist_race_bundle_extras(conn, later, card_id)
    assert again.race_stats_status == "ALREADY_STORED" and again.race_stats_id == first.race_stats_id
    rows = conn.execute("SELECT captured_at FROM twinspires_race_stats").fetchall()
    assert [r[0] for r in rows] == [CAPTURED.isoformat()]


def test_a_changed_block_is_kept_as_a_new_capture():
    b = _bundle(BEL_TEXT + "\n" + STATS)
    conn, card_id = _persist(b)
    persist_race_bundle_extras(conn, b, card_id)
    changed = parse_race_bundle(BEL_TEXT + "\n" + STATS.replace("SPD: 85", "SPD: 86"), source_path=BEL.name, as_of=CAPTURED,
                                captured_at=CAPTURED.replace(minute=40))
    persist_race_bundle_extras(conn, changed, card_id)
    assert [json.loads(r[0])["pars"]["SPD"] for r in conn.execute(
        "SELECT parsed_json FROM twinspires_race_stats ORDER BY race_stats_id")] == [85, 86]


def test_no_block_writes_no_row_and_the_rest_of_the_import_is_unchanged():
    b = _bundle(BEL_TEXT)
    conn, card_id = _persist(b)
    extras = persist_race_bundle_extras(conn, b, card_id)
    assert extras.race_stats_status == "NOT_PRESENT" and extras.race_stats_id is None and extras.twinspires_status == "PASS"
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='twinspires_race_stats'").fetchone() is None   # nothing created


def test_the_block_changes_nothing_that_scoring_reads():
    with_block, without = _bundle(BEL_TEXT + "\n" + STATS), _bundle(BEL_TEXT)
    conn_a, card_a = _persist(with_block)
    conn_b, card_b = _persist(without)
    persist_race_bundle_extras(conn_a, with_block, card_a)
    persist_race_bundle_extras(conn_b, without, card_b)
    assert twinspires_pace_for_card(conn_a, card_a) == twinspires_pace_for_card(conn_b, card_b)
    for table in ("entries", "source_observations", "source_feature_candidates", "market_snapshots"):
        try:
            qa = conn_a.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            qb = conn_b.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        assert qa == qb, table
