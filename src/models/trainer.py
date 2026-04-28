import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import MinMaxScaler

from src.utils.db import get_connection

ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = ROOT / "saved_models" / "derby_model.pkl"
SCALER_PATH = ROOT / "saved_models" / "scaler.pkl"

FEATURE_COLS = [
    "speed_score",
    "form_score",
    "distance_score",
    "class_score",
    "pace_score",
    "workout_score",
    "market_score",
]
MIN_TRAINING_ROWS = 50


def _count_historical() -> int:
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) FROM race_entries").fetchone()[0]
    conn.close()
    return n


def train_model() -> xgb.XGBClassifier | None:
    n = _count_historical()
    if n < MIN_TRAINING_ROWS:
        print(
            f"[trainer] Only {n} historical race entries (need {MIN_TRAINING_ROWS}+). "
            "Fallback scoring will be used."
        )
        return None

    conn = get_connection()
    # Build training set: each entry gets label=1 if finish_position==1 else 0
    df = pd.read_sql(
        """
        SELECT re.*, h.name as horse_name
        FROM race_entries re
        JOIN horses h ON re.horse_id = h.id
        WHERE re.finish_position IS NOT NULL
        """,
        conn,
    )
    conn.close()

    if df.empty or df["finish_position"].nunique() < 2:
        print("[trainer] Insufficient labeled data. Falling back.")
        return None

    df["label"] = (df["finish_position"] == 1).astype(int)

    # Minimal feature set available from raw entries
    raw_features = []
    for col in ["speed_figure", "beyer_figure", "morning_line_odds", "post_position", "weight"]:
        if col in df.columns:
            raw_features.append(col)

    X = df[raw_features].fillna(df[raw_features].median())
    y = df["label"]

    scaler = MinMaxScaler()
    X_scaled = scaler.fit_transform(X)

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        use_label_encoder=False,
        eval_metric="logloss",
        random_state=42,
    )
    model.fit(X_scaled, y)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(model, f)
    with open(SCALER_PATH, "wb") as f:
        pickle.dump(scaler, f)

    print(f"[trainer] XGBoost model saved to {MODEL_PATH}")
    return model


def fallback_probabilities(composite_scores: np.ndarray) -> np.ndarray:
    """Softmax over composite scores with temperature scaling."""
    temp = 8.0
    exp_s = np.exp((composite_scores - composite_scores.max()) * temp)
    return exp_s / exp_s.sum()
