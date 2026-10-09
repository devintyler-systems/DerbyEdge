# Session state (update at the end of every outcome; keep under 40 lines)

_Last updated: 2026-10-09 (evening), branch `claude/relaxed-noether-0kqijv`, PR devintyler-systems/DerbyEdge#32 (draft, CI green at 8f0bc2c)._

## Done and on the branch (not yet merged to main)
Morning-line fix + repair, Belmont at the Big A -> BAQ, Equibase chart parser, chart store/join/populate, walk-forward
evaluation, paper trading (place / settle / report), daily cycle, `training.status`, engine-version stamp, play-day
checklist, `start_derbyedge.bat`, this file and `CLAUDE.md`.

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
- The TwinSpires "RACE STATS" block (pars, track bias, post bias) is not captured. Capture-now-use-later: it cannot be reconstructed
  as-of later. Track-bias samples are tiny (6 / 3 races); do not weight until pooled.

## Not verified
Streamlit upload and scoring buttons, and `start_derbyedge.bat`, have not been run end to end on a real race.

## Open
- First real race through `docs/play_day_checklist.md`, then merge PR #32.
- Optional: backfill post times for the 57 legacy races (dry run first). Judgement given: keep them as a labelled baseline, do
  not count them toward the current engine's verdict.
- Not built, by decision: real-bet log (revisit at 30 graded races), multi-user product (needs a licensed data feed first).
- Not handled: dead-heat payoffs, cancelled / moved-off-turf races.

## Next move
Fix `training.status` (my defect): count only races whose graded/scored run has real win probabilities, and report how many collapsed to the
morning line. Then parse and store the RACE STATS block raw (not used in scoring yet).
