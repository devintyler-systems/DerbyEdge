"""Regression coverage for the entry_scores market probability mapping."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.ingest.run_state import RunMode
from src.models import scorer
from src.services.runtime_score_preflight import ExecutionMode


ROOT = Path(__file__).resolve().parents[1]
NAMES = [
    "Kissed a Cadet",
    "My Rapha",
    "Preacher Man Sam",
    "Burn Indy Burn",
    "Native Hellraiser",
    "Steely Eye",
]
ML_ODDS = [3.0, 4.5, 10.0, 2.0, 5.5, 5.0]
MODEL_PROBS = np.asarray([0.30, 0.23, 0.18, 0.13, 0.10, 0.06])


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_six_entry_card(db_path: Path) -> list[int]:
    conn = _connect(db_path)
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('Prairie Meadows', 'PRM')")
    conn.execute(
        """INSERT INTO race_cards
           (track_id, card_date, race_number, distance_yards, surface, field_size,
            scheduled_post_time_utc)
           VALUES (1, '2026-09-14', 7, 1200, 'dirt', 6, '2026-09-14T22:00:00Z')"""
    )
    entry_ids: list[int] = []
    for post, (name, odds) in enumerate(zip(NAMES, ML_ODDS), start=1):
        horse_id = conn.execute("INSERT INTO horses (name) VALUES (?)", (name,)).lastrowid
        entry_id = conn.execute(
            """INSERT INTO entries
               (card_id, horse_id, post_position, morning_line_odds)
               VALUES (1, ?, ?, ?)""",
            (horse_id, post, odds),
        ).lastrowid
        entry_ids.append(int(entry_id))
        conn.execute(
            """INSERT INTO feature_store
               (card_id, entry_id, horse_id, horse_name, post_position, build_ts,
                pace_fit_score, market_implied_prob)
               VALUES (1, ?, ?, ?, ?, '2026-09-14T20:00:00Z', 0.5, ?)""",
            (entry_id, horse_id, name, post, round(1.0 / (odds + 1.0), 6)),
        )
    conn.commit()
    conn.close()
    return entry_ids


def _patch_successful_score(monkeypatch: pytest.MonkeyPatch, db_path: Path, tmp_path: Path) -> None:
    quality = SimpleNamespace(entries_parsed=6)
    state = SimpleNamespace(mode=RunMode.MODEL_READY_LIMITED, quality=quality, audit={})
    verification = SimpleNamespace(core_rows=[], warnings=())
    artifact = SimpleNamespace(
        config={
            "bet_edge_threshold": 0.025,
            "underlay_edge_threshold": -0.015,
            "feature_groups": {},
            "model_family": "dirt_sprint",
        },
        model_type="seed_only_baseline",
        race_type_key="dirt_sprint",
        training_rows=0,
        temperature=1.0,
        calibration_audit={
            "morning_line_available": True,
            "market_prior_source": "morning_line",
        },
        dispatcher_audit={"mode": "baseline", "reason_codes": []},
        model_name="fixture",
        version="test",
    )
    run_dir = tmp_path / "score-output"
    run_dir.mkdir()

    monkeypatch.setattr(scorer, "get_connection", lambda: _connect(db_path))
    monkeypatch.setattr(scorer, "get_card_run_state", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(scorer, "verify_feature_frame", lambda *_args, **_kwargs: verification)
    monkeypatch.setattr(scorer, "quality_with_verified_features", lambda *_args: quality)
    monkeypatch.setattr(
        scorer,
        "resolve_mode_with_feature_checks",
        lambda *_args: (RunMode.MODEL_READY_LIMITED, []),
    )
    monkeypatch.setattr(scorer, "is_derby_context", lambda *_args: False)
    monkeypatch.setattr(
        scorer,
        "train_or_build",
        lambda **_kwargs: (artifact, MODEL_PROBS.copy()),
    )
    monkeypatch.setattr(
        scorer,
        "pre_market_signal_probabilities",
        lambda *_args, **_kwargs: MODEL_PROBS.copy(),
    )
    monkeypatch.setattr(
        scorer,
        "compute_group_scores",
        lambda feat_df, _config: {
            "form_class": np.full(len(feat_df), 0.5),
            "distance_surface": np.full(len(feat_df), 0.5),
        },
    )

    def confidence(_feat_df, entries_df, *_args, **_kwargs):
        return pd.DataFrame(
            {
                "entry_id": entries_df["entry_id"].astype(int),
                "confidence_flag": 1,
                "confidence_score": 0.75,
                "confidence_bucket": "MEDIUM",
                "confidence_reasons": "fixture",
                "model_confidence": "medium",
                "missing_data_flags": "",
            }
        )

    monkeypatch.setattr(scorer, "compute_horse_confidence", confidence)
    monkeypatch.setattr(
        scorer,
        "_chaos_outputs_for_run",
        lambda entries_df, *_args, **_kwargs: (
            np.zeros(len(entries_df)),
            np.zeros(len(entries_df)),
            [None] * len(entries_df),
            np.zeros(len(entries_df), dtype=int),
            False,
            0.0,
        ),
    )
    monkeypatch.setattr(scorer, "_resolve_serving_mode", lambda: "off")
    monkeypatch.setattr(scorer, "save_artifact", lambda _artifact: tmp_path / "fixture.pkl")
    monkeypatch.setattr(scorer, "register_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scorer, "run_dir_for_card", lambda *_args, **_kwargs: run_dir)
    monkeypatch.setattr(scorer, "_write_board", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scorer, "_write_eval_report", lambda *_args, **_kwargs: None)


def _score_and_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, live_decimals=None):
    db_path = tmp_path / "scorer-market.db"
    entry_ids = _seed_six_entry_card(db_path)
    if live_decimals is not None:
        conn = _connect(db_path)
        conn.execute(
            """CREATE TABLE live_odds (
                   lo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   captured_at TEXT NOT NULL,
                   book_id TEXT NOT NULL DEFAULT 'manual',
                   card_id INTEGER NOT NULL,
                   entry_id INTEGER,
                   post_position INTEGER,
                   decimal_odds REAL,
                   american_odds INTEGER,
                   is_scratched INTEGER NOT NULL DEFAULT 0,
                   is_morning_line INTEGER NOT NULL DEFAULT 0,
                   odds_type TEXT NOT NULL DEFAULT 'live_tote'
               )"""
        )
        conn.executemany(
            """INSERT INTO live_odds
               (captured_at, book_id, card_id, entry_id, post_position,
                decimal_odds, is_scratched, is_morning_line, odds_type)
               VALUES ('2026-09-14T21:00:00Z', 'manual', 1, ?, ?, ?, 0, 0, 'live_tote')""",
            [(entry_id, post, decimal) for post, (entry_id, decimal) in enumerate(
                zip(entry_ids, live_decimals), start=1
            )],
        )
        conn.commit()
        conn.close()

    _patch_successful_score(monkeypatch, db_path, tmp_path)
    scorer.score_race(card_id=1, execution_mode=ExecutionMode.BACKTEST)

    conn = _connect(db_path)
    rows = conn.execute(
        """SELECT market_implied_prob, p_ml_implied, p_market_live,
                  value_score, edge_vs_live_market, bet_tag
           FROM entry_scores ORDER BY post_position"""
    ).fetchall()
    conn.close()
    return rows


def _expected_ml() -> np.ndarray:
    raw = np.asarray([round(1.0 / (odds + 1.0), 6) for odds in ML_ODDS])
    return raw / raw.sum()


def test_six_entry_no_live_card_persists_ml_baseline_without_live_alias(monkeypatch, tmp_path):
    rows = _score_and_rows(monkeypatch, tmp_path)
    expected_ml = _expected_ml()

    assert len(rows) == 6
    assert all(row["market_implied_prob"] is not None for row in rows)
    assert [row["market_implied_prob"] for row in rows] == pytest.approx(expected_ml, abs=1e-6)
    assert [row["p_ml_implied"] for row in rows] == pytest.approx(expected_ml, abs=1e-6)
    assert all(row["p_market_live"] is None for row in rows)
    assert all(row["value_score"] is None for row in rows)
    assert all(row["edge_vs_live_market"] is None for row in rows)
    assert all(row["bet_tag"] is None for row in rows)


def test_live_card_persists_distinct_ml_baseline_and_normalized_live_market(monkeypatch, tmp_path):
    live_decimals = [2.5, 4.0, 8.0, 3.0, 6.0, 10.0]
    rows = _score_and_rows(monkeypatch, tmp_path, live_decimals=live_decimals)
    expected_ml = _expected_ml()
    live_raw = 1.0 / np.asarray(live_decimals)
    expected_live = live_raw / live_raw.sum()
    expected_edges = MODEL_PROBS - expected_live

    assert [row["market_implied_prob"] for row in rows] == pytest.approx(expected_ml, abs=1e-6)
    assert [row["p_ml_implied"] for row in rows] == pytest.approx(expected_ml, abs=1e-6)
    assert [row["p_market_live"] for row in rows] == pytest.approx(expected_live, abs=1e-6)
    assert not np.allclose(expected_ml, expected_live)
    assert [row["value_score"] for row in rows] == pytest.approx(expected_edges, abs=1e-4)
    assert [row["edge_vs_live_market"] for row in rows] == pytest.approx(expected_edges, abs=1e-4)
    assert all(row["bet_tag"] in {"bet", "neutral", "underlay"} for row in rows)
