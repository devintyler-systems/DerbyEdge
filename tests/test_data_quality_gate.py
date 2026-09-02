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
