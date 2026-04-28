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

## Disclaimer

For entertainment and educational purposes only.
