"""Paper trading: leak-free decisions, exact settlement, refunds, voids, CLV, baselines, verdict thresholds."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.services.chart_results_intake import ensure_chart_result_tables, ingest_chart_pdf
from src.services.paper_trading import (
    PaperPolicy, build_report, decide_bets, place_paper_bets, render_markdown, settle_paper_bets, summarize_bets,
)
from src.services.walk_forward_eval import load_graded_races  # noqa: F401  (import check: same schema helpers)

ROOT = Path(__file__).resolve().parents[1]
PDFS = sorted((ROOT / "data" / "raw" / "historical_results").glob("2026/04/*/eqb_CD_*_fullcard.pdf"))
D25 = PDFS[0]
POST = datetime(2026, 4, 25, 18, 0, tzinfo=timezone.utc)
RUN_TS = (POST - timedelta(hours=1)).isoformat()
CAPTURE_TS = (POST - timedelta(minutes=30)).isoformat()
SHA = "a" * 64


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("\n".join(
        l for l in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in l))
    ensure_chart_result_tables(conn)
    return conn


def _chart_db() -> sqlite3.Connection:
    conn = _db()
    ingest_chart_pdf(conn, D25)
    return conn


def _chart_race(conn, number):
    from src.services.chart_results_intake import load_chart_race
    rid = conn.execute("SELECT result_race_id FROM result_races WHERE race_key=?", (f"CD|2026-04-25|R{number}",)).fetchone()[0]
    return load_chart_race(conn, rid)


def make_card(conn, number, *, model, decimals, post=POST, run_ts=RUN_TS, capture_ts=CAPTURE_TS, ghost=None,
              extra_runs=(), scratched_on_card=()) -> tuple[int, dict[str, int]]:
    """A pre-race card whose field equals the chart's.  ``model`` / ``decimals`` are keyed by program number.
    ``ghost`` = (program, name): a runner active on the card that the chart shows as a scratch."""
    race = _chart_race(conn, number)
    trk = conn.execute("SELECT track_id FROM tracks WHERE abbrev='CD'").fetchone()
    if trk is None:
        conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('Churchill Downs','CD')")
        trk = conn.execute("SELECT track_id FROM tracks WHERE abbrev='CD'").fetchone()
    conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size, scheduled_post_time_utc) VALUES (?,?,?,?,?,?,?)",
        (trk[0], race.race_date.isoformat(), number, int(race.distance_furlongs * 220), race.surface, len(race.starters),
         post.isoformat()))
    card_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    rows = [(s.program, s.horse_name, s.post_position) for s in race.starters]
    if ghost:
        rows.append((ghost[0], ghost[1], 30))
    ids: dict[str, int] = {}
    for prog, name, pp in rows:
        conn.execute("INSERT OR IGNORE INTO horses (name) VALUES (?)", (name,))
        hid = conn.execute("SELECT horse_id FROM horses WHERE name=?", (name,)).fetchone()[0]
        conn.execute(
            "INSERT INTO entries (card_id, horse_id, post_position, program_number, morning_line_odds, scratch_flag) VALUES (?,?,?,?,?,?)",
            (card_id, hid, pp, prog, 5.0, 1 if prog in scratched_on_card else 0))
        ids[prog] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for k, ts in enumerate([run_ts, *extra_runs]):
        run_id = f"r{card_id}_{k}"
        conn.execute("INSERT INTO score_runs (run_id, card_id, run_timestamp) VALUES (?,?,?)", (run_id, card_id, ts))
        for prog, eid in ids.items():
            conn.execute(
                "INSERT INTO entry_scores (run_id, entry_id, horse_name, post_position, morning_line_odds, win_probability, p_model_pre_market) VALUES (?,?,?,?,?,?,?)",
                (run_id, eid, f"h{prog}", int(prog) if prog.isdigit() else 30, 5.0, model.get(prog, 0.0), model.get(prog, 0.0)))
    for prog, eid in ids.items():
        dec = decimals.get(prog)
        if dec is None:
            continue
        conn.execute(
            """INSERT INTO odds_snapshots (entry_id, snapshot_time, odds_numerator, odds_denominator, source, source_provider,
               source_artifact_sha256, program_number) VALUES (?,?,?,1.0,'book','draftkings',?,?)""",
            (eid, capture_ts, dec - 1.0, SHA, prog))
    conn.commit()
    return card_id, ids


# race 2 of the 2026-04-25 card: program 1 won at 2.01-1 ($4.02); program 5 second.  7 starters 1,5,7,4,8,2,6.
R2_PROBS = {"1": 0.40, "5": 0.20, "7": 0.12, "4": 0.10, "8": 0.08, "2": 0.06, "6": 0.04}
R2_DEC = {"1": 3.0, "5": 4.0, "7": 8.0, "4": 10.0, "8": 15.0, "2": 20.0, "6": 30.0}   # 1/dec sums to 0.633+...: see below
POLICY = PaperPolicy(max_bets_per_race=1)


def test_policy_id_is_a_stable_hash_of_its_parameters_and_bad_policies_are_rejected():
    assert PaperPolicy().policy_id == PaperPolicy().policy_id
    assert PaperPolicy(min_edge=0.03).policy_id != PaperPolicy().policy_id
    for bad in ({"staking": "martingale"}, {"probability_source": "x"}, {"flat_stake": 0}, {"min_decimal": 1.0},
                {"max_bets_per_race": 0}, {"min_decimal": 5.0, "max_decimal": 4.0}):
        with pytest.raises(ValueError):
            PaperPolicy(**bad)


def test_decision_math_by_hand():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    rep = decide_bets(conn, POLICY)
    total = sum(1 / d for d in R2_DEC.values())
    m1 = (1 / 3.0) / total
    assert len(rep.bets) == 1
    b = rep.bets[0]
    assert b.program == "1" and b.race_key == "CD|2026-04-25|R2"
    assert b.model_p == pytest.approx(0.40 / sum(R2_PROBS.values()))
    assert b.captured_decimal == pytest.approx(3.0, rel=1e-4)        # the schema stores implied_prob rounded to 6 dp
    assert b.market_p == pytest.approx(m1, rel=1e-4)
    assert b.ev == pytest.approx(b.model_p * b.captured_decimal - 1) and b.edge == pytest.approx(b.model_p - b.market_p)
    assert b.stake == 2.0 and b.capture_time.startswith("2026-04-25T17:30")
    # replay: the decision moment is when both the run and the price existed (the later of the two)
    assert b.decision_time.startswith("2026-04-25T17:30")


def test_no_bet_when_edge_or_price_gates_fail_and_reasons_are_counted():
    conn = _chart_db()
    flat = {p: 1 / 7 for p in R2_PROBS}
    make_card(conn, 2, model=flat, decimals={p: 7.0 for p in R2_PROBS})          # model == market: nothing to back
    rep = decide_bets(conn, POLICY)
    assert rep.bets == [] and rep.no_bet_races == ["CD|2026-04-25|R2"] and rep.races_decided == 1
    conn2 = _chart_db()
    make_card(conn2, 2, model=R2_PROBS, decimals={p: None for p in R2_PROBS})
    r2 = decide_bets(conn2, POLICY)
    assert r2.skipped["NO_VALID_PRICE"] == 1 and r2.races_decided == 0


def test_a_price_quoted_after_the_cutoff_or_a_run_after_the_cutoff_is_never_used():
    conn = _chart_db()
    late = (POST - timedelta(minutes=1)).isoformat()                               # inside the 2 minute lag
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC, capture_ts=late)
    assert decide_bets(conn, POLICY).skipped["NO_VALID_PRICE"] == 1
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC, run_ts=late)
    assert decide_bets(conn, POLICY).skipped["NO_SCORE_RUN_BEFORE_CUTOFF"] == 1


def test_the_last_run_before_the_cutoff_is_used_not_a_later_one():
    conn = _chart_db()
    cid, ids = make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC, extra_runs=((POST + timedelta(minutes=5)).isoformat(),))
    after = conn.execute("SELECT run_id FROM score_runs WHERE card_id=? ORDER BY run_timestamp DESC", (cid,)).fetchone()[0]
    conn.execute("UPDATE entry_scores SET win_probability=0.0 WHERE run_id=?", (after,))     # poisoned post-race run
    conn.execute("UPDATE entry_scores SET win_probability=1.0 WHERE run_id=? AND post_position=2", (after,))
    rep = decide_bets(conn, POLICY)
    assert [b.program for b in rep.bets] == ["1"] and not rep.bets[0].run_id.endswith("_1")


def test_stale_price_is_refused_in_live_mode_but_a_fresh_one_is_used():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    now_stale = POST - timedelta(minutes=5)                                          # price is 25 min old
    assert decide_bets(conn, POLICY, now=now_stale).skipped["STALE_PRICE"] == 1
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    rep = decide_bets(conn, POLICY, now=POST - timedelta(minutes=15))                # price 15 min old
    assert len(rep.bets) == 1 and rep.bets[0].decision_time.startswith("2026-04-25T17:45")


def test_live_mode_skips_races_already_inside_the_lag():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    rep = decide_bets(conn, POLICY, now=POST - timedelta(minutes=1))
    assert rep.bets == [] and rep.skipped["TOO_CLOSE_TO_POST_OR_PAST"] == 1


def test_decisions_do_not_depend_on_hindsight_scratch_flags():
    """Identical decisions whether or not the scratch flags were rewritten after the race (the charts are in both)."""
    def decisions(poison: bool):
        conn = _chart_db()
        cid, ids = make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
        if poison:
            conn.execute("UPDATE entries SET scratch_flag=1 WHERE entry_id=?", (ids["1"],))     # hindsight scratch flag
            conn.execute("UPDATE entries SET scratch_flag=1 WHERE entry_id=?", (ids["5"],))
        rep = decide_bets(conn, POLICY)
        return [(b.program, round(b.stake, 2), round(b.ev, 9), b.decision_time, b.capture_time) for b in rep.bets]
    assert decisions(False) == decisions(True) and decisions(True)


def test_replay_and_live_make_the_same_bet_when_live_acts_at_the_same_moment():
    conn_a, conn_b = _chart_db(), _chart_db()
    make_card(conn_a, 2, model=R2_PROBS, decimals=R2_DEC)
    make_card(conn_b, 2, model=R2_PROBS, decimals=R2_DEC)
    replay = decide_bets(conn_a, POLICY)
    live = decide_bets(conn_b, POLICY, now=POST - timedelta(minutes=20))
    key = lambda r: [(b.program, b.model_p, b.market_p, b.captured_decimal, b.stake) for b in r.bets]
    assert key(replay) == key(live) and key(replay)


def test_first_decision_stands_and_placing_twice_is_idempotent():
    conn = _chart_db()
    cid, ids = make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    first = place_paper_bets(conn, POLICY)
    assert len(first.bets) == 1
    conn.execute("UPDATE entry_scores SET win_probability=0.01")                      # the model "changes its mind"
    again = place_paper_bets(conn, POLICY)
    assert again.bets == [] and again.skipped["ALREADY_DECIDED"] == 1
    assert conn.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM paper_race_decisions").fetchone()[0] == 1


def test_different_policies_keep_separate_books():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    place_paper_bets(conn, POLICY)
    place_paper_bets(conn, PaperPolicy(min_edge=0.03))
    assert conn.execute("SELECT COUNT(*) FROM paper_policies").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0] == 2


def test_kelly_staking_is_capped_and_flat_is_flat():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    pol = PaperPolicy(staking="kelly", bankroll=1000.0, kelly_fraction=0.25, kelly_cap=0.02)
    b = decide_bets(conn, pol).bets[0]
    p = b.model_p
    f = (p * 3.0 - 1) / 2.0
    assert b.stake == pytest.approx(round(1000 * min(0.02, 0.25 * f), 2))
    assert b.stake <= 20.0
    pol2 = PaperPolicy(staking="kelly", bankroll=1000.0, kelly_fraction=0.01, kelly_cap=0.5)
    conn2 = _chart_db()
    make_card(conn2, 2, model=R2_PROBS, decimals=R2_DEC)
    assert decide_bets(conn2, pol2).bets[0].stake == pytest.approx(round(1000 * 0.01 * f, 2))


def test_low_confidence_block_and_bet_tag_gates():
    conn = _chart_db()
    cid, ids = make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    conn.execute("UPDATE entry_scores SET low_conf_bet_block=1 WHERE entry_id=?", (ids["1"],))
    assert decide_bets(conn, POLICY).bets == []
    conn = _chart_db()
    cid, ids = make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    assert decide_bets(conn, PaperPolicy(require_engine_bet_tag=True)).bets == []


# ---- settlement ----------------------------------------------------------------------------------
def test_a_winning_bet_returns_stake_times_the_exact_chart_payoff_over_two():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    place_paper_bets(conn, POLICY)
    assert settle_paper_bets(conn) == {"WON": 1}
    row = conn.execute("SELECT * FROM paper_bets").fetchone()
    assert row["status"] == "WON" and row["win_payoff"] == pytest.approx(4.02)
    assert row["return_amount"] == pytest.approx(4.02) and row["profit"] == pytest.approx(2.02)    # $2 stake -> $4.02
    assert row["final_decimal"] == pytest.approx(2.01) and row["clv"] == pytest.approx(3.0 / 2.01 - 1, rel=1e-4)
    assert settle_paper_bets(conn) == {}                                                           # idempotent


def test_a_losing_bet_loses_the_stake_and_clv_uses_the_final_price():
    conn = _chart_db()
    probs = {**R2_PROBS, "1": 0.05, "5": 0.45}
    make_card(conn, 2, model=probs, decimals={**R2_DEC, "5": 8.0})           # back program 5, who finished second at 3.17-1
    place_paper_bets(conn, POLICY)
    assert settle_paper_bets(conn) == {"LOST": 1}
    row = conn.execute("SELECT * FROM paper_bets").fetchone()
    assert row["program"] == "5" and row["profit"] == -2.0 and row["return_amount"] == 0
    assert row["final_decimal"] == pytest.approx(3.17) and row["clv"] == pytest.approx(8.0 / 3.17 - 1, rel=1e-4)


def test_a_bet_stays_open_until_a_chart_exists():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    place_paper_bets(conn, POLICY)
    conn.execute("UPDATE result_races SET is_current=0")
    assert settle_paper_bets(conn) == {"NO_CHART_YET": 1}
    assert conn.execute("SELECT status FROM paper_bets").fetchone()[0] == "OPEN"


def test_a_bet_is_held_when_the_card_and_the_chart_disagree():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals={**R2_DEC, "9": 50.0}, ghost=("9", "Not In Chart Horse"))
    place_paper_bets(conn, POLICY)
    assert settle_paper_bets(conn) == {"HELD_RECONCILIATION": 1}
    row = conn.execute("SELECT status, note FROM paper_bets").fetchone()
    assert row["status"] == "OPEN" and "CARD_RUNNER_NOT_IN_CHART" in row["note"]


def test_a_horse_scratched_after_the_bet_is_refunded():
    conn = _chart_db()
    race = _chart_race(conn, 2)
    scratch = race.scratches[0]
    model = {**R2_PROBS, "9": 0.45}
    make_card(conn, 2, model=model, decimals={**R2_DEC, "9": 4.0}, ghost=("9", scratch.horse_name))
    place_paper_bets(conn, POLICY)
    bet = conn.execute("SELECT program, horse_name FROM paper_bets").fetchone()
    assert bet["program"] == "9"
    assert settle_paper_bets(conn) == {"REFUNDED_SCRATCH": 1}
    row = conn.execute("SELECT status, profit, return_amount, stake FROM paper_bets").fetchone()
    assert row["status"] == "REFUNDED_SCRATCH" and row["profit"] == 0 and row["return_amount"] == row["stake"]
    s = summarize_bets([dict(row, model_p=0.4, clv=0.0, captured_decimal=3.0)])
    assert s["n_bets"] == 0 and s["n_refunded_scratch"] == 1


def test_a_dead_heat_is_voided_not_guessed():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    place_paper_bets(conn, POLICY)
    flags = json.loads(conn.execute("SELECT flags_json FROM result_races WHERE race_key='CD|2026-04-25|R2'").fetchone()[0])
    conn.execute("UPDATE result_races SET flags_json=? WHERE race_key='CD|2026-04-25|R2'", (json.dumps(flags + ["DEAD_HEAT"]),))
    assert settle_paper_bets(conn) == {"VOID_DEAD_HEAT": 1}
    row = conn.execute("SELECT status, profit FROM paper_bets").fetchone()
    assert row["status"] == "VOID_DEAD_HEAT" and row["profit"] == 0


# ---- report --------------------------------------------------------------------------------------
def _rows(n, *, win_every, decimal=5.0, stake=2.0, clv=0.1, model_p=0.2):
    rows = []
    for i in range(n):
        won = i % win_every == 0
        rows.append({"status": "WON" if won else "LOST", "stake": stake, "profit": stake * (decimal - 1) if won else -stake,
                     "model_p": model_p, "captured_decimal": decimal, "clv": clv})
    return rows


def test_summary_arithmetic_and_small_sample_verdict():
    rows = _rows(10, win_every=5)                                                   # 2 wins of 10 at 5.0 decimal
    s = summarize_bets(rows)
    assert s["n_bets"] == 10 and s["staked"] == 20 and s["profit"] == pytest.approx(2 * 8 - 8 * 2)
    assert s["roi"] == pytest.approx(0.0) and s["hit_rate"] == pytest.approx(0.2) and s["wins"] == 2
    assert s["verdict"].startswith("INSUFFICIENT_DATA") and s["clv_verdict"].startswith("INSUFFICIENT_DATA")
    assert summarize_bets([])["verdict"].startswith("INSUFFICIENT_DATA")


def test_verdicts_reach_a_conclusion_only_with_enough_bets_and_a_clear_interval():
    winning = summarize_bets(_rows(200, win_every=2, decimal=5.0, clv=0.08))        # 50% at 5.0: ROI +100%
    assert winning["verdict"] == "PROFITABLE_AFTER_TAKEOUT" and winning["clv_verdict"] == "BEATING_THE_CLOSE"
    losing = summarize_bets(_rows(200, win_every=20, decimal=5.0, clv=-0.08))       # 5%: ROI -75%
    assert losing["verdict"] == "LOSING" and losing["clv_verdict"] == "WORSE_THAN_THE_CLOSE"
    flat = summarize_bets(_rows(200, win_every=5, decimal=5.0, clv=0.0))            # ROI 0
    assert flat["verdict"] == "NOT_DISTINGUISHABLE_FROM_BREAKEVEN"
    assert flat["clv_verdict"] == "NOT_DISTINGUISHABLE_FROM_THE_CLOSE"


def test_max_drawdown_is_the_worst_peak_to_trough():
    rows = [{"status": "WON", "stake": 2, "profit": 6, "model_p": .3, "captured_decimal": 4, "clv": 0},
            {"status": "LOST", "stake": 2, "profit": -2, "model_p": .3, "captured_decimal": 4, "clv": 0},
            {"status": "LOST", "stake": 2, "profit": -2, "model_p": .3, "captured_decimal": 4, "clv": 0},
            {"status": "WON", "stake": 2, "profit": 6, "model_p": .3, "captured_decimal": 4, "clv": 0}]
    assert summarize_bets(rows)["max_drawdown"] == pytest.approx(4.0)


def test_report_end_to_end_with_baselines_and_markdown():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    place_paper_bets(conn, POLICY)
    settle_paper_bets(conn)
    rep = build_report(conn, POLICY.policy_id)
    assert rep["races_decided"] == 1 and rep["races_with_a_bet"] == 1 and rep["open_bets"] == 0
    assert rep["summary"]["n_bets"] == 1 and rep["summary"]["roi"] == pytest.approx(2.02 / 2.0)
    # every runner at flat $2: only the winner (4.02) pays -> 4.02 - 14 over 7 runners
    ev = rep["baselines"]["every_runner"]
    assert ev["n_bets"] == 7 and ev["profit"] == pytest.approx(4.02 - 14.0)
    # favourite = shortest captured price = program 1 (3.0): wins here
    assert rep["baselines"]["favourite"]["n_bets"] == 1 and rep["baselines"]["favourite"]["profit"] == pytest.approx(2.02)
    md = render_markdown(rep)
    assert "INSUFFICIENT DATA" in md and "pari-mutuel" in md and "Closing-line value" in md
    with pytest.raises(KeyError):
        build_report(conn, "nope")


def test_cli_place_settle_report_round_trip(tmp_path):
    import subprocess
    import sys
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript("\n".join(
        l for l in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in l))
    ensure_chart_result_tables(conn)
    ingest_chart_pdf(conn, D25)
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    conn.close()
    out = tmp_path / "out"
    base = [sys.executable, str(ROOT / "training" / "paper_trading.py"), "--db", str(db), "--out", str(out)]
    r = subprocess.run(base + ["place", "--backfill"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(base + ["settle"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "WON" in r.stdout, r.stdout + r.stderr
    r = subprocess.run(base + ["report"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert list(out.glob("*.md")) and list(out.glob("*.json"))


def test_deciding_needs_no_result_or_chart_tables_at_all():
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    with_charts = [(b.program, b.stake, b.decision_time) for b in decide_bets(conn, POLICY).bets]
    conn3 = _chart_db()
    make_card(conn3, 2, model=R2_PROBS, decimals=R2_DEC)
    for t in ("result_card_reconciliations", "result_payoffs", "result_scratches", "result_starters", "result_races", "result_sources"):
        conn3.execute(f"DROP TABLE {t}")
    without = [(b.program, b.stake, b.decision_time) for b in decide_bets(conn3, POLICY).bets]
    assert with_charts == without and with_charts


# ---- daily cycle ---------------------------------------------------------------------------------
def _cycle_db(tmp_path):
    """File DB with the pre-race card for race 2 but no chart yet, and a chart folder holding one card."""
    import shutil
    db = tmp_path / "c.db"
    conn = _chart_db()
    make_card(conn, 2, model=R2_PROBS, decimals=R2_DEC)
    disk = sqlite3.connect(db)
    conn.backup(disk)
    disk.execute("DELETE FROM result_card_reconciliations")
    for t in ("result_payoffs", "result_scratches", "result_starters", "result_races", "result_sources"):
        disk.execute(f"DELETE FROM {t}")
    disk.commit()
    disk.row_factory = sqlite3.Row
    charts = tmp_path / "charts"
    charts.mkdir()
    shutil.copy(D25, charts / D25.name)
    return disk, charts


def test_daily_cycle_ingests_populates_settles_reports_and_is_idempotent(tmp_path):
    from training.daily_cycle import run_cycle
    conn, charts = _cycle_db(tmp_path)
    place_paper_bets(conn, POLICY)
    first = run_cycle(conn, charts, tmp_path / "out", n_boot=200)
    assert first["charts"]["new"] == 1 and first["charts"]["populated"] == 1
    assert first["settled"] == {"WON": 1} and first["problems"] == []
    assert conn.execute("SELECT COUNT(*) FROM race_results").fetchone()[0] >= 7
    assert first["reports"][0]["settled_bets"] == 1 and first["reports"][0]["roi"] == pytest.approx(2.02 / 2)
    assert list((tmp_path / "out").glob("*_cycle.json")) and list((tmp_path / "out").glob("*.md"))
    second = run_cycle(conn, charts, tmp_path / "out", n_boot=200)
    assert second["charts"]["new"] == 0 and second["charts"]["already"] == 1 and second["settled"] == {}


def test_daily_cycle_keeps_going_past_a_corrupt_file_and_reports_it(tmp_path):
    from training.daily_cycle import run_cycle, main
    conn, charts = _cycle_db(tmp_path)
    (charts / "eqb_XX_2026-04-26_fullcard.pdf").write_bytes(b"not a pdf")
    place_paper_bets(conn, POLICY)
    res = run_cycle(conn, charts, tmp_path / "out", n_boot=200)
    assert any("unreadable" in p for p in res["problems"]) and res["settled"] == {"WON": 1}
    conn.close()
    assert main(["--db", str(tmp_path / "c.db"), "--root", str(charts), "--out", str(tmp_path / "o2"), "--boot", "100"]) == 1


def test_daily_cycle_with_open_bets_and_no_chart_yet_is_not_a_failure(tmp_path):
    from training.daily_cycle import main
    conn, charts = _cycle_db(tmp_path)
    place_paper_bets(conn, POLICY)
    conn.close()
    empty = tmp_path / "none"
    empty.mkdir()
    assert main(["--db", str(tmp_path / "c.db"), "--root", str(empty), "--out", str(tmp_path / "o"), "--boot", "100"]) == 0
