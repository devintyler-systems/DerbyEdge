# Session state (update at the end of every outcome; keep under 40 lines)
Working rules, including the mandatory UPDATE YOUR LOCAL FILES block: `docs/working_rules.md`.
_Last updated: 2026-10-13, `main` = PRs #32-#48 merged (#48: jockey `0 - 0 - 0 - 0` read as trainer, MNR R5); TwinSpires tabs build is on its own branch/PR. Local machine must be on `main`._
## Done and merged to main
Morning-line fix + repair, BAQ, Equibase chart parser + store/join/populate, walk-forward, paper trading, daily cycle, `training.status`,
engine-version stamp, RACE STATS capture, import fixes (`L122bOn` Basic weight cell, `9 MTP ALLOWANCE` post display), repo tidy,
`CLAUDE.md` tracked, `docs/working_rules.md`. User's machine: `C:\Projects\derbyedge-engine` on `main`; launch via desktop **DerbyEdge**
shortcut (runs `start_derbyedge.bat`). 57 older races lack a post time (`AS_OF_UNPROVEN`); existing runs are `legacy`, never pooled.
## Why Win % is blank on real races (diagnosed 2026-10-10, CT R7 + BEL R5 reproduced in a sandbox)
- **The model DOES produce a forecast** (stored `entry_scores.win_probability` sums to 1, not collapsed to the morning line). The app's
  fail-closed gate (`score_delivery.contain_ineligible_board` -> `score_eligibility`) then blanks Win %, fair odds, edge, tags for every entry.
  (Earlier notes blamed the collapse guard `scorer.py` ~1394: wrong for these runs.) CONFIRMED on the user's own DB (CT R7 10-10 shows stored values).
- Gate reasons, all 9 entries: `calibration_unavailable_or_unaudited` (seed-only baseline; promotion needs >=4,000 labeled starters),
  `active_input_unavailable_or_defaulted...` (~54% of the weight is on empty features: speed_best_3/speed_last/beyer_last, work_readiness,
  trainer_intent, traffic_resilience, horses_beaten, finish_energy, derby_override_score), `unsupported_or_unknown_runtime_source`
  (TwinSpires-sourced pace is not in `SUPPORTED_SOURCES`), `source_evidence_missing_or_not_proven_as_of_decision_timestamp`.
- **Diagnostic display + forecast class (merged, PR #35):** stored probability is shown ONLY as "Diagnostic Win % (not valid for betting)"
  with the gate reasons; actionable columns stay blank. `status` / walk-forward label graded races `DIAGNOSTIC_SEED_BASELINE` /
  `TRAINED_MODEL` / `OTHER_MODEL` (from `score_runs.model_type`) and count the diagnostic ones as evidence only. `status` already counted
  these races (it reads stored probabilities): the old "status shows 0" note was wrong.
- **Paper trading is labelled (this branch):** each bet carries its run's forecast class; `place` prints it and notes diagnostic bets; `report`
  gives each class its own verdict and shows no pooled one; `status` splits settled bets by class (`src/services/forecast_class.py`, shared).
  Paper bets on diagnostic forecasts are allowed as an experiment, never a basis for a real bet. Fixed `place --dry-run` (it never worked).
- **Auto build + score (this branch):** a validated bundle import builds features and scores once per uploaded file and card (`src/app/auto_score.py`);
  failures never undo the import; late captures are scored but flagged ungradable. Verified by uploading in the real app on a sandbox DB.
- **Bet-type picks (this branch):** Race Board shows WIN/PLACE/SHOW picks labelled PRE-THRESHOLD with break-even $ per $2 (`src/services/bet_picks.py`,
  `docs/bet_picks.md`); stored pre-post in `bet_picks`, track record under the picks (hit rate vs expected, return at stored chart payoffs + CI, INSUFFICIENT <30), longshot warning; Results Import itself stores no place/show payoffs.
  Fixed a gate leak: the results page graded stored BET tags the board never showed (CT R10 "BET 1, ROI -100%"); withheld now. Thresholds untuned.
## RACE STATS / bundle
Optional bundle block, strict parser, stored raw + parsed in `twinspires_race_stats`, not scored; confirmed on card 75. A malformed block or one contradicting the DK card blocks the import. One sample only (BEL); bias samples tiny. `track_bias`/`trip_flags` empty.
## Open
- `scripts/race_board_cli.py`: headless gated Race Board for runs away from the PC (throwaway DB via `DERBYEDGE_DB`; docs/race_board_cli.md). Not merged yet.
- MNR R8 10-12 blocked by a PP row whose PRG cell was `1A (9)` (coupled entry in a past race): fixed in the PP row pattern (letter after the program). Not yet merged.
- SA R10 10-12 Results Import silently dropped Wild West End (a chart row with `---` in Last Raced = first-time starter), gave Psalm 9th (really 10th), then back-filled the horse that ran as scratched. Fixed: `---` rows read, an unreadable chart row now fails the import loudly, a real finisher clears a wrong scratch flag. Maiden-race charts imported before this fix may carry the same fault: unaudited. Not yet merged.
- TwinSpires SPEED/CLASS/PACE tabs + Profit Line captured (merged, PR #49; verified live on MNR R7; `docs/race_bundle_format.md`). NA-style runners stored as printed; `twinspires_pace_for_card` still drops pace for the whole race if any starter has no E/E-P/P/S style (MNR R7: 3 of 5 NA -> no pace). Count how often before deciding.
- Basic grid weight cell accepts a leading `*` (first-time Lasix, `*L121`; KEE R9 10-11 was blocked by it). CT R7 10-10 race_results had the on-track order for a DQ'd horse (Results Import); overwritten from the chart with `scripts/ingest_results_charts.py --populate --replace` (path that stores on-track order for DQs not yet fixed). 18 older GP charts fail the chart parser (not blocking).
- Results Import once rejected a real Equibase stakes chart whose sponsor title read as a "1/ST BET pre-race card": fixed (a chart's own structure wins). Unverified on the user's actual PDF.
- Issue #30 (LOW-confidence BET suppression) parked until real probabilities + live odds coexist; Race Board `Sum win prob 0.0000` still reads the gated column.
- Not built: real-bet log (DK slips), place/show payoff capture from the chart PDF, multi-user. Not handled: dead heats.
## Next move: live races uploaded before post with RACE STATS + the 3 tabs (tabs auto-advance races: check each), then read the `status` diagnostic cohort. Next lever is data.
