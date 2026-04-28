import pandas as pd
import numpy as np
from src.utils.db import get_connection


def _norm(val: float, lo: float, hi: float) -> float:
    return float(max(0.0, min(1.0, (val - lo) / (hi - lo))))


def _freshness(days: int) -> float:
    if 14 <= days <= 28:
        return 1.0
    if days < 14:
        return days / 14.0
    return max(0.2, 1.0 - (days - 28) / 90.0)


def build_features() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql("SELECT * FROM derby_field ORDER BY post_position", conn)

    if df.empty:
        conn.close()
        raise RuntimeError("derby_field is empty — run ingest first.")

    records = []
    for _, h in df.iterrows():
        speed_score = (
            0.40 * _norm(h["best_speed_figure"], 80, 120)
            + 0.35 * _norm(h["last_race_speed_figure"], 80, 120)
            + 0.25 * _norm(h["avg_speed_figure"], 80, 120)
        )

        win_pct = h["career_wins"] / max(h["career_starts"], 1)
        itm_pct = (h["career_wins"] + h["career_places"] + h["career_shows"]) / max(
            h["career_starts"], 1
        )
        form_score = 0.60 * win_pct + 0.40 * itm_pct

        dist_pct = (
            h["dist_wins"] / max(h["dist_starts"], 1)
            if h["dist_starts"] > 0
            else 0.25
        )
        distance_score = 0.55 * dist_pct + 0.45 * h["stamina_index"]

        class_score = _norm(h["career_earnings"], 0, 1_000_000)

        pace_map = {"front": 0.60, "presser": 0.80, "stalker": 0.75, "closer": 0.65}
        pace_score = pace_map.get(str(h["pace_style"]).lower(), 0.70)

        workout_score = min(1.0, h["workouts_past_30"] / 6.0) * (h["gate_class"] / 5.0)

        # market_score: raw inverse odds, will be normalized by composite weighting
        market_score = 1.0 / float(h["morning_line_odds"])

        freshness = _freshness(int(h["last_race_days_ago"]))

        # Scale market_score to roughly 0-1 range (4-1 favorite = 0.25 inverse)
        scaled_market = _norm(market_score, 0.02, 0.30)

        composite = (
            0.25 * speed_score
            + 0.20 * scaled_market
            + 0.15 * form_score
            + 0.15 * distance_score
            + 0.10 * freshness
            + 0.08 * class_score
            + 0.07 * workout_score
        )

        records.append(
            {
                "horse_name": h["horse_name"],
                "speed_score": round(speed_score, 4),
                "form_score": round(form_score, 4),
                "distance_score": round(distance_score, 4),
                "class_score": round(class_score, 4),
                "pace_score": round(pace_score, 4),
                "workout_score": round(workout_score, 4),
                "market_score": round(market_score, 4),
                "composite_score": round(composite, 4),
            }
        )

    features_df = pd.DataFrame(records)
    conn.execute("DELETE FROM horse_features")
    features_df.to_sql("horse_features", conn, if_exists="append", index=False)
    conn.commit()
    conn.close()

    print(f"[builder] Built features for {len(features_df)} horses")
    return features_df
