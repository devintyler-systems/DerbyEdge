import sqlite3

import pandas as pd

from src.app.board_state import (
    LIVE_ODDS_UNAVAILABLE,
    apply_live_odds_overlay,
    latest_run_id_for_card,
    load_run_index_for_card,
    select_active_run_id,
)
from src.services.odds_intake import load_live_odds_by_pp


def _run_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE score_runs (
            run_id TEXT PRIMARY KEY,
            card_id INTEGER NOT NULL,
            model_id INTEGER,
            run_timestamp TEXT NOT NULL,
            model_type TEXT
        );
        CREATE TABLE model_registry (
            model_id INTEGER PRIMARY KEY,
            model_name TEXT,
            version TEXT
        );
        INSERT INTO model_registry VALUES (1, 'dirt-route', 'v1');
        """
    )
    return conn


def _board() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "entry_id": 101, "post_position": 1, "horse_name": "Alpha",
                "win_probability": 0.60, "market_implied_prob": 0.45,
                "value_score": 0.15, "bet_tag": "bet", "confidence_flag": 1,
            },
            {
                "entry_id": 102, "post_position": 2, "horse_name": "Bravo",
                "win_probability": 0.40, "market_implied_prob": 0.55,
                "value_score": -0.15, "bet_tag": "underlay", "confidence_flag": 1,
            },
        ]
    )


def test_run_index_is_card_scoped_newest_first_and_stable_on_timestamp_tie():
    conn = _run_conn()
    conn.executemany(
        "INSERT INTO score_runs VALUES (?, ?, 1, ?, 'dirt_route')",
        [
            ("run-a", 57, "2026-08-29T12:00:00Z"),
            ("run-z", 57, "2026-08-29T12:00:00Z"),
            ("other-card", 58, "2030-01-01T00:00:00Z"),
        ],
    )

    runs = load_run_index_for_card(conn, 57)

    assert [run["run_id"] for run in runs] == ["run-z", "run-a"]
    assert latest_run_id_for_card(conn, 57) == "run-z"


def test_selected_run_pin_keeps_manual_choice_but_rejects_another_cards_run():
    active_runs = [{"run_id": "newest"}, {"run_id": "older"}]

    assert select_active_run_id(active_runs, "older") == "older"
    assert select_active_run_id(active_runs, "another-card-run") == "newest"


def test_no_live_odds_keeps_persisted_value_and_tag_without_zero_fabrication():
    persisted = _board()

    result = apply_live_odds_overlay(
        persisted, {}, bet_edge_threshold=0.025, underlay_edge_threshold=-0.015
    )

    assert not result.available
    assert result.message == LIVE_ODDS_UNAVAILABLE
    assert result.board["value_score"].tolist() == persisted["value_score"].tolist()
    assert result.board["bet_tag"].tolist() == persisted["bet_tag"].tolist()
    assert result.board["value_score"].ne(0).any()
    assert result.board["live_market_prob"].isna().all()


def test_complete_live_odds_recomputes_market_edge_and_tag_but_not_model_probability():
    persisted = _board()
    live = {
        1: {"entry_id": 101, "decimal_odds": 3.0, "book_id": "book-a", "captured_at": "2026-08-29T12:00:00Z"},
        2: {"entry_id": 102, "decimal_odds": 2.0, "book_id": "book-a", "captured_at": "2026-08-29T12:00:00Z"},
    }

    result = apply_live_odds_overlay(
        persisted, live, bet_edge_threshold=0.025, underlay_edge_threshold=-0.015
    )

    assert result.available
    assert result.snapshot_timestamp == "2026-08-29T12:00:00Z"
    assert result.snapshot_source == "book-a"
    assert result.board["win_probability"].tolist() == persisted["win_probability"].tolist()
    assert result.board["market_implied_prob"].round(6).tolist() == [0.4, 0.6]
    assert result.board["value_score"].round(6).tolist() == [0.2, -0.2]
    assert result.board["bet_tag"].tolist() == ["bet", "underlay"]
    assert result.board["score_run_value_score"].tolist() == [0.15, -0.15]


def test_incomplete_or_mismatched_live_odds_retains_persisted_board_values():
    persisted = _board()
    incomplete = {
        1: {"entry_id": 101, "decimal_odds": 3.0, "book_id": "book-a", "captured_at": "2026-08-29T12:00:00Z"},
    }
    mismatched = {
        1: {"entry_id": 999, "decimal_odds": 3.0, "book_id": "book-a", "captured_at": "2026-08-29T12:00:00Z"},
        2: {"entry_id": 102, "decimal_odds": 2.0, "book_id": "book-a", "captured_at": "2026-08-29T12:00:00Z"},
    }

    for live in (incomplete, mismatched):
        result = apply_live_odds_overlay(
            persisted, live, bet_edge_threshold=0.025, underlay_edge_threshold=-0.015
        )
        assert not result.available
        assert result.message == LIVE_ODDS_UNAVAILABLE
        assert result.board["value_score"].tolist() == [0.15, -0.15]
        assert result.board["bet_tag"].tolist() == ["bet", "underlay"]


def test_live_overlay_preserves_low_confidence_bet_blocking():
    persisted = _board()
    persisted.loc[0, "confidence_flag"] = 0
    live = {
        1: {"entry_id": 101, "decimal_odds": 3.0},
        2: {"entry_id": 102, "decimal_odds": 2.0},
    }

    result = apply_live_odds_overlay(
        persisted, live, bet_edge_threshold=0.025, underlay_edge_threshold=-0.015
    )

    assert result.available
    assert result.board.loc[0, "value_score"] > 0.025
    assert result.board.loc[0, "bet_tag"] == "neutral"


def test_live_snapshot_loader_returns_entry_id_for_exact_overlay_mapping():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE live_odds (
               captured_at TEXT, book_id TEXT, card_id INTEGER, entry_id INTEGER,
               post_position INTEGER, decimal_odds REAL, is_scratched INTEGER,
               is_morning_line INTEGER
           )"""
    )
    conn.executemany(
        "INSERT INTO live_odds VALUES (?, 'book-a', 57, ?, ?, ?, 0, 0)",
        [
            ("2026-08-29T12:00:00Z", 101, 1, 3.0),
            ("2026-08-29T12:00:00Z", 102, 2, 2.0),
        ],
    )

    snapshot = load_live_odds_by_pp(conn, 57)

    assert snapshot[1]["entry_id"] == 101
    assert snapshot[2]["entry_id"] == 102


def test_live_snapshot_loader_rejects_ambiguous_duplicate_post_quotes():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE live_odds (
               captured_at TEXT, book_id TEXT, card_id INTEGER, entry_id INTEGER,
               post_position INTEGER, decimal_odds REAL, is_scratched INTEGER,
               is_morning_line INTEGER
           )"""
    )
    conn.executemany(
        "INSERT INTO live_odds VALUES (?, ?, 57, 101, 1, ?, 0, 0)",
        [
            ("2026-08-29T12:00:00Z", "book-a", 3.0),
            ("2026-08-29T12:00:00Z", "book-b", 3.2),
        ],
    )

    assert load_live_odds_by_pp(conn, 57) == {}
