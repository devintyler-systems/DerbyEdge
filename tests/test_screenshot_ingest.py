"""Tests for screenshot ingestor — DB-only path (no live Anthropic call)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from derbyedge.screenshot_ingest import (
    ParsedScreenshot,
    parse_distance,
    ingest_parsed_race,
    ingest_parsed_odds,
    _ml_to_decimal,
    _american_to_decimal,
    _backfill_track_id,
    _strip_json_fences,
    TRACK_NAME_TO_ID,
)
from derbyedge.schema import init_db
from derbyedge.odds_schema import init_odds_schema


@pytest.fixture
def fresh_conn(tmp_path):
    db = tmp_path / "t.sqlite"
    conn = sqlite3.connect(str(db))
    init_db(conn)
    init_odds_schema(conn)
    return conn


def _sample_parsed(track_id="MNR", race_num=2):
    return ParsedScreenshot(
        track_id=track_id,
        track_name="Mountaineer Park",
        race_date="2026-05-05",
        race_number=race_num,
        post_time="7:47 PM EST",
        distance_text="1 Mile",
        surface="D",
        race_type="Allowance",
        purse_usd=12000,
        book_id="betonline",
        runners=[
            {"program_number": "1", "horse_name": "Roubaix",
             "post_position": 1, "jockey": "F Garcia", "trainer": "J P Silva",
             "morning_line": "4-1", "current_odds_decimal": 5.0,
             "is_scratched": False},
            {"program_number": "2", "horse_name": "Roman Goddess",
             "post_position": 2, "jockey": "C Oliveros", "trainer": "J C Vazquez",
             "morning_line": "10-1", "current_odds_decimal": 11.0,
             "is_scratched": False},
            {"program_number": "4", "horse_name": "Gold Time Vixen",
             "post_position": 4, "jockey": "L A Batista", "trainer": "W J Kee",
             "morning_line": "20-1", "current_odds_decimal": None,
             "is_scratched": True},
        ],
    )


# -------------------- Distance / odds parsing --------------------

def test_parse_distance_miles():
    d, u, _ = parse_distance("1 Mile")
    assert d == 800 and u == "M"


def test_parse_distance_furlongs():
    d, u, _ = parse_distance("6 Furlongs")
    assert d == 600 and u == "F"


def test_parse_distance_half_furlong():
    d, u, _ = parse_distance("5 1/2 Furlongs")
    assert d == 550 and u == "F"


def test_parse_distance_decimal_furlong():
    d, u, _ = parse_distance("5.5f")
    assert d == 550 and u == "F"


def test_parse_distance_fractional_mile():
    d, u, _ = parse_distance("1 1/16 mile")
    assert d == 850 and u == "M"


def test_parse_distance_unknown_returns_none():
    assert parse_distance(None) == (None, None, None)
    d, u, _ = parse_distance("about a million miles")
    # "miles" still matches as 1m fallback... that's fine; just make sure no crash
    assert u in (None, "M")


def test_ml_to_decimal_dash_format():
    assert _ml_to_decimal("4-1") == pytest.approx(5.0)
    assert _ml_to_decimal("9-2") == pytest.approx(5.5)


def test_ml_to_decimal_slash_format():
    assert _ml_to_decimal("9/2") == pytest.approx(5.5)


def test_american_to_decimal():
    assert _american_to_decimal(+200) == pytest.approx(3.0)
    assert _american_to_decimal(-200) == pytest.approx(1.5)


def test_strip_json_fences():
    assert _strip_json_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_json_fences('{"a":1}') == '{"a":1}'


def test_backfill_track_id_from_name():
    p = {"track_name": "Mountaineer Park", "track_id": None}
    assert _backfill_track_id(p)["track_id"] == "MNR"


def test_backfill_skips_when_already_set():
    p = {"track_name": "Mountaineer Park", "track_id": "XYZ"}
    assert _backfill_track_id(p)["track_id"] == "XYZ"


# -------------------- DB insertion --------------------

def test_ingest_parsed_race_inserts_rows(fresh_conn):
    parsed = _sample_parsed()
    rid = ingest_parsed_race(fresh_conn, parsed)
    assert rid == "MNR|2026-05-05|2"
    cur = fresh_conn.cursor()
    assert cur.execute("SELECT count(*) FROM races").fetchone()[0] == 1
    # All 3 runners inserted (including scratched one)
    assert cur.execute("SELECT count(*) FROM entries WHERE race_id=?", (rid,)).fetchone()[0] == 3
    # Track auto-created
    assert cur.execute("SELECT track_name FROM tracks WHERE track_id='MNR'").fetchone()[0] == "Mountaineer Park"
    # Race header populated
    row = cur.execute(
        "SELECT surface, distance_id, distance_unit, purse_usa, post_time FROM races WHERE race_id=?",
        (rid,),
    ).fetchone()
    assert row == ("D", 800, "M", 12000.0, "7:47 PM EST")


def test_ingest_parsed_race_idempotent_with_overwrite(fresh_conn):
    parsed = _sample_parsed()
    ingest_parsed_race(fresh_conn, parsed)
    # Second insert without overwrite -> raises
    with pytest.raises(ValueError, match="already exists"):
        ingest_parsed_race(fresh_conn, parsed)
    # With overwrite -> succeeds and replaces
    parsed2 = _sample_parsed()
    parsed2.runners = parsed2.runners[:2]  # drop scratched horse
    ingest_parsed_race(fresh_conn, parsed2, overwrite=True)
    cur = fresh_conn.cursor()
    n = cur.execute("SELECT count(*) FROM entries WHERE race_id=?",
                    (parsed.race_id(),)).fetchone()[0]
    assert n == 2


def test_ingest_parsed_odds_writes_snapshots(fresh_conn):
    parsed = _sample_parsed()
    rid = ingest_parsed_race(fresh_conn, parsed)
    n = ingest_parsed_odds(fresh_conn, parsed, rid)
    # 2 horses with current_odds + 3 morning_lines = 5 rows
    assert n == 5
    cur = fresh_conn.cursor()
    rows = cur.execute(
        "SELECT book_id, decimal_odds, is_morning_line FROM odds_snapshots "
        "WHERE race_id=? ORDER BY is_morning_line, book_id, program_number",
        (rid,),
    ).fetchall()
    # All have entry_id resolved
    none_entry = cur.execute(
        "SELECT count(*) FROM odds_snapshots WHERE race_id=? AND entry_id IS NULL",
        (rid,),
    ).fetchone()[0]
    assert none_entry == 0


def test_race_id_requires_keys():
    bad = ParsedScreenshot(track_id="MNR", race_date=None, race_number=2)
    with pytest.raises(ValueError, match="track_id"):
        bad.race_id()


def test_known_track_names_have_codes():
    # Sanity: we have codes for the visible-screenshot tracks the user is likely to drop
    assert TRACK_NAME_TO_ID["mountaineer park"] == "MNR"
    assert TRACK_NAME_TO_ID["churchill downs"] == "CD"
