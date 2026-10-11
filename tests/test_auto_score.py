"""Automatic build + score after a bundle import: once per upload, never raises, never undoes the import."""
from __future__ import annotations

import subprocess

from src.app.auto_score import FAILED, SCORED, SKIPPED, AutoScoreResult, already_ran, auto_build_and_score, upload_key


class Steps:
    """A fake pipeline that records what ran, in order."""
    def __init__(self, results=None, boom=None):
        self.calls, self.results, self.boom = [], results or {}, boom or {}

    def __call__(self, script, card_id):
        self.calls.append((script, card_id))
        if script in self.boom:
            raise self.boom[script]
        return self.results.get(script, (True, f"{script} ok"))


def test_a_clean_import_builds_then_scores_then_reports_the_new_run():
    steps = Steps()
    res = auto_build_and_score(78, run_step=steps, newest_run_id=lambda cid: f"run-{cid}")
    assert steps.calls == [("build_features.py", 78), ("score.py", 78)]
    assert res.status == SCORED and res.run_id == "run-78" and "scored card 78 (run run-78)" in res.message
    assert "build_features.py ok" in res.output and "score.py ok" in res.output and "not be graded" not in res.message


def test_a_late_capture_is_still_scored_but_the_message_says_it_will_not_be_graded():
    res = auto_build_and_score(1, run_step=Steps(), newest_run_id=lambda c: "r", late_capture=True)
    assert res.status == SCORED and "after post" in res.message and "will not be graded" in res.message


def test_a_card_that_is_not_score_eligible_is_skipped_and_nothing_runs():
    steps = Steps()
    res = auto_build_and_score(1, run_step=steps, newest_run_id=lambda c: "r",
                               blockers=("MISSING_WEIGHT: entry 3 has no weight", "RUNNER_COUNT_MISMATCH: 9 vs 10"))
    assert steps.calls == [] and res.status == SKIPPED
    assert "MISSING_WEIGHT" in res.message and "RUNNER_COUNT_MISMATCH" in res.output


def test_a_failed_build_stops_before_scoring_and_keeps_the_import():
    steps = Steps({"build_features.py": (False, "boom in the builder")})
    res = auto_build_and_score(1, run_step=steps, newest_run_id=lambda c: "r")
    assert steps.calls == [("build_features.py", 1)]
    assert res.status == FAILED and "import is kept" in res.message and res.output == "boom in the builder"


def test_a_failed_score_reports_it_and_keeps_the_build_output():
    steps = Steps({"score.py": (False, "scorer exploded")})
    res = auto_build_and_score(1, run_step=steps, newest_run_id=lambda c: "r")
    assert res.status == FAILED and "Scoring failed" in res.message
    assert "build_features.py ok" in res.output and "scorer exploded" in res.output


def test_a_timeout_or_crash_is_returned_not_raised():
    slow = Steps(boom={"build_features.py": subprocess.TimeoutExpired("build_features.py", 180)})
    res = auto_build_and_score(1, run_step=slow, newest_run_id=lambda c: "r")
    assert res.status == FAILED and "TimeoutExpired" in res.message and slow.calls == [("build_features.py", 1)]
    crash = Steps(boom={"score.py": RuntimeError("no interpreter")})
    res = auto_build_and_score(1, run_step=crash, newest_run_id=lambda c: "r")
    assert res.status == FAILED and "Features were built" in res.message and "RuntimeError" in res.message


def test_scoring_that_creates_no_run_is_a_failure_not_a_success():
    res = auto_build_and_score(1, run_step=Steps(), newest_run_id=lambda c: None)
    assert res.status == FAILED and "no score run was created" in res.message


def test_it_runs_once_per_file_and_card_even_when_the_handler_reruns():
    done: dict[str, AutoScoreResult] = {}
    runs = Steps()
    key = upload_key("a" * 64, 78)
    for _rerun in range(5):                                 # Streamlit re-runs the import handler on every interaction
        if not already_ran(done, key):
            done[key] = auto_build_and_score(78, run_step=runs, newest_run_id=lambda c: "r")
    assert runs.calls == [("build_features.py", 78), ("score.py", 78)]
    assert upload_key("a" * 64, 78) != upload_key("b" * 64, 78) != upload_key("b" * 64, 79)   # a corrected file is a new upload
    assert not already_ran(done, upload_key("b" * 64, 78))
