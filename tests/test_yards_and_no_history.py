"""Yard distances (Quarter Horse races) and runners with no past performances (IND R9 10-19-26).

The card failed with "race identity missing required distance" and "materially incomplete workout row ... missing distance, time"
because only furlongs and miles were read.  A yards-only distance marks a Quarter Horse race, which the thoroughbred model must not score.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.ingest.race_bundle import parse_race_bundle
from src.services import bet_picks as bp
from src.services.draftkings_markdown_intake import markdown_card_score_readiness, persist_validated_draftkings_markdown
from src.services.no_history import no_history_entry_ids
from src.utils.distance_parser import parse_furlongs

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
IND = FIX / "IND_Full_Race_Data_R9_10-19-26.md"
LATE = datetime(2026, 10, 19, 21, 3, 18, tzinfo=timezone.utc)      # after the 3:15 PM ET post


def _bundle():
    return parse_race_bundle(IND.read_text(encoding="utf-8"), source_path=IND.name,
                             as_of=LATE.replace(second=0), captured_at=LATE)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("\n".join(
        l for l in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in l))
    return conn


def test_yards_alone_are_furlongs_and_a_mile_plus_yards_is_unchanged():
    assert parse_furlongs("400 Y") == pytest.approx(400 / 220) and parse_furlongs("220 Y") == 1.0
    assert parse_furlongs("1m 70 Y") == 8.0 and parse_furlongs("5 1/2 F") == 5.5 and parse_furlongs("1 M") == 8.0


def test_the_ind_card_now_validates_with_its_yard_distance_and_yard_workouts():
    b = _bundle()
    assert b.validation.passed and not b.validation.errors, b.validation.errors
    race = b.card.race
    assert race.distance == "400 Y" and race.normalized_distance_furlongs == pytest.approx(400 / 220)
    assert b.validation.workout_row_count == 28 and b.validation.past_performance_row_count == 43
    assert all(w.distance and w.time for w in b.card.workouts)


def test_starters_with_no_details_are_named_in_a_warning_not_an_error():
    w = " ".join(_bundle().validation.warnings)
    assert "NO_PP_HISTORY: 4 Zoomin Cartel6 (DK: no details available); 5 Klt Fursflyin (DK: no details available)" in w
    assert "Seizedbymarielaveau" not in w.split("NO_PP_HISTORY")[1]          # a scratch is not a starter


def test_a_quarter_horse_card_is_kept_but_blocked_from_scoring_with_the_reason():
    b = _bundle()
    assert any(x.startswith("SCOPE_QUARTER_HORSE") for x in b.validation.warnings)
    conn = _db()
    r = persist_validated_draftkings_markdown(conn, b.card, b.validation, source_filename=IND.name,
                                              scheduled_post_utc=b.post_utc, captured_at=b.captured_at)
    ready = markdown_card_score_readiness(conn, r.card_id)
    assert not ready.score_eligible
    assert [x.code for x in ready.blockers] == ["OUT_OF_SCOPE_QUARTER_HORSE"]
    assert "Quarter Horse" in ready.blockers[0].message


def test_a_thoroughbred_card_has_no_quarter_horse_warning():
    text = (FIX / "MNR_Full_Race_Data_R5_10-12-26.md").read_text(encoding="utf-8")
    b = parse_race_bundle(text, source_path="MNR_Full_Race_Data_R5_10-12-26.md",
                          as_of=datetime(2026, 10, 13, 0, 32, tzinfo=timezone.utc),
                          captured_at=datetime(2026, 10, 13, 0, 32, 35, tzinfo=timezone.utc))
    assert not any(x.startswith(("SCOPE_QUARTER_HORSE", "NO_PP_HISTORY")) for x in b.validation.warnings)


def test_no_history_ids_need_an_explicit_zero_count():
    frame = pd.DataFrame({"entry_id": [1, 2, 3], "dk_history_start_count": [0, 12, None]})
    assert no_history_entry_ids(frame) == {1}                                # a missing count is not evidence of no history
    assert no_history_entry_ids(pd.DataFrame()) == set() and no_history_entry_ids(None) == set()


def test_a_pick_on_a_runner_with_no_history_carries_the_warning_and_is_otherwise_unchanged():
    runners = [{"entry_id": i, "horse_name": f"H{i}", "post_position": i, "model_win": m, "market_win": k}
               for i, (m, k) in enumerate([(0.40, 0.10), (0.20, 0.30), (0.15, 0.20), (0.15, 0.25), (0.10, 0.15)], start=1)]
    plain = bp.recommend_picks(runners)
    flagged = bp.recommend_picks([{**r, "no_history": r["entry_id"] == 1} for r in runners])
    assert [p["entry_id"] for p in plain] == [p["entry_id"] for p in flagged] and plain          # selection is not changed
    frame = bp.picks_frame(flagged)
    assert "NO PP HISTORY (default-driven)" in frame.loc[frame["Horse"] == "H1", "Warning"].iloc[0]
    assert "NO PP HISTORY" not in " ".join(bp.picks_frame(plain)["Warning"])


def _run_cli(path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "DERBYEDGE_DB"}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "race_board_cli.py"), str(path), *args],
                          capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=300)


def test_the_board_cli_stops_at_the_quarter_horse_blocker_with_a_readable_reason():
    r = _run_cli(IND, "--captured-at", LATE.isoformat())
    assert r.returncode == 3, r.stdout + r.stderr
    assert "Import: PASS · 7 runners" in r.stdout and "NOT SCORED" in r.stdout and "Quarter Horse race" in r.stdout
    assert "ScoreReadinessBlocker(" not in r.stdout and "| Rank |" not in r.stdout


def test_the_board_cli_labels_a_runner_with_no_history(tmp_path):
    text = (FIX / "MNR_Full_Race_Data_R5_10-12-26.md").read_text(encoding="utf-8")
    start, end = text.index("4\n\nPP 4\n"), text.index("5\n\nPP 5\n")
    blank = ("4\n\nPP 4\n2\nM: 2\nPasta Salad Rhonda\nDark Bay or Brown, Mare, 6 yrs (KY) L\nHumberto Haldar\n0 - 0 - 0 - 0\n"
             "Jabdiel Castillo\n0 - 0 - 0 - 0\nFree Drop Billy\nAdios Caballero\nNo details available.\n")
    path = tmp_path / "MNR_Full_Race_Data_R5_10-12-26.md"
    path.write_text(text[:start] + blank + text[end:] + "\n" + (FIX / "TS_Tabs_MNR_R5_10-12-26.md").read_text(encoding="utf-8"),
                    encoding="utf-8")
    r = _run_cli(path, "--captured-at", "2026-10-12T20:00:00+00:00")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NO_PP_HISTORY: 4 Pasta Salad Rhonda (DK: no details available)" in r.stdout
    assert "NO PP HISTORY: Pasta Salad Rhonda." in r.stdout
    row = next(ln for ln in r.stdout.splitlines() if "| Pasta Salad Rhonda" in ln)
    assert row.rstrip().endswith("NONE |")
