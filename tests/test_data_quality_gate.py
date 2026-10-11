from __future__ import annotations

import sqlite3

import pytest

from src.ingest.firstbet_pdf import build_feature_audit, parse_firstbet_text
from src.ingest.run_state import DataQuality, RunMode, resolve_run_mode
from src.services.run_mode import ScoringBlockedError, ensure_scoring_eligible


def _quality(**overrides) -> DataQuality:
    values = dict(
        entries_parsed=9,
        field_size_declared=9,
        entries_with_pp_history=9,
        starter_match_rate=1.0,
        race_metadata_complete=True,
        has_morning_lines=True,
        has_live_odds=False,
        required_model_features_complete=False,
        blocking_errors=[],
    )
    values.update(overrides)
    return DataQuality(**values)


def test_zero_attached_pps_is_market_baseline_only():
    mode, reasons = resolve_run_mode(
        _quality(entries_with_pp_history=0, starter_match_rate=0.0)
    )
    assert mode == RunMode.MARKET_BASELINE_ONLY
    assert "not model output" in reasons[1]


def test_incomplete_declared_field_blocks():
    mode, reasons = resolve_run_mode(
        _quality(entries_parsed=8, entries_with_pp_history=8, starter_match_rate=1.0)
    )
    assert mode == RunMode.BLOCKED
    assert any("Declared field size is 9" in reason for reason in reasons)


def test_confirmed_scratches_fully_explain_active_field_reduction():
    mode, reasons = resolve_run_mode(
        _quality(
            entries_parsed=10,
            field_size_declared=13,
            entries_with_pp_history=10,
            starter_match_rate=1.0,
            entries_scratched=3,
        )
    )
    assert mode == RunMode.PP_PARSED_FEATURES_PENDING
    assert not any("Declared field size" in reason for reason in reasons)


def test_starter_match_and_pp_coverage_thresholds_block():
    mode, reasons = resolve_run_mode(
        _quality(entries_with_pp_history=7, starter_match_rate=7 / 9)
    )
    assert mode == RunMode.BLOCKED
    assert "minimum is 90%" in reasons[0]


def test_only_full_features_plus_live_odds_reaches_model_ready():
    assert resolve_run_mode(
        _quality(has_live_odds=True)
    )[0] == RunMode.PP_PARSED_FEATURES_PENDING
    assert resolve_run_mode(
        _quality(required_model_features_complete=True)
    )[0] == RunMode.MODEL_READY_LIMITED
    assert resolve_run_mode(
        _quality(has_live_odds=True, required_model_features_complete=True)
    )[0] == RunMode.MODEL_READY


def test_parser_field_mismatch_is_blocked():
    text = """9/2/26, 1:24 PM 1/ST BET - Test
SARATOGA TB R 8
2:12 PM 3 Horses AOC $110,000 6 1/2F Dirt / Muddy
HORSE ONE -
1
J: A Rider ML 4
T: A Trainer
PP1
HORSE TWO -
2
J: B Rider ML 5
T: B Trainer
PP2
"""
    payload, parser_audit = parse_firstbet_text(
        text,
        filename="short.pdf",
        sha256="x",
        uploaded_at_utc="2026-09-02T20:24:00Z",
    )
    audit = build_feature_audit(payload)
    assert parser_audit["run_mode"] == RunMode.BLOCKED.value
    assert audit["entries_parsed"] == 2
    assert any("Declared field size is 3" in error for error in audit["blocking_errors"])


def test_model_call_guard_rejects_ml_only_card(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tracks (track_id INTEGER PRIMARY KEY, abbrev TEXT);
        CREATE TABLE race_cards (
            card_id INTEGER PRIMARY KEY, track_id INTEGER, field_size INTEGER,
            card_date TEXT, race_number INTEGER, distance_yards INTEGER, surface TEXT
        );
        CREATE TABLE entries (
            entry_id INTEGER PRIMARY KEY, card_id INTEGER, morning_line_odds REAL,
            scratch_flag INTEGER
        );
        INSERT INTO tracks VALUES (1, 'SAR');
        INSERT INTO race_cards VALUES (8, 1, 2, '2026-09-02', 8, 1430, 'dirt');
        INSERT INTO entries VALUES (1, 8, 4.0, 0), (2, 8, 5.0, 0);
        """
    )
    with pytest.raises(ScoringBlockedError, match="MARKET_BASELINE_ONLY"):
        ensure_scoring_eligible(conn, 8, runs_root=tmp_path)


# ---- KEE R9 10-11-26: a maiden race with 5 debut runners was blocked as a 50% "starter match" ---------------------
def test_debut_runners_are_not_counted_as_parser_misses():
    q = _quality(entries_parsed=10, field_size_declared=12, entries_scratched=2, entries_with_pp_history=5,
                 starter_match_rate=1.0, first_time_starters=5)
    mode, _ = resolve_run_mode(q)
    assert mode == RunMode.PP_PARSED_FEATURES_PENDING


def test_a_horse_with_prior_starts_but_no_pp_rows_still_blocks():
    mode, reasons = resolve_run_mode(_quality(entries_parsed=10, field_size_declared=10, entries_with_pp_history=5,
                                              starter_match_rate=0.5, first_time_starters=0))
    assert mode == RunMode.BLOCKED and any("50%" in r for r in reasons)


def _db_card(starts_and_pp):
    """starts_and_pp: list of (career_starts, has_pp_row)."""
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE race_cards (card_id INTEGER, field_size INT, card_date TEXT, race_number INT,
                                 distance_yards INT, surface TEXT, track_id INT);
        CREATE TABLE tracks (track_id INTEGER, abbrev TEXT);
        CREATE TABLE entries (entry_id INTEGER, card_id INT, morning_line_odds REAL, scratch_flag INT, career_starts INT);
        CREATE TABLE horse_starts (entry_id INT, card_id INT);
        INSERT INTO tracks VALUES (1, 'KEE');
        INSERT INTO race_cards VALUES (1, %d, '2026-10-11', 9, 1320, 'dirt', 1);""" % len(starts_and_pp))
    for i, (starts, pp) in enumerate(starts_and_pp, 1):
        c.execute("INSERT INTO entries VALUES (?,1,5.0,0,?)", (i, starts))
        if pp:
            c.execute("INSERT INTO horse_starts VALUES (?,1)", (i,))
    return c


def test_card_quality_treats_career_starts_zero_as_a_debut_but_not_a_missing_pp():
    from src.services.run_mode import data_quality_from_card
    q = data_quality_from_card(_db_card([(12, True)] * 5 + [(0, False)] * 5), 1)
    assert (q.first_time_starters, q.entries_with_pp_history, q.starter_match_rate) == (5, 5, 1.0)
    assert resolve_run_mode(q)[0] != RunMode.BLOCKED
    miss = data_quality_from_card(_db_card([(12, True)] * 5 + [(3, False)] * 5), 1)
    assert (miss.first_time_starters, miss.starter_match_rate) == (0, 0.5)
    assert resolve_run_mode(miss)[0] == RunMode.BLOCKED
    unknown = data_quality_from_card(_db_card([(12, True)] * 5 + [(None, False)] * 5), 1)
    assert unknown.first_time_starters == 0
