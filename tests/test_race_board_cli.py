"""scripts/race_board_cli.py: the headless Race Board for away-from-home runs (throwaway DB, the app's gate and picks)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
CLI = ROOT / "scripts" / "race_board_cli.py"
PRE_POST = "2026-10-12T20:00:00+00:00"          # post is 5:40 PM ET = 21:40 UTC
AFTER_POST = "2026-10-13T00:32:35+00:00"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("bundle") / "MNR_Full_Race_Data_R5_10-12-26.md"
    path.write_text((FIX / "MNR_Full_Race_Data_R5_10-12-26.md").read_text(encoding="utf-8") + "\n"
                    + (FIX / "TS_Tabs_MNR_R5_10-12-26.md").read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _run(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "DERBYEDGE_DB"}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=300)


@pytest.fixture(scope="module")
def pre_post_run(bundle):
    real = ROOT / "db" / "derbyedge.db"
    existed = real.exists()
    before = real.stat().st_mtime if existed else None
    result = _run(str(bundle), "--captured-at", PRE_POST)
    return result, existed, before


def test_a_pre_post_bundle_prints_the_gated_board_and_labelled_picks(pre_post_run):
    r, _, _ = pre_post_run
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "Mountaineer R5 2026-10-12" in out and "Import: PASS · 10 runners · 2 scratched" in out
    assert "Speed/Class/Pace tabs: class, pace, speed" in out and "pre-post" in out
    assert "DIAGNOSTIC FORECAST — NOT VALID FOR BETTING" in out and "Gate reasons:" in out
    assert "| Rank | Horse" in out and "Diagnostic Win %" in out and "ML-implied %" in out
    assert "PRE-THRESHOLD: NOT VALIDATED EVIDENCE" in out and "Break-even payout per $2" in out
    assert "NOT SAVED" in out and "throwaway database" in out


def test_nothing_actionable_leaks_past_the_eligibility_gate(pre_post_run):
    out = pre_post_run[0].stdout.lower()
    for forbidden in ("bet candidates", "underlay", "kelly", "stake:", "recommended wager:"):
        assert forbidden not in out, forbidden
    headers = [ln for ln in out.splitlines() if ln.startswith("| ")]
    assert headers and not any(w in h for h in headers for w in ("fair", "edge", "bet tag", "value"))   # actionable columns stay withheld
    assert "no stake is computed" in out                       # the only mention of a stake is the disclaimer


def test_it_never_touches_the_real_database(pre_post_run):
    _, existed, before = pre_post_run
    real = ROOT / "db" / "derbyedge.db"
    assert real.exists() == existed and (not existed or real.stat().st_mtime == before)


def test_it_refuses_the_real_database_path(bundle):
    r = _run(str(bundle), "--db", str(ROOT / "db" / "derbyedge.db"))
    assert r.returncode == 2 and "REFUSED" in r.stdout


def test_a_late_capture_is_flagged_and_still_printed(bundle):
    r = _run(str(bundle), "--captured-at", AFTER_POST)
    assert r.returncode == 0 and "LATE CAPTURE" in r.stdout and "never gradable" in r.stdout


def test_a_blocked_import_prints_the_named_reasons_and_does_not_score(tmp_path):
    bad = tmp_path / "MNR_Full_Race_Data_R5_10-12-26.md"
    bad.write_text((FIX / "MNR_Full_Race_Data_R5_10-12-26.md").read_text(encoding="utf-8").replace("Greyhound Station", "Greyhound Stn", 1)
                   + "\n" + (FIX / "TS_Tabs_MNR_R5_10-12-26.md").read_text(encoding="utf-8"), encoding="utf-8")
    r = _run(str(bad), "--captured-at", PRE_POST)
    assert r.returncode == 2 and "IMPORT BLOCKED" in r.stdout and "Greyhound" in r.stdout
    assert "Scoring:" not in r.stdout and "| Rank |" not in r.stdout


def test_derbyedge_db_redirects_the_pipeline_database(tmp_path):
    target = tmp_path / "other.db"
    r = _run("--help", env_extra={})   # smoke: CLI imports cleanly
    assert r.returncode == 0
    code = "from src.utils.db import DB_PATH; print(DB_PATH)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT),
                         env={**os.environ, "DERBYEDGE_DB": str(target)})
    assert out.stdout.strip() == str(target)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT),
                         env={k: v for k, v in os.environ.items() if k != "DERBYEDGE_DB"})
    assert out.stdout.strip() == str(ROOT / "db" / "derbyedge.db")
