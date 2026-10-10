# Session state (update at the end of every outcome; keep under 40 lines)
Working rules, including the mandatory UPDATE YOUR LOCAL FILES block: `docs/working_rules.md`.
_Last updated: 2026-10-10, branch `main`: PR devintyler-systems/DerbyEdge#32 MERGED (CI green, 1254 tests passed). Local machine must be on `main`._
## Done and merged to main (PR #32)
Morning-line fix + repair, Belmont at the Big A -> BAQ, Equibase chart parser, chart store/join/populate, walk-forward evaluation,
paper trading, daily cycle, `training.status`, engine-version stamp, play-day checklist, `start_derbyedge.bat`, this file.
`CLAUDE.md` now tracked (`!CLAUDE.md` in `.gitignore`); the user's old local copy was stale and is replaced by the tracked one.
## User's machine
`C:\Projects\derbyedge-engine`, on the PR branch. Morning-line repair applied to cards 73 and 74. `status` showed 1 graded race:
57 older races lack a post time (`AS_OF_UNPROVEN`). Existing runs are `legacy`, never pooled with new ones.

## First real race (BEL R5 10-9-26) - what it showed
- Launcher works (the desktop **DerbyEdge** shortcut runs `start_derbyedge.bat`). Bundle import PASSED after the
  TwinSpires "expert Nth pick" tag fix (`twinspires_markdown.py`; fixture `tests/fixtures/BEL_Full_Race_Data_R5_10-9-26.md`).
- **The engine produced no independent forecast.** Win % is NULL/nan, sum win prob 0, model `seed_only_baseline` (0 labeled starters),
  run mode `MARKET_ANCHORED_NOT_ACTIONABLE`: the collapse-to-morning-line guard (`scorer.py` ~1394, stores NULL win_probability) fired.
  About 25% of the feature weight sits on speed-figure features with no data. Consequence: nothing from such runs can be graded
  (`model_board` absent) or paper-traded (`MODEL_PROBABILITIES_INCOMPLETE`).
- TwinSpires per-runner speed / class / power ARE parsed and stored as `twinspires_*` evidence (`twinspires_intake.py` ~177) but are
  deliberately NOT mapped to model features (not labelled Beyer). Candidate inputs once there is outcome data to learn weights.
- **RACE STATS block now captured, not scored:** optional bundle section (line `RACE STATS`), strict parser
  `src/ingest/twinspires_race_stats.py`, stored raw + parsed in `twinspires_race_stats` (card_id, captured_at). A malformed block, or one whose
  track / distance / surface contradicts the DK card, blocks the import with a named reason. Seen in ONE sample (BEL R5); bias samples tiny (6 / 3 races): no weight until pooled.
  See `docs/race_bundle_format.md`.
- **`training.status` fixed:** counts a race as scored/graded only if the grading run (last pre-post) has real win probabilities
  for every active runner; also prints races that collapsed to the morning line and the stored `model_collapse_status`
  ("(none stored)" = guard fired, no status written). Graded-eligible races lacking `model_board` show as `NO_MODEL_PROBABILITIES`.

## Open
- Not verified: Streamlit upload / scoring buttons and `start_derbyedge.bat` have not run end to end on a real race.
- RACE STATS capture CONFIRMED on the user's machine (card 75, post-race TEST row: not evidence). Next: a live race uploaded before post for a valid row.
- Pasting caution: DK and TwinSpires tabs auto-advance to the next race; the first R5 attempt had R6's tabs.
- Repo tidy done on this branch (stale root files and `artifacts/` removed, `01_downloader.py` -> `scripts/`, `.gitignore` `C*` narrowed, `samples/` templates tracked). Local-only archive of old captures: `data/raw_input/captures/`.
- Optional: backfill post times for the 57 legacy races (dry run first); keep as a labelled baseline.
- Not built, by decision: real-bet log (30 graded races), multi-user product. Not handled: dead heats, cancelled / moved-off-turf races.
- Issue #30 (Race Board: explain LOW-confidence BET suppression): still valid, deliberately parked. Only matters once the engine emits model probabilities AND live odds exist; revisit then.
## Next move
The engine still gives no independent forecast on a real race (speed-figure features empty). Get outcome data into the model
(backfilled charts + `twinspires_*` evidence) before more capture work; until then `status` correctly shows 0 scored races for new runs.
