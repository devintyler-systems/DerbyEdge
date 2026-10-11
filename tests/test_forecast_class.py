"""One rule for what kind of forecast a run is, shared by grading, status and paper trading."""
from __future__ import annotations

import sqlite3

from src.services.forecast_class import DIAGNOSTIC_SEED, OTHER, TRAINED, forecast_class, forecast_classes_for_runs
from tests.test_walk_forward_eval import add_race, new_db


def test_only_a_seed_baseline_is_diagnostic_only_xgboost_is_trained_everything_else_is_kept_apart():
    assert forecast_class("seed_only_baseline") == forecast_class("  Seed_Only_Baseline ") == DIAGNOSTIC_SEED
    assert forecast_class("xgboost") == TRAINED
    assert forecast_class("fallback") == forecast_class("derby_override") == forecast_class(None) == forecast_class("") == OTHER
    assert forecast_class("xgboost_calibrated") == OTHER                      # no prefix guessing


def test_classes_are_read_from_the_runs_and_a_missing_run_is_never_assumed_trained():
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    add_race(conn, day=3, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    conn.execute("UPDATE score_runs SET model_type='seed_only_baseline' WHERE card_id=(SELECT MIN(card_id) FROM race_cards)")
    conn.execute("UPDATE score_runs SET model_type='xgboost' WHERE card_id=(SELECT MAX(card_id) FROM race_cards)")
    runs = [r[0] for r in conn.execute("SELECT run_id FROM score_runs ORDER BY card_id")]
    got = forecast_classes_for_runs(conn, runs + ["no-such-run", None])
    assert got == {runs[0]: DIAGNOSTIC_SEED, runs[1]: TRAINED, "no-such-run": OTHER}
    assert forecast_classes_for_runs(conn, []) == {}


def test_a_score_runs_table_without_model_type_classifies_every_run_as_other():
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    run = conn.execute("SELECT run_id FROM score_runs").fetchone()[0]
    conn.execute("DROP VIEW race_review")
    conn.execute("ALTER TABLE score_runs DROP COLUMN model_type")
    assert forecast_classes_for_runs(conn, [run]) == {run: OTHER}
