import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from src.models.trainer import fallback_probabilities, train_model
from src.utils.db import get_connection

ROOT = Path(__file__).resolve().parents[2]


def _place_show_probs(win_probs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = len(win_probs)
    place = 0.45 * win_probs + 0.55 * (1.0 / n)
    show = 0.35 * win_probs + 0.65 * (1.0 / n)
    return place / place.sum(), show / show.sum()


def score_derby() -> pd.DataFrame:
    conn = get_connection()
    features_df = pd.read_sql(
        """
        SELECT hf.*, df.post_position, df.morning_line_odds,
               df.pace_style, df.trainer, df.jockey
        FROM horse_features hf
        JOIN derby_field df ON hf.horse_name = df.horse_name
        ORDER BY df.post_position
        """,
        conn,
    )
    conn.close()

    if features_df.empty:
        raise RuntimeError("horse_features is empty — run build_features first.")

    model = train_model()
    if model is None:
        win_probs = fallback_probabilities(features_df["composite_score"].values)
        model_type = "fallback_weighted"
    else:
        from pathlib import Path
        import pickle

        scaler_path = ROOT / "saved_models" / "scaler.pkl"
        with open(scaler_path, "rb") as f:
            scaler = pickle.load(f)

        feature_cols = [
            "speed_score", "form_score", "distance_score", "class_score",
            "pace_score", "workout_score", "market_score",
        ]
        X = features_df[feature_cols].values
        raw = model.predict_proba(scaler.transform(X))[:, 1]
        win_probs = raw / raw.sum()
        model_type = "xgboost"

    place_probs, show_probs = _place_show_probs(win_probs)
    run_id = str(uuid.uuid4())[:8]

    results = features_df.copy()
    results["win_probability"] = np.round(win_probs * 100, 2)
    results["place_probability"] = np.round(place_probs * 100, 2)
    results["show_probability"] = np.round(show_probs * 100, 2)
    results["model_type"] = model_type
    results["run_id"] = run_id
    results["rank"] = (
        results["win_probability"].rank(ascending=False, method="min").astype(int)
    )

    out_df = results[
        [
            "run_id", "horse_name", "post_position", "morning_line_odds",
            "win_probability", "place_probability", "show_probability",
            "composite_score", "model_type", "rank",
        ]
    ]

    conn = get_connection()
    conn.execute("DELETE FROM derby_predictions")
    out_df.to_sql("derby_predictions", conn, if_exists="append", index=False)
    conn.commit()
    conn.close()

    board = results.sort_values("rank").reset_index(drop=True)
    _write_output(board)
    return board


def _write_output(df: pd.DataFrame) -> None:
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)

    csv_cols = [
        "rank", "horse_name", "post_position", "morning_line_odds",
        "win_probability", "place_probability", "show_probability",
        "composite_score", "model_type", "pace_style", "trainer", "jockey",
    ]
    df[csv_cols].to_csv(out_dir / "derby_2026_board.csv", index=False)

    lines = [
        "# DerbyEdge Engine — 2026 Kentucky Derby Predictions",
        "",
        f"*Model: `{df['model_type'].iloc[0]}` | Run ID: `{df['run_id'].iloc[0]}`*",
        "",
        "| Rank | Horse | Post | ML Odds | Win % | Place % | Show % | Pace |",
        "|------|-------|------|---------|-------|---------|--------|------|",
    ]
    for _, r in df.iterrows():
        lines.append(
            f"| {int(r['rank'])} | **{r['horse_name']}** | {int(r['post_position'])} | "
            f"{r['morning_line_odds']:.0f}-1 | {r['win_probability']:.1f}% | "
            f"{r['place_probability']:.1f}% | {r['show_probability']:.1f}% | "
            f"{r['pace_style']} |"
        )

    (out_dir / "derby_2026_board.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[scorer] Output written to {out_dir}")
