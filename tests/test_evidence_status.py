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
