"""Paper bets are labelled with the forecast class of the run that made them and reported per class, never pooled."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from src.services.evidence_status import build_status, render_text
from src.services.forecast_class import DIAGNOSTIC_SEED, TRAINED
from src.services.paper_trading import PaperPolicy, build_report, decide_bets, place_paper_bets, render_markdown, settle_paper_bets
from tests.test_paper_trading import POLICY, _chart_db, _chart_race, make_card


def _race_inputs(conn, number):
    """A model that loves the first starter and a market that does not: it qualifies for exactly one bet."""
    programs = [s.program for s in _chart_race(conn, number).starters]
    rest = 0.5 / (len(programs) - 1)
    return ({programs[0]: 0.5, **{p: rest for p in programs[1:]}}, {programs[0]: 4.0, **{p: 6.0 for p in programs[1:]}})


def _two_class_book():
    """Race 2 scored by a seed-only baseline, race 3 by a trained model; both bet, both settled."""
    conn = _chart_db()
    for number, model_type in ((2, "seed_only_baseline"), (3, "xgboost")):
        model, dec = _race_inputs(conn, number)
        card_id, _ = make_card(conn, number, model=model, decimals=dec)
        conn.execute("UPDATE score_runs SET model_type=? WHERE card_id=?", (model_type, card_id))
    conn.commit()
    return conn


def test_each_bet_carries_the_class_of_the_run_that_made_it():
    conn = _two_class_book()
    rep = decide_bets(conn, POLICY)
    assert sorted((b.race_key, b.forecast_class) for b in rep.bets) == [
        ("CD|2026-04-25|R2", DIAGNOSTIC_SEED), ("CD|2026-04-25|R3", TRAINED)]


def test_the_report_gives_each_class_its_own_verdict_and_shows_no_pooled_one():
    conn = _two_class_book()
    place_paper_bets(conn, POLICY)
    settle_paper_bets(conn)
    rep = build_report(conn, POLICY.policy_id)
    assert set(rep["by_forecast_class"]) == {DIAGNOSTIC_SEED, TRAINED}
    for c in rep["by_forecast_class"].values():
        assert c["summary"]["n_bets"] == 1 and c["summary"]["n_bets"] + c["summary"]["n_refunded_scratch"] <= 1
    assert {b["forecast_class"] for b in rep["bets"]} == {DIAGNOSTIC_SEED, TRAINED}
    assert rep["summary"]["n_bets"] == 2                                         # the pooled dict still exists for callers...
    md = render_markdown(rep)                                                    # ...but the report never presents it
    assert "no pooled verdict is shown" in md
    assert f"## Forecast class: {DIAGNOSTIC_SEED}" in md and f"## Forecast class: {TRAINED}" in md
    assert "uncalibrated" in md and "not a basis for a real bet" in md
    assert md.count("Verdict:") == 4                                             # return + CLV verdict, once per class


def test_baselines_are_computed_on_each_classs_own_races():
    conn = _two_class_book()
    place_paper_bets(conn, POLICY)
    settle_paper_bets(conn)
    rep = build_report(conn, POLICY.policy_id)
    n2, n3 = (len(_chart_race(conn, n).starters) for n in (2, 3))
    assert rep["by_forecast_class"][DIAGNOSTIC_SEED]["baselines"]["every_runner"]["n_bets"] == n2
    assert rep["by_forecast_class"][TRAINED]["baselines"]["every_runner"]["n_bets"] == n3
    assert rep["baselines"]["every_runner"]["n_bets"] == n2 + n3


def test_a_single_class_book_says_which_class_and_keeps_the_usual_report():
    conn = _chart_db()
    model, dec = _race_inputs(conn, 2)
    card_id, _ = make_card(conn, 2, model=model, decimals=dec)
    conn.execute("UPDATE score_runs SET model_type='seed_only_baseline' WHERE card_id=?", (card_id,))
    place_paper_bets(conn, POLICY)
    settle_paper_bets(conn)
    md = render_markdown(build_report(conn, POLICY.policy_id))
    assert f"Forecast class of every bet: **{DIAGNOSTIC_SEED}**" in md and "no pooled verdict" not in md
    assert "INSUFFICIENT DATA" in md and "Closing-line value" in md


def test_legacy_runs_without_a_model_type_are_labelled_other_not_trained():
    from src.services.forecast_class import OTHER
    conn = _chart_db()
    model, dec = _race_inputs(conn, 2)
    make_card(conn, 2, model=model, decimals=dec)                                 # schema default model_type = 'fallback'
    assert [b.forecast_class for b in decide_bets(conn, POLICY).bets] == [OTHER]


def test_status_splits_settled_paper_bets_by_class():
    conn = _two_class_book()
    place_paper_bets(conn, POLICY)
    settle_paper_bets(conn)
    st = build_status(conn, today=date(2026, 5, 1))
    assert st["forecast_class"]["paper_settled"] == {DIAGNOSTIC_SEED: 1, TRAINED: 1}
    note = st["metrics"][2]["note"]
    assert f"{DIAGNOSTIC_SEED}=1" in note and f"{TRAINED}=1" in note and "never pooled" in note
    assert f"{DIAGNOSTIC_SEED}=" in render_text(st)


def test_place_cli_marks_diagnostic_bets_and_says_they_are_a_separate_cohort(tmp_path, capsys):
    from training.paper_trading import main
    src = _two_class_book()
    db = tmp_path / "t.db"
    out = sqlite3.connect(db)
    src.backup(out)
    out.close()
    before = db.read_bytes()
    assert main(["--db", str(db), "place", "--backfill", "--dry-run"]) == 0
    assert db.read_bytes() == before                                              # a dry run writes nothing at all
    text = capsys.readouterr().out
    assert "(dry run, nothing written)" in text
    assert f"[{DIAGNOSTIC_SEED}] CD|2026-04-25|R2" in text and f"[{TRAINED}] CD|2026-04-25|R3" in text
    assert "NOTE: 1 bet(s) are on DIAGNOSTIC_SEED_BASELINE forecasts" in text and "never pooled" in text
