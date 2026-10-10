"""Evidence status: counts, pace gating, projection arithmetic, read-only CLI."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from src.services.evidence_status import _metric, build_status, render_text
from tests.test_walk_forward_eval import add_race, new_db  # noqa: E402  (conftest-less import of the shared builder)

TODAY = date(2026, 4, 1)


def test_reached_when_enough():
    m = _metric("x", 3, [TODAY] * 3, TODAY)
    assert m["reached"] and m["status"] == "REACHED" and m["projected_date"] is None


def test_no_data_and_pace_not_established_early():
    assert _metric("x", 30, [], TODAY)["status"] == "NO DATA YET"
    early = _metric("x", 30, [TODAY - timedelta(days=5)] * 8, TODAY)          # 5 days of history
    assert early["status"].startswith("pace not established") and early["projected_date"] is None
    few = _metric("x", 30, [TODAY - timedelta(days=40)] * 3, TODAY)           # long history, only 3 events
    assert few["status"].startswith("pace not established")


def test_projection_arithmetic():
    days = [TODAY - timedelta(days=28)] * 10                                    # 10 events over 28 days = 2.5 / week
    m = _metric("x", 30, days, TODAY)
    assert m["pace_per_week"] == 2.5 and m["projected_date"] == (TODAY + timedelta(days=56)).isoformat()   # 20 more / (10/28)
    assert "20 to go" in m["status"]


def test_counts_from_a_real_schema_and_read_only_cli(tmp_path, capsys):
    db = tmp_path / "s.db"
    conn = new_db(str(db))
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)                               # scored + graded
    add_race(conn, day=3, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0, results=False)                # scored, not graded
    add_race(conn, day=4, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0, run_ts="2026-03-04T20:00:00.000Z")   # run after post
    st = build_status(conn, today=date(2026, 3, 31))
    scored, graded, bets, clv = st["metrics"]
    assert scored["have"] == 2 and graded["have"] == 1 and bets["have"] == 0 and clv["have"] == 0
    assert st["excluded_from_grading"]
    assert "Evidence status" in render_text(st)
    conn.close()
    from training.status import main
    before = db.read_bytes()
    assert main(["--db", str(db)]) == 0 and "Races graded" in capsys.readouterr().out
    assert db.read_bytes() == before
    assert main(["--db", str(tmp_path / "missing.db")]) == 1


def test_settled_bets_are_counted_by_decision_day():
    from src.services.paper_trading import ensure_paper_tables
    conn = new_db()
    ensure_paper_tables(conn)
    conn.execute("INSERT INTO paper_policies VALUES ('p','n','{}','t')")
    for i, (status, clv) in enumerate([("WON", 0.1), ("LOST", -0.1), ("OPEN", None), ("REFUNDED_SCRATCH", None)]):
        conn.execute(
            """INSERT INTO paper_bets (policy_id, card_id, race_key, entry_id, run_id, decision_time, capture_time, capture_provider,
               post_utc, model_p, market_p, captured_decimal, edge, ev, stake, status, clv)
               VALUES ('p',?,?,?, 'r','2026-03-10T15:00:00+00:00','t','dk','t',.3,.2,4,.1,.2,2,?,?)""",
            (i, f"CD|2026-03-10|R{i}", i, status, clv))
    st = build_status(conn, today=date(2026, 4, 1))
    assert st["metrics"][2]["have"] == 2 and st["metrics"][3]["have"] == 2 and st["open_bets"] == 1


def _collapse(conn, card_day: int, status: str | None = "MODEL_COLLAPSED_TO_ML_PRIOR"):
    """Make the run on that day's card look like the engine collapsed to the morning line: NULL probabilities."""
    conn.execute("""UPDATE entry_scores SET win_probability=NULL WHERE run_id IN (
                      SELECT s.run_id FROM score_runs s JOIN race_cards rc ON rc.card_id=s.card_id WHERE rc.card_date=?)""",
                 (f"2026-03-{card_day:02d}",))
    conn.execute("""UPDATE score_runs SET model_collapse_status=? WHERE card_id IN (
                      SELECT card_id FROM race_cards WHERE card_date=?)""", (status, f"2026-03-{card_day:02d}"))


def test_collapsed_races_are_not_counted_as_scored_or_graded():
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)                      # real probabilities
    add_race(conn, day=3, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)                      # will collapse, has a result
    add_race(conn, day=4, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0, results=False)       # will collapse, no result yet
    _collapse(conn, 3)
    _collapse(conn, 4, status=None)                                                        # guard fired but status not stored
    st = build_status(conn, today=date(2026, 3, 31))
    scored, graded, *_ = st["metrics"]
    assert scored["have"] == 1 and graded["have"] == 1
    assert st["scored_collapsed_to_morning_line"] == 2
    assert st["model_collapse_status"] == {"MODEL_COLLAPSED_TO_ML_PRIOR": 1, "(none stored)": 2}   # healthy run + the unstored collapse
    assert st["excluded_from_grading"]["NO_MODEL_PROBABILITIES"] == 1
    text = render_text(st)
    assert "collapsed to the morning line" in text and "MODEL_COLLAPSED_TO_ML_PRIOR=1" in text


def test_the_run_used_for_grading_decides_not_an_earlier_one():
    conn = new_db()
    # an earlier pre-post run has real probabilities, the later pre-post run (the one grading uses) collapsed
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0, run_ts="2026-03-02T14:00:00.000Z",
             extra_runs=["2026-03-02T15:00:00.000Z"])
    last = conn.execute("SELECT run_id FROM score_runs WHERE run_timestamp='2026-03-02T15:00:00.000Z'").fetchone()[0]
    conn.execute("UPDATE entry_scores SET win_probability=NULL WHERE run_id=?", (last,))
    st = build_status(conn, today=date(2026, 3, 31))
    assert st["metrics"][0]["have"] == 0 and st["metrics"][1]["have"] == 0 and st["scored_collapsed_to_morning_line"] == 1


def test_scratched_runner_without_probability_does_not_count_as_collapse():
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0, scratched=(2,))
    conn.execute("UPDATE entries SET scratch_flag=1 WHERE post_position=3")
    conn.execute("UPDATE entry_scores SET win_probability=NULL WHERE post_position=3")
    st = build_status(conn, today=date(2026, 3, 31))
    assert st["metrics"][0]["have"] == 1 and st["scored_collapsed_to_morning_line"] == 0


def test_database_without_collapse_status_column_still_reports(tmp_path):
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    conn.execute("ALTER TABLE score_runs DROP COLUMN model_collapse_status")
    st = build_status(conn, today=date(2026, 3, 31))
    assert st["metrics"][0]["have"] == 1 and st["model_collapse_status"] == {"(none stored)": 1}


def test_status_labels_seed_baseline_races_as_diagnostic_evidence():
    from src.services.walk_forward_eval import DIAGNOSTIC_SEED, TRAINED
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)                     # seed baseline, scored + graded
    add_race(conn, day=3, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0, results=False)      # trained model, scored only
    conn.execute("UPDATE score_runs SET model_type='seed_only_baseline' WHERE card_id=(SELECT card_id FROM race_cards WHERE card_date='2026-03-02')")
    conn.execute("UPDATE score_runs SET model_type='xgboost' WHERE card_id=(SELECT card_id FROM race_cards WHERE card_date='2026-03-03')")
    st = build_status(conn, today=date(2026, 3, 31))
    assert st["forecast_class"] == {"scored": {DIAGNOSTIC_SEED: 1, TRAINED: 1}, "graded": {DIAGNOSTIC_SEED: 1}}
    text = render_text(st)
    assert f"{DIAGNOSTIC_SEED}=1/1" in text and f"{TRAINED}=1/0" in text and "evidence only" in text


def test_status_has_no_diagnostic_note_when_nothing_is_a_seed_baseline():
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    conn.execute("UPDATE score_runs SET model_type='xgboost'")
    assert "DIAGNOSTIC_SEED_BASELINE" not in render_text(build_status(conn, today=date(2026, 3, 31)))
