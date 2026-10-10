"""Morning lines are stored "to-one": 9/2 -> 4.5, 20 -> 20, EVEN -> 1 (schema prob = 1/(odds+1))."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest.race_bundle import parse_race_bundle
from src.services.draftkings_markdown_intake import persist_validated_draftkings_markdown
from src.services.morning_line_repair import apply_morning_line_repair, find_misstored_morning_lines
from src.services.race_card_builder import parse_morning_line
from src.services.twinspires_intake import ensure_twinspires_source_tables

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "CT_Full_Race_Data_R7_10-8-26.md"
NAME = FIXTURE.name


@pytest.mark.parametrize("text,expected", [
    ("9/2", 4.5), ("9-2", 4.5), ("7/2", 3.5), ("5-2", 2.5), ("3/5", 0.6), ("1-9", 1 / 9),
    ("15-1", 15.0), ("20", 20.0), ("3", 3.0), ("99", 99.0), ("1", 1.0), ("2.5", 2.5),
    ("EVEN", 1.0), ("Even", 1.0), ("evens", 1.0),
    ("0", None), ("0.5", None), ("", None), (None, None), ("SCR", None), ("M: 3", None), ("5/0", None),
])
def test_parse_morning_line_is_to_one(text, expected):
    got = parse_morning_line(text)
    assert (got is None) if expected is None else got == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("text,prob", [("9/2", 2 / 11), ("7/2", 2 / 9), ("5-2", 2 / 7), ("20", 1 / 21), ("EVEN", 0.5)])
def test_the_probability_the_schema_derives_is_right(text, prob):
    odds = parse_morning_line(text)
    assert round(1.0 / (odds + 1.0), 6) == pytest.approx(prob, abs=1e-6)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("\n".join(
        l for l in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in l))
    ensure_twinspires_source_tables(conn)
    return conn


def _persisted():
    b = parse_race_bundle(
        FIXTURE.read_text(encoding="utf-8"), source_path=NAME,
        as_of=datetime(2026, 10, 8, 12, tzinfo=timezone.utc),
        captured_at=datetime(2026, 10, 8, 22, 30, tzinfo=timezone.utc),
    )
    conn = _db()
    r = persist_validated_draftkings_markdown(conn, b.card, b.validation, source_filename=NAME, scheduled_post_utc=b.post_utc)
    return conn, r


def test_persisted_fractional_line_has_the_right_stored_odds_and_probability():
    conn, _ = _persisted()
    row = conn.execute(
        "SELECT e.morning_line_odds o, e.morning_line_prob p FROM entries e WHERE program_number='4'").fetchone()
    assert row["o"] == pytest.approx(4.5) and row["p"] == pytest.approx(2 / 11, abs=1e-6)     # Manseeyasway 9/2
    whole = conn.execute("SELECT morning_line_odds o FROM entries WHERE program_number='1'").fetchone()
    assert whole["o"] == 15.0                                                                  # Road Minister 15


def test_repair_finds_and_fixes_cards_stored_with_the_old_convention():
    conn, r = _persisted()
    assert find_misstored_morning_lines(conn) == []                       # nothing wrong after the fix
    # recreate what the old code stored: fractional lines carried +1 (9/2 -> 5.5, 7/2 -> 4.5)
    conn.execute("UPDATE entries SET morning_line_odds = morning_line_odds + 1 WHERE program_number IN ('4','8')")
    bad = find_misstored_morning_lines(conn)
    assert {(x["program"], x["source_ml"], x["stored"], x["correct"]) for x in bad} == {
        ("4", "9/2", 5.5, 4.5), ("8", "7/2", 4.5, 3.5)}
    assert apply_morning_line_repair(conn, bad) == 2
    assert find_misstored_morning_lines(conn) == []
    assert conn.execute("SELECT morning_line_prob p FROM entries WHERE program_number='4'").fetchone()["p"] == pytest.approx(2 / 11, abs=1e-6)


def test_repair_is_a_noop_on_a_database_without_stored_dk_documents():
    conn = sqlite3.connect(":memory:")
    assert find_misstored_morning_lines(conn) == []
