"""Build features and score right after a validated race-bundle import, so a forgotten click cannot cost a race its evidence.

A score run's timestamp is what proves a forecast was made before post; a race scored after post is excluded from grading.
So a successful bundle import runs the same two steps as the Race Board's "Build + Score now" button (``build_features.py`` then
``score.py``) without waiting for a click.  Rules:

* It runs ONCE per uploaded file and card: the Streamlit import handler re-runs on every interaction while the file is still in the
  uploader, and each run would otherwise add a new score run.
* It never undoes or blocks the import.  If the card is not score-eligible, or a step fails or times out, the result says so and
  the Race Board buttons stay available.
* A late capture (received at/after post) is still scored so the board can be read, but the message says it will not be graded.
"""
from __future__ import annotations

import dataclasses
from typing import Callable, MutableMapping, Sequence

SCORED = "SCORED"
SKIPPED = "SKIPPED"
FAILED = "FAILED"


@dataclasses.dataclass(frozen=True)
class AutoScoreResult:
    status: str                    # SCORED | SKIPPED | FAILED
    message: str
    output: str = ""               # the pipeline output, for the Race Board's "Pipeline output" expander
    run_id: str | None = None


def upload_key(upload_sha256: str, card_id: int) -> str:
    """One automatic run per exact file and card; a corrected file (new SHA) is a new upload."""
    return f"{upload_sha256}:{int(card_id)}"


def already_ran(done: MutableMapping[str, AutoScoreResult], key: str) -> bool:
    return key in done


def auto_build_and_score(
    card_id: int,
    *,
    run_step: Callable[[str, int], tuple[bool, str]],
    newest_run_id: Callable[[int], str | None],
    blockers: Sequence[str] = (),
    late_capture: bool = False,
) -> AutoScoreResult:
    """Run build then score for ``card_id``.  Never raises: every failure is returned as a result."""
    if blockers:
        return AutoScoreResult(SKIPPED, f"Not auto-scored, the card is not score-eligible: {blockers[0]}", "\n".join(blockers))
    try:
        built, build_out = run_step("build_features.py", card_id)
    except Exception as exc:                                  # timeout, missing interpreter, ...
        return AutoScoreResult(FAILED, f"Feature build did not run ({type(exc).__name__}: {exc}). The import is kept; "
                                       "use Build + Score now on the Race Board.", str(exc))
    if not built:
        return AutoScoreResult(FAILED, "Feature build failed. The import is kept; see Pipeline output on the Race Board, "
                                       "then use Build + Score now.", build_out)
    try:
        scored, score_out = run_step("score.py", card_id)
    except Exception as exc:
        return AutoScoreResult(FAILED, f"Scoring did not run ({type(exc).__name__}: {exc}). Features were built; "
                                       "use Score this race on the Race Board.", build_out)
    output = f"{build_out}\n{score_out}"
    if not scored:
        return AutoScoreResult(FAILED, "Scoring failed. Features were built; see Pipeline output on the Race Board.", output)
    run_id = newest_run_id(card_id)
    if not run_id:
        return AutoScoreResult(FAILED, "Scoring finished but no score run was created for this card.", output)
    note = " Late capture: this run is after post, so the race will not be graded." if late_capture else ""
    return AutoScoreResult(SCORED, f"Built features and scored card {card_id} (run {run_id}).{note}", output, run_id)
