"""Walk-forward evaluation over stored score runs: as-of guards, exclusions, benchmarks, retraining folds."""
from __future__ import annotations

import json
import math
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.analysis.forecast_metrics import devig
from src.services.walk_forward_eval import (
    FORECASTS, GradedRace, RaceInput, evaluate, load_graded_races, per_race_rows, render_markdown, walk_forward_oof,
)
from training.walk_forward import main as cli_main

ROOT = Path(__file__).resolve().parents[1]
POST = "2026-03-{d:02d}T19:00:00+00:00"


def new_db(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("\n".join(
        l for l in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in l))
    from src.services.results_intake import _ensure_table
    _ensure_table(conn)
    return conn


_seq = {"horse": 0, "run": 0}


def add_race(
    conn, *, day: int, number: int = 1, probs, ml, dec_odds=None, winner: int = 0, scratched=(), post="auto",
    run_ts="2026-03-{d:02d}T15:00:00.000Z", extra_runs=(), track="CD", surface="dirt", furlongs=6.0, finish_override=None,
    pre=None, results=True, with_run=True,
) -> int:
    """probs/ml/dec_odds are per entry (all entries, including scratched ones)."""
    trk = conn.execute("SELECT track_id FROM tracks WHERE abbrev=?", (track,)).fetchone()
    if trk is None:
        conn.execute("INSERT INTO tracks (name, abbrev) VALUES (?, ?)", (track, track))
        trk = conn.execute("SELECT track_id FROM tracks WHERE abbrev=?", (track,)).fetchone()
    post_val = POST.format(d=day) if post == "auto" else post
    conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, scheduled_post_time_utc) VALUES (?,?,?,?,?,?)",
        (trk[0], f"2026-03-{day:02d}", number, int(furlongs * 220), surface, post_val))
    card_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    entry_ids = []
    for i, (p, m) in enumerate(zip(probs, ml), start=1):
        _seq["horse"] += 1
        conn.execute("INSERT INTO horses (name) VALUES (?)", (f"Horse {_seq['horse']}",))
        hid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO entries (card_id, horse_id, post_position, program_number, morning_line_odds, scratch_flag) VALUES (?,?,?,?,?,0)",
            (card_id, hid, i, str(i), m))
        entry_ids.append((conn.execute("SELECT last_insert_rowid()").fetchone()[0], hid, i))
    runs = [(run_ts.format(d=day), True)] if with_run else []
    runs += [(t, False) for t in extra_runs]
    for ts, _main in runs:
        _seq["run"] += 1
        run_id = f"run{_seq['run']}"
        conn.execute("INSERT INTO score_runs (run_id, card_id, run_timestamp) VALUES (?,?,?)", (run_id, card_id, ts))
        for (eid, _hid, pp), p, m in zip(entry_ids, probs, ml):
            pre_p = None if pre is None else pre[pp - 1]
            conn.execute(
                """INSERT INTO entry_scores (run_id, entry_id, horse_name, post_position, morning_line_odds, win_probability,
                   p_model_pre_market) VALUES (?,?,?,?,?,?,?)""", (run_id, eid, f"h{pp}", pp, m, p, pre_p))
    if results:
        for k, (eid, hid, pp) in enumerate(entry_ids):
            scr = k in scratched
            finish = None if scr else (1 if k == winner else 2 + k)
            if finish_override and k in finish_override:
                finish = finish_override[k]
            dec = None if (scr or dec_odds is None) else dec_odds[k]
            conn.execute(
                """INSERT INTO race_results (card_id, entry_id, horse_id, post_position, finish_position, official_finish,
                   is_scratched, official_odds_decimal, ingested_at) VALUES (?,?,?,?,?,?,?,?,'2026-03-30')""",
                (card_id, eid, hid, pp, finish, finish, int(scr), dec))
    conn.commit()
    return card_id


def test_basic_race_is_graded_with_each_forecast_computed_independently():
    conn = new_db()
    add_race(conn, day=2, probs=[0.5, 0.3, 0.2], ml=[1.0, 2.0, 3.0], dec_odds=[2.2, 3.6, 5.0], winner=1)
    graded, excluded = load_graded_races(conn)
    assert len(graded) == 1 and not excluded
    g = graded[0]
    assert g.as_of == "PROVEN" and g.race.winner_idx == 1 and g.starters == ["1", "2", "3"] and g.family == "dirt_sprint"
    fc = g.race.forecasts
    assert fc["model_board"] == pytest.approx([0.5, 0.3, 0.2])
    assert fc["uniform"] == pytest.approx([1 / 3] * 3)
    assert fc["morning_line"] == pytest.approx(devig([1 / 2.0, 1 / 3.0, 1 / 4.0]))            # to-one 1,2,3 -> decimal 2,3,4
    assert fc["closing_tote"] == pytest.approx(devig([1 / 2.2, 1 / 3.6, 1 / 5.0]))
    assert "model_pre_market" not in fc and "live_market" not in fc
    res = evaluate(graded, excluded)
    assert res["headline"]["model_board"]["log_loss"] == pytest.approx(-math.log(0.3))
    assert res["headline"]["uniform"]["log_loss"] == pytest.approx(math.log(3))
    assert res["headline"]["morning_line"]["log_loss"] == pytest.approx(-math.log(fc["morning_line"][1]))


def test_probabilities_are_renormalised_over_the_horses_that_started():
    conn = new_db()
    add_race(conn, day=2, probs=[0.4, 0.3, 0.2, 0.1], ml=[1.0, 2.0, 3.0, 9.0], winner=0, scratched=(3,))
    fc = load_graded_races(conn)[0][0].race.forecasts
    assert fc["model_board"] == pytest.approx([0.4 / 0.9, 0.3 / 0.9, 0.2 / 0.9]) and sum(fc["model_board"]) == pytest.approx(1)
    assert len(fc["morning_line"]) == len(fc["uniform"]) == 3


def test_a_score_run_made_after_post_is_never_used():
    conn = new_db()
    # pre-post run says horse 0 at 0.2; a later run (after the 19:00 post) says 0.9
    cid = add_race(conn, day=2, probs=[0.2, 0.4, 0.4], ml=[1, 2, 3], winner=0, extra_runs=("2026-03-02T21:00:00.000Z",))
    late = conn.execute("SELECT run_id FROM score_runs WHERE card_id=? ORDER BY run_timestamp DESC", (cid,)).fetchone()[0]
    conn.execute("UPDATE entry_scores SET win_probability=0.9 WHERE run_id=? AND post_position=1", (late,))
    conn.execute("UPDATE entry_scores SET win_probability=0.05 WHERE run_id=? AND post_position IN (2,3)", (late,))
    g = load_graded_races(conn)[0][0]
    assert g.race.forecasts["model_board"] == pytest.approx([0.2, 0.4, 0.4]) and g.run_timestamp.startswith("2026-03-02T15")


def test_the_last_run_before_post_wins_when_there_are_several():
    conn = new_db()
    cid = add_race(conn, day=2, probs=[0.2, 0.4, 0.4], ml=[1, 2, 3], extra_runs=("2026-03-02T18:30:00.000Z",))
    newest = conn.execute("SELECT run_id FROM score_runs WHERE card_id=? ORDER BY run_timestamp DESC", (cid,)).fetchone()[0]
    conn.execute("UPDATE entry_scores SET win_probability=0.7 WHERE run_id=? AND post_position=1", (newest,))
    conn.execute("UPDATE entry_scores SET win_probability=0.15 WHERE run_id=? AND post_position IN (2,3)", (newest,))
    g = load_graded_races(conn)[0][0]
    assert g.race.forecasts["model_board"][0] == pytest.approx(0.7)


def test_every_exclusion_is_counted_with_its_reason():
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .5], ml=[1, 1], run_ts="2026-03-02T20:00:00.000Z")                  # run after post
    add_race(conn, day=3, probs=[.5, .5], ml=[1, 1], post=None)                                         # no post time
    add_race(conn, day=4, probs=[.5, .5, 0], ml=[1, 1, 1], finish_override={1: 1})                       # dead heat
    add_race(conn, day=5, probs=[.5, .5], ml=[1, 1], finish_override={0: 2, 1: 3})                       # no winner
    add_race(conn, day=6, probs=[.5, .5], ml=[1, 1], scratched=(1,))                                    # one starter
    add_race(conn, day=7, probs=[.5, .5], ml=[1, 1], with_run=False)                                    # never scored
    add_race(conn, day=8, probs=[.5, .5], ml=[1, 1], results=False)                                     # no result at all
    add_race(conn, day=9, probs=[.6, .4], ml=[1, 1])                                                    # fine
    graded, excluded = load_graded_races(conn)
    assert [g.race.when.day for g in graded] == [9]
    assert dict(excluded) == {"ALL_SCORE_RUNS_AFTER_POST": 1, "AS_OF_UNPROVEN_NO_POST_TIME": 1, "DEAD_HEAT": 1,
                              "NO_OFFICIAL_WINNER": 1, "FIELD_TOO_SMALL_OR_NO_RESULT": 1, "NO_SCORE_RUN": 1}


def test_a_database_with_no_results_table_is_reported_not_a_crash():
    conn = sqlite3.connect(":memory:")
    graded, excluded = load_graded_races(conn)
    assert graded == [] and dict(excluded) == {"NO_RESULTS_TABLE": 1}


def test_races_without_a_post_time_can_be_included_but_are_flagged_unproven():
    conn = new_db()
    add_race(conn, day=3, probs=[.5, .5], ml=[1, 1], post=None)
    graded, excluded = load_graded_races(conn, include_unproven=True)
    assert [g.as_of for g in graded] == ["UNPROVEN"] and not excluded
    md = render_markdown(evaluate(graded, excluded))
    assert "no scheduled post time" in md


def test_a_race_still_grades_when_the_model_did_not_score_every_starter():
    conn = new_db()
    cid = add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3])
    conn.execute("DELETE FROM entry_scores WHERE post_position=3")
    fc = load_graded_races(conn)[0][0].race.forecasts
    assert "model_board" not in fc and "morning_line" in fc and "uniform" in fc


def test_live_market_is_used_only_when_complete_and_before_post():
    conn = new_db()
    cid = add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    ids = [r[0] for r in conn.execute("SELECT entry_id FROM entries WHERE card_id=? ORDER BY post_position", (cid,))]

    def snap(when, nums):
        for eid, n in zip(ids, nums):
            conn.execute(
                """INSERT INTO odds_snapshots (entry_id, snapshot_time, odds_numerator, odds_denominator, source,
                   source_provider, source_artifact_sha256) VALUES (?,?,?,1,'book','twinspires',?)""",
                (eid, when, n, f"sha-{when}"))

    snap("2026-03-02T22:00:00+00:00", [1, 1, 1])                            # after post: not eligible
    assert "live_market" not in load_graded_races(conn)[0][0].race.forecasts
    snap("2026-03-02T18:30:00+00:00", [1.0, 3.0, 4.0])                      # before post, complete
    fc = load_graded_races(conn)[0][0].race.forecasts
    assert fc["live_market"] == pytest.approx(devig([1 / 2, 1 / 4, 1 / 5]))


def test_pre_market_model_probability_is_kept_separate_from_the_displayed_one():
    conn = new_db()
    add_race(conn, day=2, probs=[.6, .3, .1], ml=[1, 2, 3], pre=[.3, .4, .3])
    fc = load_graded_races(conn)[0][0].race.forecasts
    assert fc["model_board"] == pytest.approx([.6, .3, .1]) and fc["model_pre_market"] == pytest.approx([.3, .4, .3])


def _season(conn, n=40, seed=11):
    """n races on consecutive days; the model is a noisy but informative version of the truth, the morning line is flatter."""
    rng = random.Random(seed)
    for k in range(n):
        size = rng.choice([5, 6, 8, 10])
        truth = devig([rng.uniform(0.3, 3.0) for _ in range(size)])
        winner = rng.choices(range(size), weights=truth)[0]
        model = devig([max(0.02, t * rng.uniform(0.8, 1.25)) for t in truth])
        ml_odds = [max(0.5, round(1 / t - 1 + rng.uniform(-1, 1), 1)) for t in truth]
        dec = [1 / t * 0.85 for t in truth]
        add_race(conn, day=1 + k % 28, number=1 + k // 28, probs=model, ml=ml_odds, dec_odds=[max(1.05, d) for d in dec],
                 winner=winner, surface="turf" if k % 3 == 0 else "dirt", furlongs=6.0 if k % 2 else 9.0,
                 track="CD" if k < 28 else "SAR")
    return conn


def test_a_full_evaluation_reports_paired_deltas_segments_calibration_and_the_running_curve():
    conn = _season(new_db())
    graded, excluded = load_graded_races(conn)
    assert len(graded) == 40 and not excluded
    res = evaluate(graded, excluded, n_boot=500)
    assert set(res["headline"]) == {"model_board", "morning_line", "closing_tote", "uniform"}
    assert res["headline"]["uniform"]["log_loss"] > res["headline"]["model_board"]["log_loss"]
    paired = {p["candidate"]: p for p in res["paired"]}
    assert paired["uniform"]["n_races"] == 40 and not paired["model_board"]["verdict"].startswith("INSUFFICIENT")
    assert res["hindsight_forecasts"] == ["closing_tote"]
    assert set(res["by_family"]) == {"dirt_sprint", "dirt_route", "turf_sprint", "turf_route"}
    assert all(v["n_paired"] <= v["n_races"] for v in res["by_family"].values())
    assert sum(v["n_races"] for v in res["by_field_size"].values()) == 40
    cal = res["calibration"]["model_board"]
    assert cal["n_starters"] == res["population"]["n_starters"] and cal["n_winners"] == 40
    assert len(res["running_log_loss"]) == 40 and res["running_log_loss"][-1]["n"] == 40
    assert res["running_log_loss"][-1]["model_board"] == pytest.approx(res["headline"]["model_board"]["log_loss"])
    md = render_markdown(res)
    assert "closing_tote (hindsight)" in md and "Paired against `morning_line`" in md and "Calibration: model_board" in md
    json.dumps(res, default=str)


def test_too_few_races_is_stated_plainly():
    conn = new_db()
    add_race(conn, day=2, probs=[.6, .4], ml=[1, 1])
    md = render_markdown(evaluate(*load_graded_races(conn)))
    assert "INSUFFICIENT DATA" in md and "descriptive only" in md


def test_since_until_and_track_filters():
    conn = _season(new_db(), n=40)
    assert len(load_graded_races(conn, track="SAR")[0]) == 12
    assert len(load_graded_races(conn, since="2026-03-10", until="2026-03-15")[0]) == len(
        [g for g in load_graded_races(conn)[0] if date(2026, 3, 10) <= g.race.when <= date(2026, 3, 15)])


def test_per_race_rows_cover_every_forecast_and_mark_hindsight():
    graded, _ = load_graded_races(_season(new_db(), n=3))
    rows = per_race_rows(graded)
    assert len(rows) == sum(len(g.race.forecasts) for g in graded)
    assert {r["forecast"] for r in rows if r["hindsight"]} == {"closing_tote"}


# ---- retrainable models ---------------------------------------------------------------------------
def test_oof_folds_train_only_on_earlier_races_and_hide_the_test_winners():
    conn = _season(new_db(), n=60, seed=2)
    graded, _ = load_graded_races(conn)
    seen = []

    def fit_predict(train, test):
        assert all(isinstance(t, RaceInput) and not hasattr(t, "winner_idx") for t in test)
        seen.append((max(g.race.when for g in train), min(t.when for t in test), len(train), len(test)))
        wins_top = sum(1 for g in train if g.race.forecasts["morning_line"].index(max(g.race.forecasts["morning_line"])) == g.race.winner_idx)
        shrink = 0.5 + wins_top / (2 * len(train))                    # something learned from the training results only
        return {t.key: devig([shrink * a + (1 - shrink) * b for a, b in zip(t.forecasts["morning_line"], t.forecasts["uniform"])])
                for t in test}

    races, report = walk_forward_oof(graded, fit_predict, n_folds=3, min_train=20, embargo_days=1)
    assert report["folds"] and all(tr < te - timedelta(days=1) for tr, te, _n, _m in seen)
    have = [g for g in races if "oof_model" in g.race.forecasts]
    assert len(have) == report["n_predicted"] > 0 and len(have) < len(races)
    res = evaluate(races, reference="morning_line", candidate="oof_model", forecasts=FORECASTS + ("oof_model",), n_boot=200)
    assert "oof_model" in res["headline"] and res["headline"]["oof_model"]["n_races"] == len(have)


def test_a_model_that_returns_bad_probabilities_is_stopped():
    graded, _ = load_graded_races(_season(new_db(), n=40, seed=3))
    with pytest.raises(ValueError, match="invalid probabilities"):
        walk_forward_oof(graded, lambda tr, te: {t.key: [0.9] * len(t.starters) for t in te}, n_folds=2, min_train=10)


# ---- command line ---------------------------------------------------------------------------------
def test_cli_writes_the_report_files_and_enforces_min_races(tmp_path, capsys):
    db = tmp_path / "t.db"
    conn = _season(new_db(str(db)), n=12)
    conn.close()
    out = tmp_path / "out"
    assert cli_main(["--db", str(db), "--out-dir", str(out), "--min-races", "10", "--boot", "100"]) == 0
    run = next(out.iterdir())
    assert {p.name for p in run.iterdir()} == {"report.md", "summary.json", "per_race.csv"}
    summary = json.loads((run / "summary.json").read_text())
    assert summary["population"]["n_graded_races"] == 12 and "# Walk-forward evaluation" in capsys.readouterr().out
    assert cli_main(["--db", str(db), "--out-dir", str(out), "--min-races", "99", "--boot", "100"]) == 1
    assert cli_main(["--db", str(tmp_path / "missing.db"), "--out-dir", str(out)]) == 1


def test_cli_opens_the_database_read_only(tmp_path):
    db = tmp_path / "ro.db"
    conn = _season(new_db(str(db)), n=3)
    conn.close()
    before = db.read_bytes()
    cli_main(["--db", str(db), "--out-dir", str(tmp_path / "o"), "--boot", "50"])
    assert db.read_bytes() == before
