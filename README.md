# DerbyEdge Engine — v0.5.1

Pre-race probabilistic prediction system for thoroughbred racing.
Optimizes for predictive accuracy, calibration, and market inefficiency capture
— not for narrative.

## What's new in v0.5.1 — Screenshot ingestor

- **Drop a sportsbook screenshot → race appears in the UI.** Anthropic Claude
  vision parses BetOnline / FanDuel / DRF / etc. race-card screenshots into
  structured JSON, then the engine inserts a race shell + entries + odds
  snapshot in one click.
- **Honest limit (surfaced in the UI):** a screenshot-ingested race has zero
  past-performance history. The model falls back to the base-rate prior, so
  `Model%` is essentially uniform. Use this view as an odds dashboard with
  devig + Kelly math, not for genuine model edge. A yellow badge appears
  above the edge sheet whenever the selected race has no PP history.
- **Idempotent re-ingest** with an "Overwrite if race exists" checkbox.
- **Track-name fallback:** if the vision model can't read the Equibase code,
  the ingestor maps common track names to codes (Mountaineer Park → MNR,
  Churchill Downs → CD, etc.).
- **Setup:** set `ANTHROPIC_API_KEY` once permanently:
  ```powershell
  [Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
  ```
  Restart PowerShell + Streamlit. Done.
- **17 tests** added (107 total, all green).


## Race-day quickstart (4 commands)

From the repo root, after the DB is built and a model is trained:

```powershell
# 1. Activate the venv
.\.venv\Scripts\Activate.ps1

# 2. Make sure OOF predictions exist (used by the calibration plot)
$env:PYTHONPATH = "src"
python scripts/train_baseline.py    # one-time, regenerates models/baseline_v0.3.pkl

# 3. Launch the UI
streamlit run app/streamlit_app.py

# 4. In the sidebar:
#    - Pick the track + race
#    - Set bankroll + max-Kelly cap
#    - Drop a FanDuel-shaped CSV (see samples/race_day_template.csv)
#    -> edge sheet recomputes with stake $ per playable bet
```

Use `samples/race_day_template.csv` as a starting point. Mapping rules from a
FanDuel race-card export are in `samples/RACE_DAY_TEMPLATE_README.md`.

## What's new in v0.5

- **Kelly stake column** on the edge sheet. Sidebar inputs: bankroll ($) and max-Kelly cap (%). Stake = `kelly_fraction(p, dec, cap=user_cap) * bankroll`. Capped quarter-Kelly by default.
- **Bet-tag filter** in the sidebar (multi-select). Defaults to `STRONG` + `BET` so the playable rows surface first.
- **CSV export button** on the current race's edge sheet. Raw numerics, current filters applied.
- **Reliability curve** ("Model trust" expander) plotting predicted vs observed win rate by 10 quantile bins, computed from out-of-fold walk-forward predictions (`data/processed/oof_predictions.parquet`).
- **Live odds uploader** in the sidebar. Drop a CSV in the `odds_template.csv` schema; the engine writes snapshots, rebuilds `odds_features`, and re-renders edges in one click.
- **Bankroll summary line** under the table: number of playable bets and total stake as % of bankroll.
- **Sample race-day template** at `samples/race_day_template.csv` with multi-book fields, plus mapping notes for FanDuel CSV exports.

## What's new in v0.4

- **Workout features** (`workout_features.py`): per-entry trainer-intent and fitness signals derived from the `workouts` table (1,241 rows, 98% coverage on today's entries). Computes `workout_score` (0–10), days since last work, 30/60d work counts, sharp `H`-type flag, regular-pattern flag, long-work (≥6f) flag.
- **As-of safety**: tests verify a workout dated after race_date cannot leak into a past row's features. Every feature is strictly less-than race_date.
- **UI surfaces it**: edge sheet now shows `WrkScr`, `LastWk`, `#Wks30d` columns next to model probabilities.
- **84 tests** (was 74) — adds 10 workout no-leak/scoring tests.

### Why workouts didn't go into the model (yet)

The `workouts` table in the Equibase free dataset is **today-card-only** — each row maps to a 2023 entry, not to historical PPs. So historical training rows have zero workout context to learn from. Three options were considered:

1. Skip them — wastes the 1,241 rows already loaded.
2. Train on today-only and score today-only — leakage risk; corpus too thin for separate model.
3. **Compute today-side workout signals and surface them as an explicit decision input** — honest about what the data supports, no model-degrade risk.

Went with option 3. `workout_score` is computed at scoring time, not used by the trained logistic, but displayed prominently on the edge sheet so it informs human handicapping. When corpus expands and historical workouts are loaded, `workout_features.py` plugs into `historical_features.py` with a one-line change.

## What's new in v0.3

- **Connection priors** (`connection_priors.py`): per-jockey, trainer, and J/T-combo win rates with surface conditioning. AS-OF semantics enforced via expanding-window pass; no future leak. Beta(2,12) smoothing pulls thin samples toward the league rate.
- **Streamlit UI** (`app/streamlit_app.py`): race selector, edge-sheet table with bet-tag color coding, chaos overlay toggle, model-vs-market and devig-prob bar charts, and per-horse odds-drift sparklines.
- **PowerShell launcher** (`scripts/run_ui.ps1`): one command to start the UI.
- **74 tests** (was 62) — adds 10 no-leak prior tests and 2 app smoke tests.

### Honest read on the priors

At the current 2.1K-row corpus, surface-conditioned and J/T-combo cells are too thin to add signal beyond the league prior — including the 8 prior features dropped AUC from 0.643 → 0.632. **Priors are wired and tested but disabled by default**; activate with `DERBYEDGE_USE_CONNECTION_PRIORS=1` once the corpus expands to ≥ 10K rows. The infrastructure is correct; the data isn't there yet.

## What's in v0.2

```
src/derbyedge/
  schema.py          Core SQLite schema (10 tables, normalized entities)
  parser.py          Equibase SIMD XML -> normalized records
  loader.py          Records -> SQLite
  features.py        Per-entry feature row (run-style, late_fig_z, devcurve, ...)
  chaos_patch.py     Derby family override: dark-horse flag + reallocation
  odds_schema.py     v0.1: markets, odds_snapshots, odds_features tables
  odds_math.py       v0.1: conversions, devig, drift, edge, Kelly, bet tags
  odds_ingest.py     v0.1: CSV adapter (works) + HTTP adapter scaffolding
  odds_features.py   v0.1: latest/best/median + devig + publicness + drift
  edge_calc.py       v0.1: model_prob vs market_prob -> fair odds + bet tag
  historical_features.py v0.2: per-PP as-of training rows
  evaluation.py      v0.2: log-loss, Brier, AUC, ECE, ROI
  backtest.py        v0.2: walk-forward time splits (quarterly / yearly)
  model.py           v0.2: surface-routed logistic + isotonic calibration
  scoring.py         v0.2: today's features -> trained model -> race softmax
models/
  baseline_v0.2.pkl  Trained surface models (D, T)
tests/
  test_chaos_patch.py    10 tests; codifies the 2026 Golden Tempo lesson
  test_odds_math.py      19 tests; conversions/devig/drift/edge/Kelly/tags
  test_odds_pipeline.py   2 tests; CSV -> ingest -> features -> edge
  test_evaluation.py     17 tests; metrics + walk-forward splits
  test_model.py          8 tests; pipeline + softmax + determinism
data/
  raw/               SIMD*.xml + Equibase Parameters.xlsx
  processed/         SQLite DB + feature parquet
samples/
  odds_template.csv  Minimal odds CSV template
  aqu_demo_odds.csv  Real-race demo (16-runner Aqueduct turf)
scripts/
  setup.ps1          Windows / PowerShell bootstrap (v0)
  run_pipeline.py    End-to-end ingest -> features -> chaos demo
  migrate_v01.py     Apply v0.1 schema migrations to existing DB
  ingest_odds.ps1    Ingest odds CSV + recompute odds_features
  run_edge.ps1       Produce edge_sheet.csv (placeholder model)
  train_baseline.py  v0.2/3: walk-forward backtest + persist trained model
  score_today.ps1    v0.2: trained model -> edge sheet
  run_ui.ps1         v0.3: launch Streamlit UI
app/
  streamlit_app.py   v0.3: race selector + edge sheet + charts
src/derbyedge/
  connection_priors.py  v0.3: AS-OF jockey/trainer/J-T win-rate priors
  workout_features.py   v0.4: per-entry workout pattern + score
```

## Baseline model performance (v0.3)

Walk-forward quarterly backtest, surface-routed logistic regression, no
calibration in folds (calibrated only on the final full-data model):

| Metric | Value | Vs naive baseline |
|---|---|---|
| Mean AUC (6 folds) | **0.643** | 0.50 = random |
| Mean log-loss | **0.401** | 0.430 (predict base rate) |
| Mean Brier | **0.117** | 0.123 (predict base rate) |
| Mean ECE (10 bins) | **0.060** | <0.05 is well-calibrated |

Training set: 2,119 historical PP rows, 14.9% win base rate, 2019-04 to 2023-12.
Surface models: D (1,598 rows), T (519 rows). Test scoring on a 16-runner
Aqueduct race produces sensible separation (model probs 5.2-8.5%, fading the
3.2-decimal favorites at 19.7% market prob, flagging value at the 22-42 dec
longshots).

Full metrics: `docs/baseline_metrics.md`.

**This is plumbing-grade signal, not bankable yet.** AUC 0.64 is real lift but the corpus is small and feature space narrow.

### v0.3 priors experiment

| Variant | Active features | Mean AUC | Mean log-loss |
|---|---|---|---|
| v0.2 baseline | 13 core | 0.6430 | 0.4005 |
| All 8 priors (jockey, trainer, J/T, surface, starts) | 21 | 0.6316 | 0.4123 |
| Winrate-only (3 priors) | 16 | 0.6406 | 0.4039 |
| J/T combo only | 14 | 0.6426 | 0.4005 |

More features did not help. The Beta(2,12) smoothing correctly pulls thin samples toward the league rate, which means at this corpus size the priors carry mostly league rate (≈ a constant), and logistic adds noise. **Priors are gated behind `DERBYEDGE_USE_CONNECTION_PRIORS=1`** so the default model matches v0.2. The expectation: priors will pay off once corpus ≥ 10K rows.

Next: larger corpus (biggest unlock), class-distance interactions, workout features (1,241 rows already in DB), Beyer vs Equibase native decision, live FanDuel/DK adapter.

## Schema

Normalized entities matching the system prompt:

| Table | Rows (current data) | Notes |
|---|---|---|
| `tracks` | 60 | Track ID + country |
| `people` | 1,074 | Jockeys, trainers, owners (Equibase external IDs) |
| `horses` | 360 | Pedigree (sire, dam, dam-sire) |
| `races` | 41 | Today's entry-card races |
| `entries` | 361 | Today's starters |
| `horse_starts` | 2,462 | Past performances |
| `fractions` | 14,772 | Sectional fraction times |
| `point_of_call` | 17,234 | Position at S/1/2/3/4/5/F |
| `company_line` | 7,386 | Top-3 finishers in each PP |
| `workouts` | 1,241 | Morning workouts before today's race |

All times are **integer hundredths of a second**. Lengths are **integer hundredths of a length**. Odds are **int / 100**. Equibase sentinel `9999` for speed-figure and `0` for pace-figures = "not assigned" — handled as NULL in `features.py`.

## Feature layer (per entry, race-aware)

| Feature | Source | Used by |
|---|---|---|
| `n_starts`, `n_starts_route`, `n_starts_today_surface`, `n_starts_today_distbucket` | PPs | DistanceProj, surface fit |
| `avg_speed_last3`, `best_speed_last3`, `speed_trend_slope` | Equibase Speed Figure | recent_form, devcurve |
| `pace2_avg_last3` | PaceFigure2 | early_pace_z |
| `finish_energy_raw` | (1st-call pos) − (finish pos) | late_fig_z, FinishEnergy |
| `run_style` ∈ {E, EP, P, S} | Avg 1st-call position | PaceFit |
| `recent_fig_z`, `late_fig_z`, `early_pace_z` | Within-race z-scores | All downstream scores |
| `devcurve_score` (0-10) | speed_trend_slope | Dark-horse flag |
| `finish_energy_score` (0-10) | late_fig_z | Dark-horse flag |
| `pacefit_score` (0-10) | run_style + race-shape | Chaos beneficiary |
| `distance_proj_score` (0-10) | Surface + distance experience | Dark-horse flag |
| `days_since_last`, `layoff_flag` | PPs | Layoff penalty |

## Derby Chaos Patch

Codifies the 2026 Derby calibration. Activates on **3yo G1 dirt routes, field ≥ 14, ChaosIndex ≥ 0.7**.

**Pipeline order:**
1. **Favorite-archetype multipliers** — Renegade-type ×0.90, Commandment-type ×0.95, Further Ado-type ×1.05.
2. **Dark-horse floor** — strong-tail closers (UpsideScore ≥ 0.7, late_fig_z ≥ 0.7, PaceFit ≥ 7) get `WinProb ≥ 3.5%` floor.
3. **Chaos reallocation** — move 5–10% of total mass from over-trusted chalk to flagged beneficiaries, weighted by `UpsideScore × max(late_fig_z, 0.1)`.
4. **Re-normalize** to sum 1.0.

**Tunable parameters** (in `chaos_patch.py`):

| Param | Default | Why |
|---|---|---|
| `CHAOS_INDEX_THRESHOLD` | 0.70 | When to activate |
| `CHAOS_MIN_REALLOCATION` | 0.05 | Floor when chaos = threshold |
| `CHAOS_MAX_REALLOCATION` | 0.10 | Cap at chaos = 1.0 |
| `DARK_HORSE_WIN_FLOOR` | 0.035 | Golden Tempo floor |
| `DARK_PROB_BAND_LO` | 0.015 | Lowered from 0.03 to capture Golden Tempo |
| `DARK_PROB_BAND_HI` | 0.12 | No favorites |
| `DARK_PUBLICNESS_MAX` | 7.0 | No public darlings |

## Running it

### Windows (PowerShell)

```powershell
# From the repo root:
PowerShell -ExecutionPolicy Bypass -File scripts\setup.ps1

# Build DB + features (one-time):
python scripts\run_pipeline.py

# Train baseline:
python scripts\train_baseline.py

# Launch UI:
PowerShell -ExecutionPolicy Bypass -File scripts\run_ui.ps1
# -> http://localhost:8501
```

### Activate connection priors (optional, sets the env flag in PowerShell)

```powershell
$env:DERBYEDGE_USE_CONNECTION_PRIORS = "1"
python scripts\train_baseline.py   # retrain with priors
PowerShell -ExecutionPolicy Bypass -File scripts\run_ui.ps1
```

### Linux / macOS / WSL

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python scripts/run_pipeline.py
PYTHONPATH=src pytest tests/ -v
```

Outputs:
- `data/processed/derbyedge.sqlite` — query with any SQLite client
- `data/processed/entry_features.parquet` (and `.csv`)

## Anti-patterns intentionally avoided

- No global model — surface/distance/age normalization built into the feature layer.
- No raw lifetime win-pct or earnings as primary signals.
- No post-race info bleed: features only read PPs strictly before today's date.
- No random train-test splits across racing rows (when modeling lands, splits will be **time-forward only**).

## Odds layer (v0.1)

Odds enter via CSV — most flexible and resilient input. HTTP adapters for
FanDuel/DraftKings/TwinSpires are scaffolded with documented endpoints but
**not auto-fetched** because each requires authenticated session + US IP.
Run those from your Windows box if you want live pulls; everything downstream
is identical.

**CSV format** (any one odds column suffices):

```
book_id,race_id,program_number,decimal_odds,american_odds,morning_line,is_morning_line,is_scratched,captured_at
morningline,CD|2026-05-02|12,1,,,4-1,1,0,
fanduel,CD|2026-05-02|12,1,5.5,,,0,0,
draftkings,CD|2026-05-02|12,1,,400,,0,0,
```

**Race-day flow:**

```powershell
# 1. Paste current odds into a CSV (per the format above)
# 2. Ingest + recompute odds_features
.\scripts\ingest_odds.ps1 -CsvPath .\my_odds.csv
# 3. Produce edge sheet (with your model probs CSV; or omit for placeholder)
.\scripts\run_edge.ps1 -ModelProbsCsv .\my_model.csv
```

**Derived per entry** (`odds_features` table):

| Field | Meaning |
|---|---|
| `morning_line_dec` / `morning_line_prob` | Pulled from `book_id='morningline'` if present |
| `best_dec_now` / `best_book_now` | Best (longest) decimal across non-ML books — your bettor edge |
| `median_dec_now` | Sanity check vs outliers |
| `market_prob_devig` | Avg of devigged win probs across books (proportional devig) |
| `publicness_score` | 0–10 vs equal-mass baseline. 5 = field average. ≥7 = heavily backed |
| `odds_drift_pct` / `drift_direction` | Best-odds drift vs earliest snapshot today |
| `n_books` | Distinct books offering this runner |

**Edge calc** (`edge_calc.build_edge_table`):

- `edge` = (model_prob − market_prob) / market_prob
- `ev` = expected value per $1 stake at best decimal
- `kelly_frac` = fractional Kelly, capped at 5%
- `bet_tag` ∈ {`STRONG_PLAY`, `VALUE_PLAY`, `WATCH`, `FADE`, `PASS`, `NO_MARKET`}

## What's NOT in v0.1 (next on the list)

| Gap | Plan |
|---|---|
| No win-probability model (only features) | Train softmax over features by race-family. Need >1 year of data. |
| Equibase Speed Figure ≠ Beyer | Add a Beyer ingest path or stay native. |
| HTTP odds fetchers are skeletons | Plug your Windows box's authenticated session in; pipeline is ready. |
| Workout features unused | Bullet-work counts, sectional speed, days-from-last-work. |
| No backtest harness | Walk-forward eval on prior Derbies once we have results data. |
| No UI | Streamlit edge-sheet viewer with race-day filtering. |

## Citation

Schema reference: Equibase Free Dataset Overview (`data/raw/Equibase Parameters.xlsx`).
2026 Derby calibration source: `docs/derby_engine_darkhorse.md` (Perplexity Computer chat archive).
