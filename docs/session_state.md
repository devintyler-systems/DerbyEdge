# Session state (update at the end of every outcome; keep under 40 lines)

_Last updated: 2026-10-09, branch `claude/relaxed-noether-0kqijv`, PR devintyler-systems/DerbyEdge#32 (draft, CI green)._

## Done and on the branch (not yet merged to main)
Morning-line fix + repair, Belmont at the Big A -> BAQ, Equibase chart parser, chart store/join/populate, walk-forward
evaluation, paper trading (place / settle / report), daily cycle, `training.status`, engine-version stamp, play-day
checklist, `start_derbyedge.bat`, this file and `CLAUDE.md`.

## User's machine
`C:\Projects\derbyedge-engine`, on the PR branch, 28 paper-trading tests passed locally. Morning-line repair applied to
cards 73 and 74. `status` showed 1 graded race: 57 older races lack a scheduled post time (`AS_OF_UNPROVEN`).
Existing runs are `legacy`; they are not pooled with new ones.

## Not verified
Streamlit upload and scoring buttons, and `start_derbyedge.bat`, have not been run end to end on a real race.

## Open
- First real race through `docs/play_day_checklist.md`, then merge PR #32.
- Optional: backfill post times for the 57 legacy races (dry run first). Judgement given: keep them as a labelled baseline, do
  not count them toward the current engine's verdict.
- Not built, by decision: real-bet log (revisit at 30 graded races), multi-user product (needs a licensed data feed first).
- Not handled: dead-heat payoffs, cancelled / moved-off-turf races.

## Next move
User runs one race end to end and reports anything that does not match the checklist.
