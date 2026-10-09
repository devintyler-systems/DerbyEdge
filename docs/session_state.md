# Session state (update at the end of every outcome; keep under 40 lines)

_Last updated: 2026-10-09 (night), branch `claude/relaxed-noether-0kqijv`, PR devintyler-systems/DerbyEdge#32 (draft). Full suite 1247 passed locally; CI not yet seen on the new commits._

## Done and on the branch (not yet merged to main)
Morning-line fix + repair, Belmont at the Big A -> BAQ, Equibase chart parser, chart store/join/populate, walk-forward
evaluation, paper trading (place / settle / report), daily cycle, `training.status`, engine-version stamp, play-day
checklist, `start_derbyedge.bat`, this file. **`CLAUDE.md` is NOT in git** (`.gitignore` rule `C*` skips it): it exists only on the user's machine.
Also done: `training.status` fix (below) and RACE STATS capture (below).

## User's machine
`C:\Projects\derbyedge-engine`, on the PR branch, 28 paper-trading tests passed locally. Morning-line repair applied to
cards 73 and 74. `status` showed 1 graded race: 57 older races lack a scheduled post time (`AS_OF_UNPROVEN`).
Existing runs are `legacy`; they are not pooled with new ones.

## First real race (BEL R5 10-9-26) - what it showed
- Launcher works (the desktop shortcut must point at `start_derbyedge.bat`, not `START_DERBYEDGE.md`). Bundle import PASSED after the
  TwinSpires "expert Nth pick" tag fix (`twinspires_markdown.py`; fixture `tests/fixtures/BEL_Full_Race_Data_R5_10-9-26.md`).
- **The engine produced no independent forecast.** Win % is NULL/nan, sum win prob 0, model `seed_only_baseline` (0 labeled starters),
  run mode `MARKET_ANCHORED_NOT_ACTIONABLE`: the collapse-to-morning-line guard (`scorer.py` ~1394, stores NULL win_probability) fired.
  About 25% of the feature weight sits on speed-figure features with no data. Consequence: nothing from such runs can be graded
  (`model_board` absent) or paper-traded (`MODEL_PROBABILITIES_INCOMPLETE`).
- TwinSpires per-runner speed / class / power ARE parsed and stored as `twinspires_*` evidence (`twinspires_intake.py` ~177) but are
  deliberately NOT mapped to model features (not labelled Beyer). Candidate inputs once there is outcome data to learn weights.
- **RACE STATS block: now captured** (not scored). Optional bundle section starting at a line `RACE STATS`; strict parser
  `src/ingest/twinspires_race_stats.py`; stored raw + parsed in `twinspires_race_stats` (card_id, captured_at). Malformed block
  blocks the import with a named reason; delete the block to import without it. Format seen in ONE sample only (BEL R5). Track-bias
  samples are tiny (6 / 3 races): do not weight until pooled. Details: `docs/race_bundle_format.md`.
- **`training.status` fixed:** a race counts as scored/graded only if the run grading uses (last pre-post run) has real win
  probabilities for every active runner. It now also prints "scored races that collapsed to the morning line" and the stored
  `model_collapse_status` of the scored runs ("(none stored)" = guard fired but no status written). Graded races without
  `model_board` are listed as `NO_MODEL_PROBABILITIES`. Scored now follows the grading run, not "any pre-post run of the version".

## Not verified
Streamlit upload and scoring buttons, and `start_derbyedge.bat`, have not been run end to end on a real race.

## Open
- Re-run the first real race with the RACE STATS block pasted into the bundle (confirms it stores on a real import). Then merge PR #32.
- Commit `CLAUDE.md` (add `!CLAUDE.md` to `.gitignore`) so cloud sessions can read the working agreement.
- Optional: backfill post times for the 57 legacy races (dry run first). Judgement given: keep them as a labelled baseline, do
  not count them toward the current engine's verdict.
- Not built, by decision: real-bet log (revisit at 30 graded races), multi-user product (needs a licensed data feed first).
- Not handled: dead-heat payoffs, cancelled / moved-off-turf races.

## Next move
The engine still produces no independent forecast on a real race (speed-figure features empty). Decide how to get outcome data
into the model (backfilled charts + the `twinspires_*` evidence) before any more capture work: until then `status` will show 0
scored races for new runs, which is the true answer.
