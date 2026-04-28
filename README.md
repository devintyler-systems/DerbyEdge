# DerbyEdge Engine 🏇

A fully local horse-racing prediction engine for the 2026 Kentucky Derby.  
No cloud. No external APIs. Runs entirely on your machine.

## Stack
- **Python 3.11** — runtime
- **SQLite** — local database
- **pandas / NumPy** — data wrangling
- **scikit-learn + XGBoost** — machine learning
- **Streamlit + Plotly** — interactive dashboard

## Project Structure

```
derbyedge-engine/
├── db/schema.sql               SQLite schema
├── data/seeds/                 Seed CSV files
├── src/
│   ├── utils/db.py             DB connection & init
│   ├── ingest/loader.py        CSV → SQLite ingestion
│   ├── features/builder.py     Feature engineering
│   ├── models/trainer.py       XGBoost training (+ fallback)
│   ├── models/scorer.py        Score Derby field, write outputs
│   └── app/app.py              Streamlit dashboard
├── scripts/                    CLI entry points
├── output/                     Generated board CSV + Markdown
└── saved_models/               Persisted model artifacts
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Initialize the database
python scripts/init_db.py

# 3. Load the Derby seed file
python scripts/ingest.py

# 4. Build the feature store
python scripts/build_features.py

# 5. Score the Derby field
python scripts/score.py

# 6. Launch the Streamlit app
streamlit run src/app/app.py
```

## 2026 Derby Field (featured horses)

| Post | Horse | ML Odds |
|------|-------|---------|
| 1  | Renegade        | 4-1  |
| 6  | Commandment     | 6-1  |
| 18 | Further Ado     | 6-1  |
| 11 | Valiant Knight  | 8-1  |
| 12 | Chief Wallabee  | 8-1  |
| 9  | The Puma        | 10-1 |
| 15 | Emerging Market | 15-1 |

## Scoring Methodology

The engine uses a **weighted composite model** when fewer than 50 historical
race entries are present (the default out-of-the-box state). Weights:

| Factor | Weight |
|--------|--------|
| Speed Figures | 25 % |
| Market Odds   | 20 % |
| Career Form   | 15 % |
| Distance/Stamina | 15 % |
| Freshness     | 10 % |
| Class (Earnings) | 8 % |
| Workouts      | 7 % |

Load 50+ `race_entries` rows to activate the XGBoost classifier automatically.

---
*For entertainment and educational purposes only.*
