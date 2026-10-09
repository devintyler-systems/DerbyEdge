"""Engine version stamp: stable fingerprint, cohorts never pooled in grading, status and the CLI."""
from __future__ import annotations

import json
from datetime import date

import pytest

from src.services.evidence_status import build_status, render_text
from src.services.paper_trading import ensure_paper_tables
from src.services.walk_forward_eval import load_graded_races, per_race_rows
from src.utils import engine_version as ev
from tests.test_walk_forward_eval import add_race, new_db
from training.walk_forward import main as cli_main


# ---- fingerprint ---------------------------------------------------------------------------------
def _tree(tmp_path, body=b"x = 1\n"):
    for d in ("src/models", "src/features", "src/ingest"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/models/scorer.py").write_bytes(body)
    (tmp_path / "src/features/builder.py").write_bytes(b"y = 2\n")
    (tmp_path / "src/ingest/other.py").write_bytes(b"z = 3\n")
    return str(tmp_path)


def test_fingerprint_changes_with_engine_code_only_and_ignores_line_endings(tmp_path):
    root = _tree(tmp_path)
    base = ev.code_fingerprint.__wrapped__(root)
    (tmp_path / "src/ingest/other.py").write_bytes(b"z = 99\n")                         # not engine code
    (tmp_path / "README.md").write_text("docs")
    assert ev.code_fingerprint.__wrapped__(root) == base
    (tmp_path / "src/models/scorer.py").write_bytes(b"x = 1\r\n")                       # same code, CRLF checkout
    assert ev.code_fingerprint.__wrapped__(root) == base
    (tmp_path / "src/models/scorer.py").write_bytes(b"x = 2\n")                         # real change
    assert ev.code_fingerprint.__wrapped__(root) != base


def test_engine_version_never_raises_and_names_the_model():
    class A:
        model_name, version = "dirt_sprint", "0.3"
    assert ev.engine_version(A()).endswith("/dirt_sprint@0.3") and ev.engine_version(A()).startswith("code-")
    assert ev.engine_version(object()).endswith("/unknown-model")
    assert ev.engine_version(None).endswith("/no-model")
    assert ev.label(None) == "legacy" and ev.label("v") == "v"


# ---- grading by cohort ---------------------------------------------------------------------------
def _stamp(conn, card_id, version):
    conn.execute("UPDATE score_runs SET engine_version=? WHERE card_id=?", (version, card_id))


def _cohorts(conn):
    legacy = add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    old = add_race(conn, day=3, probs=[.5, .3, .2], ml=[1, 2, 3], winner=1, run_ts="2026-03-03T15:00:00.000Z")
    new = add_race(conn, day=4, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0, run_ts="2026-03-04T15:00:00.000Z")
    new2 = add_race(conn, day=5, probs=[.5, .3, .2], ml=[1, 2, 3], winner=2, run_ts="2026-03-05T15:00:00.000Z")
    _stamp(conn, old, "code-aaa/m@1")
    _stamp(conn, new, "code-bbb/m@1")
    _stamp(conn, new2, "code-bbb/m@1")
    return legacy


def test_null_runs_are_legacy_and_the_loader_filters_by_version():
    conn = new_db()
    _cohorts(conn)
    graded, excluded = load_graded_races(conn)
    assert sorted(g.engine_version for g in graded) == ["code-aaa/m@1", "code-bbb/m@1", "code-bbb/m@1", "legacy"]
    only, exc = load_graded_races(conn, engine_version="code-bbb/m@1")
    assert len(only) == 2 and exc["OTHER_ENGINE_VERSION"] == 2
    assert [g.engine_version for g in load_graded_races(conn, engine_version="legacy")[0]] == ["legacy"]
    assert {r["engine_version"] for r in per_race_rows(graded)} == {g.engine_version for g in graded}


def test_the_version_of_the_graded_run_decides_the_cohort_not_a_later_run():
    conn = new_db()
    cid = add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0, extra_runs=("2026-03-02T21:00:00.000Z",))   # after post
    conn.execute("UPDATE score_runs SET engine_version='code-new/m@1' WHERE card_id=? AND run_timestamp > '2026-03-02T19:00:00'", (cid,))
    g = load_graded_races(conn)[0][0]
    assert g.engine_version == "legacy" and g.run_timestamp.startswith("2026-03-02T15")


def test_a_database_without_the_column_grades_everything_as_legacy():
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    conn.execute("ALTER TABLE score_runs DROP COLUMN engine_version")
    assert [g.engine_version for g in load_graded_races(conn)[0]] == ["legacy"]


# ---- CLI -----------------------------------------------------------------------------------------
def _db_file(tmp_path):
    db = tmp_path / "v.db"
    conn = new_db(str(db))
    _cohorts(conn)
    conn.commit()
    conn.close()
    return db


def _run(db, tmp_path, *extra):
    out = tmp_path / "out"
    code = cli_main(["--db", str(db), "--out-dir", str(out), "--boot", "50", *extra])
    runs = sorted(out.iterdir()) if out.exists() else []
    return code, runs[-1] if runs else None


def test_cli_defaults_to_the_newest_stamped_version_and_never_pools(tmp_path, capsys):
    db = _db_file(tmp_path)
    code, run = _run(db, tmp_path)
    assert code == 0
    summary = json.loads((run / "summary.json").read_text())
    assert summary["engine_version"] == "code-bbb/m@1" and summary["population"]["n_graded_races"] == 2
    assert summary["population"]["excluded"]["OTHER_ENGINE_VERSION"] == 2
    text = (run / "report.md").read_text()
    assert "Engine version: `code-bbb/m@1`" in text and "Other versions are not pooled" in text


def test_cli_can_select_legacy_or_a_named_version_and_lists_versions(tmp_path, capsys):
    db = _db_file(tmp_path)
    _, run = _run(db, tmp_path, "--engine-version", "legacy")
    s = json.loads((run / "summary.json").read_text())
    assert s["engine_version"] == "legacy" and s["population"]["n_graded_races"] == 1
    assert "pre-stamp" in (run / "report.md").read_text() or "before versions were stamped" in (run / "report.md").read_text()
    capsys.readouterr()
    assert cli_main(["--db", str(db), "--list-versions"]) == 0
    listed = capsys.readouterr().out
    assert "legacy: 1" in listed and "code-aaa/m@1: 1" in listed and "code-bbb/m@1: 2" in listed


def test_cli_with_only_legacy_runs_shows_them_and_says_so(tmp_path):
    db = tmp_path / "l.db"
    conn = new_db(str(db))
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    conn.commit()
    conn.close()
    code, run = _run(db, tmp_path)
    text = (run / "report.md").read_text()
    assert code == 0 and "No score run carries an engine version yet" in text


# ---- status --------------------------------------------------------------------------------------
def test_status_counts_only_the_newest_version_and_lists_the_others():
    conn = new_db()
    _cohorts(conn)
    st = build_status(conn, today=date(2026, 4, 1))
    assert st["engine_version"] == "code-bbb/m@1"
    scored, graded, bets, clv = st["metrics"]
    assert scored["have"] == 2 and graded["have"] == 2
    assert st["graded_by_version"] == {"legacy": 1, "code-aaa/m@1": 1, "code-bbb/m@1": 2}
    text = render_text(st)
    assert "Engine version counted: code-bbb/m@1" in text and "legacy=1" in text and "code-aaa/m@1=1" in text


def test_status_with_no_stamped_runs_counts_legacy_and_says_so():
    conn = new_db()
    add_race(conn, day=2, probs=[.5, .3, .2], ml=[1, 2, 3], winner=0)
    st = build_status(conn, today=date(2026, 4, 1))
    assert st["engine_version"] == "legacy" and st["metrics"][1]["have"] == 1
    assert "no stamped score run yet" in render_text(st)


def test_paper_bets_are_counted_under_the_version_of_the_run_that_made_them():
    conn = new_db()
    _cohorts(conn)
    ensure_paper_tables(conn)
    conn.execute("INSERT INTO paper_policies VALUES ('p','n','{}','t')")
    new_run = next(r for r, v in conn.execute("SELECT run_id, engine_version FROM score_runs") if v == "code-bbb/m@1")
    old_run = next(r for r, v in conn.execute("SELECT run_id, engine_version FROM score_runs") if v == "code-aaa/m@1")
    for i, run in enumerate([new_run, new_run, old_run]):
        conn.execute(
            """INSERT INTO paper_bets (policy_id, card_id, race_key, entry_id, run_id, decision_time, capture_time, capture_provider,
               post_utc, model_p, market_p, captured_decimal, edge, ev, stake, status, clv)
               VALUES ('p',?,?,?,?, '2026-03-10T15:00:00+00:00','t','dk','t',.3,.2,4,.1,.2,2,'WON',0.1)""",
            (i, f"CD|2026-03-10|R{i}", i, run))
    st = build_status(conn, today=date(2026, 4, 1))
    assert st["metrics"][2]["have"] == 2 and st["metrics"][3]["have"] == 2
