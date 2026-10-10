# DerbyEdge
project_handle: derbyedge

Local-first thoroughbred pre-race engine: imports DraftKings / TwinSpires race bundles, scores win probabilities, and
grades them (walk-forward) and paper-trades them against official results. This file is tracked in git and is the single
source of truth; the local copy is just the checkout. Volatile state lives in `docs/session_state.md`, not here.

## Read first, every session
1. This file. 2. `docs/session_state.md` (what is done, what is next; keep it under 40 lines, update it at the end of every outcome).
3. `docs/working_rules.md` (mandatory UPDATE YOUR LOCAL FILES block whenever work needs the user's machine).

## Stack and layout
Python 3.11, SQLite (`db/derbyedge.db`, gitignored; schema `db/schema.sql`), Streamlit app `src/app/app.py`, pytest (CI: `.github/workflows/pytest.yml`).
- `src/ingest/` parsers (DK markdown, TwinSpires, race bundle, Equibase charts); `src/services/` intake, evidence status, paper trading, walk-forward
- `src/models/` scorer, trainer, policy, confidence; `src/features/` feature builder; `src/derbyedge/` tracks, odds math
- `training/` CLIs: `python -m training.status | walk_forward | paper_trading | daily_cycle`; `scripts/` ingest and migration tools
- Docs: `docs/race_bundle_format.md`, `chart_parser.md`, `walk_forward.md`, `paper_trading.md`, `play_day_checklist.md`
- The user launches the app from the desktop **DerbyEdge** shortcut, which runs `start_derbyedge.bat` (Windows, `C:\Projects\derbyedge-engine`).

## Working agreement
- Develop on the branch the session names; draft PR; never push elsewhere. Commit messages explain why.
- One commit per requested job when the user asks for that. Run only the tests for files you change; run the full suite
  (`python -m pytest -q`) once before the final push.
- Failures must be loud: a malformed or contradictory input blocks the import with a named reason, never a silent partial read.
- Report faithfully: say what was verified and what was not. Do not claim a result you did not run.
- The user does manual steps on their own machine: always give exact PowerShell steps and the expected output.

## Evidence rules (do not weaken)
- Only count a race as scored or graded if the run used has real model win probabilities (not a morning-line collapse).
- As-of discipline: decisions use only data captured before post. Late captures are flagged, not used for live features.
- Capture-now-use-later data (e.g. TwinSpires RACE STATS) is stored raw and is NOT fed to scoring until there is outcome data to validate it.
- A forecast the app's eligibility gate blocks (uncalibrated seed baseline) is DIAGNOSTIC: graded as labelled evidence, shown only as "not valid for betting", never used for real bets, fair odds or edge.
- Never pool score runs from different engine versions (`score_runs.engine_version`); `legacy` is a labelled baseline.
- Verdicts stay `INSUFFICIENT_DATA` below the documented thresholds (30 graded races; 100 settled paper bets, 50 for CLV).

## Carried over from the earlier CLAUDE.md (NOT re-verified against current code; confirm before relying)
- Speed figures normalized to a common scale with track variant applied; do not write figures without it.
- Class ladder G1 > G2 > G3 > Listed > Allowance > Claiming; pace classified E / E/P / P / S; pace is directional, flag low-confidence scenarios.
- Kelly sizing with a capped unit size; do not hardcode Kelly multipliers.
- Do not modify raw source files (`data/raw/`) after ingestion.
- Before enabling any ML model, run it in shadow mode against the baseline (ranks, probability deltas, confidence, bet tags); keep the `.venv` lock before an XGBoost upgrade.
