# DerbyEdge Engine V1

A fully local, normalized horse-racing prediction engine for the 2026 Kentucky Derby.  
No cloud. No external APIs. Runs entirely on your machine.

## Stack

| Layer      | Tool                         |
|------------|------------------------------|
| Runtime    | Python 3.11                  |
| Storage    | SQLite 3.31+ (WAL, generated cols) |
| Data       | pandas / NumPy               |
| ML         | XGBoost + scikit-learn       |
| UI         | Streamlit + Plotly           |

---

## V1 Pipeline Commands

```bash
# 0. Install dependencies (once)
pip install -r requirements.txt

# 1. Initialize the V1 database schema
python scripts/init_db.py

# 2. Migrate from V0 (if upgrading) OR skip for a fresh install
python scripts/migrate_schema.py          # safe to re-run; --drop-legacy optional

# 3. Load Derby field into normalized tables
python scripts/ingest.py

# 4. Build the feature store
python scripts/build_features.py

# 5. Score the Derby field
python scripts/score.py

# 6. Launch the Streamlit operator console
streamlit run src/app/app.py
```

---

## V1 Schema (13 tables)

```
tracks            — physical racing plants
race_cards        — individual races (distance_furlongs = generated column)
horses            — canonical horse profiles
people            — trainers / jockeys / owners
entries           — horse registration per race
                    morning_line_prob = generated column
horse_starts      — official race results (source of historical form)
workouts          — individual workout records (grade: B/F/G/N)
odds_snapshots    — time-series odds (implied_prob = generated column)
track_bias        — observed pace / post bias per day
trip_flags        — post-race trouble annotations
model_registry    — ML artifact metadata + evaluation metrics
score_runs        — links a race card to a model run
entry_scores      — per-horse predictions
                    fair_odds    = generated column
                    model_edge   = generated column
```

### Generated columns

| Table            | Column               | Expression                                  |
|------------------|----------------------|---------------------------------------------|
| `race_cards`     | `distance_furlongs`  | `ROUND(distance_yards / 220.0, 2)`          |
| `entries`        | `morning_line_prob`  | `ROUND(1.0 / (morning_line_odds + 1.0), 6)` |
| `odds_snapshots` | `implied_prob`       | `odds_denominator / (odds_numerator + odds_denominator)` |
| `entry_scores`   | `fair_odds`          | `(1.0 / win_probability) - 1.0`             |
| `entry_scores`   | `model_edge`         | `win_probability - market_implied_prob`      |

### Indexes

`card_date`, `track_id`, `horse_id`, `trainer_id`, `jockey_id`, `run_id`, `rank`

---

## V0 → V1 Migration Guide

### What changed

| V0 table            | V1 replacement                               |
|---------------------|----------------------------------------------|
| `derby_field`       | `entries` (join `horses`, `race_cards`, `people`) |
| `horse_features`    | Rebuilt in Stage 3 feature store             |
| `derby_predictions` | `score_runs` + `entry_scores`                |
| `races`             | `race_cards`                                 |
| `race_entries`      | `horse_starts`                               |

### Breaking change — probability storage

V0 stored `win_probability` as a **percentage** (e.g. `26.79`).  
V1 stores all probabilities as **fractions** in `[0, 1]` (e.g. `0.2679`).  
`fair_odds` and `model_edge` are derived automatically as generated columns.

### Migration steps

```bash
# Step 1 — back up and apply new DDL (idempotent)
python scripts/migrate_schema.py

# Step 2 — optionally drop the five legacy tables
python scripts/migrate_schema.py --drop-legacy
```

The migration script:
1. Backs up `db/derbyedge.db` → `db/derbyedge_v0_backup.db`
2. Applies V1 DDL (`CREATE TABLE IF NOT EXISTS` — safe to re-run)
3. Migrates `derby_field` → `tracks`, `race_cards`, `horses`, `people`, `entries`
4. Converts `workouts_past_30` counts → synthetic `workouts` rows (`synthetic=1`)
5. Migrates `derby_predictions` → `score_runs` + `entry_scores` (converts % to fraction)
6. Writes `output/migration_report.txt`

---

## 2026 Derby Field (20 starters)

| Post | Horse            | ML Odds | Jockey               | Trainer        |
|------|------------------|---------|----------------------|----------------|
|  1   | Renegade         |  4-1    | John Velazquez       | Todd Pletcher  |
|  6   | Commandment      |  6-1    | Javier Castellano    | Brad Cox       |
| 18   | Further Ado      |  6-1    | Irad Ortiz Jr.       | Bill Mott      |
| 11   | Valiant Knight   |  8-1    | Luis Saez            | Todd Pletcher  |
| 12   | Chief Wallabee   |  8-1    | Joseph Talamo        | Wayne Lukas    |
|  9   | The Puma         | 10-1    | Victor Espinoza      | Doug O'Neill   |
| 15   | Emerging Market  | 15-1    | Joel Rosario         | Chad Brown     |

---

## First ML shadow run

If you are setting up the shadow evaluation pipeline for the first time, or after
populating a fresh database, run these four steps in order:

```powershell
# 1. Populate starter_observations from historical scores + results (one-time, idempotent)
python -m training.backfill_observations

# 2. Train win-probability model artifacts from those observations
python -m training.train_win_model

# 3. Score the next race in shadow mode (ML logged, heuristic still served)
.\scripts\run_shadow_cycle.ps1

# 4. After race results are ingested, evaluate shadow predictions vs. outcomes
.\scripts\run_shadow_cycle.ps1 -SkipScore
```

**Why this order matters:**

| Step | Requires | Produces |
|------|----------|----------|
| `backfill_observations` | `entry_scores` + `race_results` in DB | `starter_observations` rows |
| `train_win_model` | `starter_observations` rows | `models/artifacts/*.pkl` |
| `run_shadow_cycle` (score) | `models/artifacts/*.pkl` | `output/shadow_log.csv` with `ml_win_prob` |
| `run_shadow_cycle -SkipScore` | ingested results + `shadow_log.csv` | promotion decision |

Skipping step 2 does not crash the scorer, but `ml_win_prob` will be `null` in the
shadow log and the evaluation will report **"No ML metrics available"**.  Use
`-RequireMlArtifact` to hard-fail before scoring if no artifact is present:

```powershell
.\scripts\run_shadow_cycle.ps1 -RequireMlArtifact
```

> **Tip:** `train_win_model` needs at minimum ~10 labeled races per segment.  With
> fewer races it falls back to a pooled model.  Run
> `python -m training.backfill_observations --dry-run` to preview how many
> observation rows are available before training.

---

## One-command shadow cycle

Run the full ML shadow evaluation pipeline — schema check, scoring, backfill,
evaluation, and report — with a single command:

```powershell
# PowerShell (recommended on Windows)
.\scripts\run_shadow_cycle.ps1

# Or Python directly
python -m training.run_shadow_cycle
```

**Common invocations:**

```powershell
# Score + full evaluation (default)
.\scripts\run_shadow_cycle.ps1

# Skip re-scoring; evaluate existing shadow_log.csv
.\scripts\run_shadow_cycle.ps1 -SkipScore

# Apply horse_norm migration automatically and run everything
.\scripts\run_shadow_cycle.ps1 -AutoMigrate

# Just re-generate the markdown report (no re-scoring or re-evaluation)
.\scripts\run_shadow_cycle.ps1 -ReportOnly

# Score a specific race card
.\scripts\run_shadow_cycle.ps1 -CardId 7
```

**What it does, in order:**

| Step | Description | Skip flag |
|------|-------------|-----------|
| Schema check | Validates `race_cards` + `starter_observations` columns | `--skip-migration` |
| Migration check | Detects missing `horse_norm` column; optionally applies it | `--auto-migrate` |
| Shadow scoring | Runs `scripts/score.py` with `DERBYEDGE_ML_MODE=shadow` | `--skip-score` |
| Backfill join | `backfill_shadow_eval` — joins shadow log with outcomes | — |
| Evaluation | `promote_check` — writes `eval_run_*/` artifacts | — |
| Report | `generate_promotion_report` — writes `output/ml_promotion_report.md` | — |
| Summary | Compact terminal output of decision + match rate + next action | — |

**Safety:**
- The command always enforces `DERBYEDGE_ML_MODE=shadow`.  If `live` is set in
  the environment, the script aborts rather than accidentally re-scoring in live mode.
- Pass `--skip-score` to evaluate existing data without touching serving mode.

### Workflow hints (Claude Code hook)

When working in Claude Code, a `Stop` hook in `.claude/settings.json` runs
`scripts/hooks/shadow_workflow_hint.py` at the end of every Claude turn.  The
script checks the pipeline state and prints the next suggested command when
something actionable is waiting:

| State detected | Suggested command |
|----------------|-------------------|
| shadow_log newer than shadow_eval | `python -m training.backfill_shadow_eval` |
| shadow_eval newer than last eval run | `python -m training.promote_check` |
| Decision = PASS | `$env:DERBYEDGE_ML_MODE='live'; python scripts/score.py` |
| Decision = HOLD or INSUFFICIENT_DATA | Score more races in shadow mode |

The hook is silent when the pipeline is fully up to date.

---

## ML Serving Modes and Promotion Workflow

DerbyEdge supports three serving modes controlled by a single environment variable:

```
DERBYEDGE_ML_MODE=off     # default — heuristic only, ML never runs
DERBYEDGE_ML_MODE=shadow  # ML scored + logged; heuristic still served
DERBYEDGE_ML_MODE=live    # ML served; heuristic fallback on failure
```

The Derby override (Churchill Downs dirt route 18+ runners) always uses the
heuristic regardless of mode.

### What each mode means

| Mode | ML runs? | What is served | Shadow log written? |
|------|----------|----------------|---------------------|
| `off` | No | Heuristic | No |
| `shadow` | Yes | Heuristic | Yes |
| `live` | Yes | ML (heuristic fallback) | Yes |

### Commands

```bash
# 0. One-time: apply horse_norm migration to existing database
python -m training.migrate_horse_norm

# 1. Score in shadow mode (ML logged, heuristic served)
$env:DERBYEDGE_ML_MODE="shadow"; python scripts/score.py

# 2. Backfill evaluation dataset (join shadow log with outcomes)
python -m training.backfill_shadow_eval
# Writes: output/shadow_eval.csv
#         output/join_diagnostics.json
#         output/unmatched_shadow_rows.csv

# 3. Run promotion check (evaluate ML vs heuristic, write report)
python -m training.promote_check

# 4. Generate operator promotion report
python -m training.generate_promotion_report
# Report written to: output/ml_promotion_report.md

# 5. Promote to live after PASS decision
$env:DERBYEDGE_ML_MODE="live"; python scripts/score.py
```

### Join strategy

The backfill step joins `output/shadow_log.csv` (ML predictions) with
`starter_observations` (race outcomes) using a normalised horse name key.

**Normalisation rules** applied to every horse name before matching:
1. Lowercase and trim
2. Replace `&` with `and`
3. Remove: apostrophes `'`, periods `.`, commas `,`, hyphens `-`, parentheses `()`
4. Collapse repeated whitespace to a single space

**Key priority order:**
1. `race_id + post + horse_norm` — primary (exact post + normalised name)
2. `race_id + horse_norm` — fallback (used only if post is unavailable in either source)

**Join diagnostics** (`output/join_diagnostics.json`) report the match rate after
both passes.  If match rate is below 80%, check `output/unmatched_shadow_rows.csv`.

**What to inspect first when match rate is low:**
- Open `unmatched_shadow_rows.csv` — the `_key_full` column shows exactly which
  keys failed.  Compare with the horse names in `starter_observations`.
- Common causes: apostrophes/accents in names (`O'Brien` vs `OBrien`), post
  position missing in one source, or race results not yet loaded
  (run `python -m training.backfill_observations`).
- The normalisation step handles most punctuation differences automatically;
  remaining mismatches are usually a data-entry inconsistency in the source.

### Promotion artifacts

Each `python -m training.promote_check` run writes to `output/eval_run_TIMESTAMP/`:

| File | Contents |
|------|----------|
| `metrics_summary.json` | Overall heuristic vs ML metrics |
| `segment_metrics.csv` | Per-segment log loss, Brier, hit rates, winner counts |
| `insufficient_segments.csv` | Segments below minimum sample-size thresholds |
| `calibration_table.csv` | Probability bin reliability (both models) |
| `promotion_decision.json` | PASS / HOLD / FAIL / INSUFFICIENT_DATA + exact reasons |
| `join_diagnostics.json` | Match rate from most recent backfill run |
| `unmatched_shadow_rows.csv` | Shadow rows with no outcome match |

### How to read the promotion decision

- **PASS** — ML log loss improved ≥ 3% overall AND Brier improved ≥ 2% overall,
  no segment degraded by > 1%, and all integrity checks passed.
  Set `DERBYEDGE_ML_MODE=live`.
- **HOLD** — Overall data is sufficient but improvement thresholds not yet met,
  or individual segments are below the minimum sample size.
  Stay in shadow mode and collect more races.
- **FAIL** — A segment degraded, integrity checks failed, or ML outputs are
  missing. Roll back: investigate, retrain, re-evaluate.
- **INSUFFICIENT_DATA** — Fewer than 30 labeled races overall.  No meaningful
  promotion decision can be made.  Continue scoring in shadow mode.

### Promotion sample-size thresholds

| Threshold | Value | Effect if not met |
|-----------|-------|-------------------|
| Overall minimum races | 30 | Decision = INSUFFICIENT_DATA |
| Per-segment minimum races | 10 | Segment flagged as insufficient (HOLD note) |
| Per-segment minimum winners | 10 | Segment flagged as insufficient (HOLD note) |

Insufficient segments are listed in `insufficient_segments.csv` and noted in
the `promotion_decision.json` reasons.  An insufficient segment never blocks a
PASS if the overall data is strong — it only contributes to HOLD reasons.

### Shadow log

`output/shadow_log.csv` accumulates one row per starter per scored race when
mode is `shadow` or `live`. Fields include `horse_norm` (normalised name used
for join keys), heuristic/ML/served probabilities, ranks, model version, and
metadata for every horse.

---

## Disclaimer

For entertainment and educational purposes only.
