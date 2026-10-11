"""Pre-threshold WIN / PLACE / SHOW picks, their pre-post storage, hit/miss settlement and the gate-leak fix."""
from __future__ import annotations

import sqlite3

import pytest

from src.services import bet_picks as bp
from src.services.results_intake import evaluate_score_run
from tests.conftest import insert_minimal_race

MODEL = [21.1, 12.8, 12.1, 11.7, 9.9, 9.8, 7.1, 7.1, 4.8, 3.6]        # CT R10 diagnostic forecast
ML = [13.9, 15.3, 10.9, 8.5, 4.8, 27.4, 4.8, 4.8, 4.8, 4.8]            # its morning-line implied %


def _runners(model=MODEL, ml=ML):
    return [dict(entry_id=i + 1, horse_name=f"H{i + 1}", post_position=i + 1, model_win=m / 100, market_win=k / 100,
                morning_line=(100 / k - 1) if k else None)
            for i, (m, k) in enumerate(zip(model, ml))]


def test_harville_place_sums_to_two_and_show_to_three():
    place, show = bp.harville_place_show([m / 100 for m in MODEL])
    assert sum(place) == pytest.approx(2.0, abs=1e-9)
    assert sum(show) == pytest.approx(3.0, abs=1e-9)
    assert all(0 < w <= p <= s <= 1 for w, p, s in zip([m / 100 for m in MODEL], place, show))


def test_picks_flag_the_horses_the_model_likes_more_than_the_morning_line():
    picks = {p["horse_name"]: p for p in bp.recommend_picks(_runners())}
    assert picks["H1"]["bet_types"] == ["WIN", "PLACE", "SHOW"] and picks["H1"]["label"] == "WIN+PLACE+SHOW"
    assert picks["H5"]["bet_types"] == ["WIN"]                         # 9.9% vs 4.8% ML
    assert "H6" not in picks                                          # the model rates the 2-1 shot below the ML
    leg = picks["H1"]["legs"]["SHOW"]
    assert leg["breakeven_payout"] == pytest.approx(2.0 / leg["model_prob"])
    assert len(picks) <= bp.MAX_PICKS


def test_tiny_field_or_missing_odds_gives_no_picks():
    assert bp.recommend_picks(_runners(MODEL[:4], ML[:4])) == []
    assert bp.recommend_picks(_runners(ml=[0] * 10)) == []


def _seed_run(conn, post_utc, run_ts):
    ids = insert_minimal_race(conn)
    conn.execute("UPDATE race_cards SET scheduled_post_time_utc=? WHERE card_id=?", (post_utc, ids["card_id"]))
    conn.execute("UPDATE score_runs SET run_timestamp=? WHERE run_id=?", (run_ts, ids["run_id"]))
    conn.commit()
    return ids


def test_picks_are_recorded_only_when_the_run_is_before_post_and_only_once(mem_conn):
    ids = _seed_run(mem_conn, "2026-10-10T23:00:00+00:00", "2026-10-10T22:00:00+00:00")
    picks = bp.recommend_picks(_runners())
    n, why = bp.persist_picks_pre_post(mem_conn, ids["run_id"], ids["card_id"], picks)
    assert why == "recorded" and n == sum(len(p["legs"]) for p in picks)
    assert bp.persist_picks_pre_post(mem_conn, ids["run_id"], ids["card_id"], picks)[0] == 0      # rerun adds nothing
    assert len(bp.stored_picks(mem_conn, ids["run_id"])) == n


def test_picks_not_recorded_after_post_or_without_a_post_time(mem_conn):
    late = _seed_run(mem_conn, "2026-10-10T23:00:00+00:00", "2026-10-11T00:00:00+00:00")
    assert bp.persist_picks_pre_post(mem_conn, late["run_id"], late["card_id"], bp.recommend_picks(_runners()))[0] == 0
    assert bp.stored_picks(mem_conn, late["run_id"]) == []


def test_settlement_is_hit_miss_by_bet_type_and_void_when_scratched():
    legs = [{"entry_id": 1, "bet_type": "WIN", "model_prob": .2}, {"entry_id": 1, "bet_type": "SHOW", "model_prob": .5},
            {"entry_id": 2, "bet_type": "PLACE", "model_prob": .4}, {"entry_id": 3, "bet_type": "SHOW", "model_prob": .4},
            {"entry_id": 4, "bet_type": "SHOW", "model_prob": .4}]
    out = bp.settle_legs(legs, {1: 3, 2: 3, 3: None})
    assert [o["status"] for o in out] == ["MISS", "HIT", "MISS", "VOID", "VOID"]
    summary = bp.summarize(out)
    assert summary["SHOW"]["legs"] == 1 and summary["SHOW"]["hits"] == 1 and "WIN" in summary


def test_gated_run_bet_tags_are_not_graded(mem_conn):
    ids = insert_minimal_race(mem_conn)
    mem_conn.execute("UPDATE entry_scores SET bet_tag='bet' WHERE run_id=? AND rank=2", (ids["run_id"],))
    for i, (eid, hid) in enumerate(zip(ids["entry_ids"], ids["horse_ids"]), start=1):
        mem_conn.execute(
            "INSERT INTO race_results (card_id, entry_id, horse_id, official_finish, finish_position, is_scratched, "
            "ingested_at) VALUES (?,?,?,?,?,0,'2026-05-08T12:00:00Z')", (ids["card_id"], eid, hid, i, i))
    mem_conn.commit()
    shown = evaluate_score_run(mem_conn, ids["run_id"], ids["card_id"])
    withheld = evaluate_score_run(mem_conn, ids["run_id"], ids["card_id"], bet_tags_valid=False)
    assert shown["n_bets"] == 1 and shown["bet_tags_withheld"] is False
    assert withheld["n_bets"] == 0 and withheld["n_bets_itm"] == 0 and withheld["kelly_roi_pct"] is None
    assert withheld["bet_tags_withheld"] is True


# ── The four conditions on the pre-threshold exception ──────────────────────────────────────────────────────────

def test_claude_md_keeps_the_diagnostic_rule_and_limits_the_unconfirmed_exception():
    text = open("CLAUDE.md", encoding="utf-8").read()
    assert "never used for real bets, fair odds or edge" in text                   # the original rule is unchanged
    line = next(l for l in text.splitlines() if "PROPOSED exception" in l)
    for needed in ("not in force until the owner confirms", "personal wagering", "uncalibrated seed baseline",
                   "differ from the morning line", "side by side", "unvalidated", "more than 2x", "INSUFFICIENT DATA below 30",
                   "engine_version", "paper-trading counts", "never computes a stake", "recommended wager"):
        assert needed in line, needed
    assert "no fair odds" not in line and "no edge" not in line                    # the old wording is gone


def test_morning_line_collapse_is_labelled_baseline_and_yields_no_picks():
    collapsed = [dict(entry_id=i + 1, horse_name=f"H{i + 1}", post_position=i + 1, model_win=None, market_win=k / 100)
                 for i, k in enumerate(ML)]
    assert bp.forecast_source(collapsed) == bp.ML_BASELINE and bp.recommend_picks(collapsed) == []
    same = _runners(model=ML, ml=ML)                                  # model vector equal to the market: still a baseline
    assert bp.forecast_source(same) == bp.ML_BASELINE and bp.recommend_picks(same) == []
    assert bp.forecast_source(_runners()) == bp.ENGINE_FORECAST
    assert "MORNING-LINE BASELINE" in bp.ML_BASELINE_NOTE and "no pick is made" in bp.ML_BASELINE_NOTE


def test_stored_picks_carry_engine_version_source_and_stay_out_of_status_and_walk_forward(mem_conn):
    from datetime import date
    from src.services.evidence_status import build_status
    from src.services.walk_forward_eval import load_graded_races
    ids = _seed_run(mem_conn, "2026-10-10T23:00:00+00:00", "2026-10-10T22:00:00+00:00")
    mem_conn.execute("UPDATE score_runs SET engine_version='v-test' WHERE run_id=?", (ids["run_id"],))
    mem_conn.commit()
    before = (build_status(mem_conn, today=date(2026, 10, 11)), load_graded_races(mem_conn))
    picks = bp.recommend_picks(_runners())
    assert bp.persist_picks_pre_post(mem_conn, ids["run_id"], ids["card_id"], picks)[1] == "recorded"
    rows = bp.stored_picks(mem_conn, ids["run_id"])
    assert rows and {r["engine_version"] for r in rows} == {"v-test"} and {r["source"] for r in rows} == {bp.ENGINE_FORECAST}
    assert {r["picks_version"] for r in rows} == {bp.PICKS_VERSION}
    assert (build_status(mem_conn, today=date(2026, 10, 11)), load_graded_races(mem_conn)) == before
    mem_conn.execute("UPDATE score_runs SET run_timestamp='2026-10-11T00:00:00+00:00' WHERE run_id=?", (ids["run_id"],))
    mem_conn.execute("DELETE FROM bet_picks")
    assert bp.persist_picks_pre_post(mem_conn, ids["run_id"], ids["card_id"], picks)[0] == 0      # after post: not stored


def test_board_shows_model_breakeven_and_market_price_side_by_side_with_no_stake(mem_conn):
    picks = bp.recommend_picks(_runners())
    frame = bp.picks_frame(picks)
    assert {"Morning line", "Break-even payout per $2", "Warning", "Source"} <= set(frame.columns)
    assert any("model / ML-implied" in c for c in frame.columns)
    assert frame.loc[frame["Horse"] == "H1", "Morning line"].iloc[0] == "6-1"
    banned = ("stake", "kelly", "recommend")
    assert not [c for c in frame.columns if any(b in c.lower() for b in banned)]
    bp.persist_picks(mem_conn, "r", 1, picks)
    assert not [r[1] for r in mem_conn.execute("PRAGMA table_info(bet_picks)") if any(b in r[1].lower() for b in banned)]
    assert "PRE-THRESHOLD" in bp.LABEL and "not validated" in bp.LABEL


def test_longshot_warning_when_model_probability_is_more_than_twice_the_market():
    picks = {p["horse_name"]: p for p in bp.recommend_picks(_runners())}
    assert picks["H5"]["longshot_warning"] is True                        # WIN 9.9% vs ML 4.8%: 2.06x
    assert picks["H4"]["longshot_warning"] is False if "H4" in picks else True
    frame = bp.picks_frame(list(picks.values()))
    assert frame.loc[frame["Horse"] == "H5", "Warning"].iloc[0].startswith("LONGSHOT")
    assert "2x" in bp.LONGSHOT_NOTE


def test_track_record_is_insufficient_below_30_then_reports_hit_rate_and_return():
    mem_conn = sqlite3.connect(":memory:")
    mem_conn.executescript(bp.DDL)
    mem_conn.execute("CREATE TABLE race_results (card_id INTEGER, entry_id INTEGER, finish_position INTEGER, "
                     "official_finish INTEGER, is_scratched INTEGER DEFAULT 0)")
    for engine in ("v-test",):
        for i in range(1, 36):                       # 35 SHOW legs, model_prob .4, every third one finishes 4th (miss)
            mem_conn.execute(
                "INSERT INTO bet_picks (run_id, card_id, entry_id, horse_name, bet_type, model_prob, market_prob, "
                "breakeven_payout, picks_version, engine_version, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"r{i}", i, i, f"H{i}", "SHOW", .4, .3, 5.0, bp.PICKS_VERSION, engine, bp.ENGINE_FORECAST, "2026-10-10"))
            mem_conn.execute("INSERT INTO race_results VALUES (?,?,?,?,0)", (i, i, 4 if i % 3 == 0 else 2, 4 if i % 3 == 0 else 2))
    # an older engine version must never be counted
    mem_conn.execute(
        "INSERT INTO bet_picks (run_id, card_id, entry_id, horse_name, bet_type, model_prob, market_prob, breakeven_payout, "
        "picks_version, engine_version, source, created_at) VALUES ('old',99,99,'X','SHOW',.4,.3,5,?, 'v-old', ?, '2026-01-01')",
        (bp.PICKS_VERSION, bp.ENGINE_FORECAST))
    rec = bp.track_record(mem_conn)
    assert rec["engine_version"] == "v-test" and rec["other_engine_versions_not_counted"] == ["v-old"]
    show = rec["by_type"]["SHOW"]
    assert show["graded"] == 35 and show["hits"] == 24 and show["sufficient"]
    assert show["hit_rate"] == pytest.approx(24 / 35) and show["expected_hit_rate"] == pytest.approx(.4)
    assert "return_per_2" not in show                       # no stored chart payoffs: return is not claimed
    text = " ".join(bp.render_track_record(rec))
    assert "no stored chart payoffs" in text and "PRE-THRESHOLD" in text and "vs the model's own expected 40%" in text
    mem_conn.execute("DELETE FROM bet_picks WHERE card_id > 10")                  # 10 graded legs left
    thin = bp.render_track_record(bp.track_record(mem_conn))[0]
    assert "INSUFFICIENT DATA" in thin and "10 picks graded" in thin and "hit rate" not in thin


def test_picks_are_limited_to_the_gate_blocked_seed_baseline():
    import pandas as pd
    blocked = pd.DataFrame({"win_probability": [None] * 3, "diagnostic_win_probability": [.5, .3, .2]})
    valid = pd.DataFrame({"win_probability": [.5, .3, .2], "diagnostic_win_probability": [.5, .3, .2]})
    nothing = pd.DataFrame({"win_probability": [None] * 3, "diagnostic_win_probability": [None] * 3})
    assert bp.picks_allowed(blocked, "seed_only_baseline")
    assert not bp.picks_allowed(valid, "seed_only_baseline")            # a valid, ungated board is outside the exception
    assert not bp.picks_allowed(blocked, "xgboost") and not bp.picks_allowed(blocked, "fallback")
    assert not bp.picks_allowed(nothing, "seed_only_baseline") and not bp.picks_allowed(None, "seed_only_baseline")


# ── Return at chart payoffs, end to end on the real schema ──────────────────────────────────────────────────────

def _seed_two_show_picks(conn, *, with_chart: bool):
    """Alpha SHOW (model .5) finishes 2nd; Bravo SHOW (model .3) finishes 4th.  The chart pays $4.02 to show on Alpha."""
    ids = insert_minimal_race(conn)
    for n, eid in enumerate(ids["entry_ids"], start=1):
        conn.execute("UPDATE entries SET program_number=? WHERE entry_id=?", (str(n), eid))
    for finish, eid, hid in zip([2, 4, 1, 3, 5], ids["entry_ids"], ids["horse_ids"]):
        conn.execute("INSERT INTO race_results (card_id, entry_id, horse_id, finish_position, official_finish, is_scratched, "
                     "ingested_at) VALUES (?,?,?,?,?,0,'2026-05-02T20:00:00Z')", (ids["card_id"], eid, hid, finish, finish))
    legs = lambda p: {"SHOW": {"model_prob": p, "market_prob": .3, "breakeven_payout": 2 / p}}
    bp.persist_picks(conn, ids["run_id"], ids["card_id"], [
        {"entry_id": ids["entry_ids"][0], "horse_name": "Alpha", "bet_types": ["SHOW"], "legs": legs(.5), "source": bp.ENGINE_FORECAST},
        {"entry_id": ids["entry_ids"][1], "horse_name": "Bravo", "bet_types": ["SHOW"], "legs": legs(.3), "source": bp.ENGINE_FORECAST}])
    conn.executescript("DROP TABLE IF EXISTS result_starters; DROP TABLE IF EXISTS result_races;")
    if with_chart:
        conn.executescript("""
            CREATE TABLE result_races (result_race_id INTEGER PRIMARY KEY, track_code TEXT, race_date TEXT, race_number INTEGER,
                                       is_current INTEGER);
            CREATE TABLE result_starters (result_race_id INTEGER, program TEXT, win_payoff REAL, place_payoff REAL,
                                          show_payoff REAL);
            INSERT INTO result_races VALUES (1, 'CD', '2026-05-02', 1, 1);
            INSERT INTO result_starters VALUES (1, '1', NULL, 6.10, 4.02), (1, '2', NULL, NULL, NULL);""")
    conn.commit()
    return ids


def test_return_at_chart_payoffs_is_hand_computed_end_to_end(mem_conn, monkeypatch):
    monkeypatch.setattr(bp, "MIN_PICKS_FOR_RECORD", 2)                      # two legs are enough for this arithmetic check
    ids = _seed_two_show_picks(mem_conn, with_chart=True)
    assert bp.chart_payoffs_by_entry(mem_conn, ids["card_id"])[ids["entry_ids"][0]] == (None, 6.10, 4.02)
    show = bp.track_record(mem_conn)["by_type"]["SHOW"]
    # Alpha: $2 show ticket returns $4.02 -> net +2.02.  Bravo finished 4th -> net -2.00.  Mean net = 0.01 per $2 staked.
    assert show["graded"] == 2 and show["hits"] == 1 and show["money_legs"] == 2
    assert show["hit_rate"] == pytest.approx(0.5) and show["expected_hit_rate"] == pytest.approx(0.4)
    assert show["return_per_2"] == pytest.approx(0.01)
    # sample sd of (+2.02, -2.00) is 2.01*sqrt(2); half-width = 1.96 * sd / sqrt(2) = 1.96 * 2.01 = 3.9396
    assert show["return_ci"] == pytest.approx((0.01 - 3.9396, 0.01 + 3.9396))
    text = " ".join(bp.render_track_record(bp.track_record(mem_conn)))
    assert "+0.01 per $2 staked" in text and "2 legs" in text


def test_race_without_a_stored_chart_counts_for_hit_rate_but_not_return(mem_conn, monkeypatch):
    monkeypatch.setattr(bp, "MIN_PICKS_FOR_RECORD", 2)
    ids = _seed_two_show_picks(mem_conn, with_chart=False)
    assert bp.chart_payoffs_by_entry(mem_conn, ids["card_id"]) == {}
    show = bp.track_record(mem_conn)["by_type"]["SHOW"]
    assert show["graded"] == 2 and show["hits"] == 1 and show["hit_rate"] == pytest.approx(0.5)
    assert show["money_legs"] == 0 and "return_per_2" not in show        # a loss is known but the win's payoff is not: no ROI
    text = " ".join(bp.render_track_record(bp.track_record(mem_conn)))
    assert "hit rate 50%" in text and "return not computed: no stored chart payoffs" in text
