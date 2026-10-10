# Session state (update at the end of every outcome; keep under 40 lines)
Working rules, including the mandatory UPDATE YOUR LOCAL FILES block: `docs/working_rules.md`.
_Last updated: 2026-10-10, `main` = PRs #32-#34 merged (CT R7 import fix); the diagnostic-forecast PR is the newest. Local machine must be on `main`._
## Done and merged to main
Morning-line fix + repair, BAQ, Equibase chart parser + store/join/populate, walk-forward, paper trading, daily cycle, `training.status`,
engine-version stamp, RACE STATS capture, import fixes (`L122bOn` Basic weight cell, `9 MTP ALLOWANCE` post display), repo tidy,
`CLAUDE.md` tracked, `docs/working_rules.md`. User's machine: `C:\Projects\derbyedge-engine` on `main`; launch via desktop **DerbyEdge**
shortcut (runs `start_derbyedge.bat`). 57 older races lack a post time (`AS_OF_UNPROVEN`); existing runs are `legacy`, never pooled.
## Why Win % is blank on real races (diagnosed 2026-10-10, CT R7 + BEL R5 reproduced in a sandbox)
- **The model DOES produce a forecast** (stored `entry_scores.win_probability` sums to 1, not collapsed to the morning line). The app's
  fail-closed gate (`score_delivery.contain_ineligible_board` -> `score_eligibility`) then blanks Win %, fair odds, edge, tags for every entry.
  (Earlier notes blamed the collapse guard `scorer.py` ~1394: wrong for these runs.) Not yet confirmed against the user's own DB.
- Gate reasons, all 9 entries: `calibration_unavailable_or_unaudited` (seed-only baseline; promotion needs >=4,000 labeled starters),
  `active_input_unavailable_or_defaulted...` (~54% of the weight is on empty features: speed_best_3/speed_last/beyer_last, work_readiness,
  trainer_intent, traffic_resilience, horses_beaten, finish_energy, derby_override_score), `unsupported_or_unknown_runtime_source`
  (TwinSpires-sourced pace is not in `SUPPORTED_SOURCES`), `source_evidence_missing_or_not_proven_as_of_decision_timestamp`.
- **Diagnostic display + forecast class (this branch):** stored probability is shown ONLY as "Diagnostic Win % (not valid for betting)"
  with the gate reasons; actionable columns stay blank. `status` / walk-forward label graded races `DIAGNOSTIC_SEED_BASELINE` /
  `TRAINED_MODEL` / `OTHER_MODEL` (from `score_runs.model_type`) and count the diagnostic ones as evidence only. `status` already counted
  these races (it reads stored probabilities): the old "status shows 0" note was wrong.
- **Open risk:** paper trading reads stored probabilities / bet tags and never consults the gate, so it can place paper bets on diagnostic
  forecasts. Decide whether to restrict or label before any paper-bet verdict.
- Next lever is data, in this order: capture every race pre-post with the full bundle; measure the diagnostic cohort vs morning line;
  then add inputs one at a time (TwinSpires SPD as a labelled proxy, DK jockey/trainer stats) and re-measure. Calibration needs outcomes.
## RACE STATS / bundle
Optional bundle block, strict parser, stored raw + parsed in `twinspires_race_stats`, not scored; confirmed on card 75 (post-race TEST row).
A malformed block or one contradicting the DK card blocks the import. One sample only (BEL); bias samples tiny. `track_bias`/`trip_flags` empty.
## Open
- Pasting caution: DK and TwinSpires tabs auto-advance to the next race. Bare `ALLOWANCE` (no `$`) leaves `race_class` NULL (no scoring effect).
- Issue #30 (explain LOW-confidence BET suppression): parked until real probabilities + live odds coexist.
- Optional: backfill post times for the 57 legacy races (dry run first). Not built: real-bet log, multi-user. Not handled: dead heats.
## Next move
A live race uploaded before post with its RACE STATS block, then read the `status` diagnostic cohort as the baseline.
