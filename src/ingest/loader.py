import pandas as pd
from pathlib import Path
from src.utils.db import get_connection

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEED = ROOT / "data" / "seeds" / "derby_2026_field.csv"

DERBY_FIELD_COLS = [
    "horse_name", "post_position", "morning_line_odds", "trainer", "jockey",
    "sire", "dam", "owner", "weight", "career_starts", "career_wins",
    "career_places", "career_shows", "career_earnings", "last_race_days_ago",
    "last_race_finish", "last_race_speed_figure", "best_speed_figure",
    "avg_speed_figure", "beyer_speed_figure", "dirt_starts", "dirt_wins",
    "dist_starts", "dist_wins", "wet_starts", "wet_wins", "workouts_past_30",
    "gate_class", "stamina_index", "pace_style",
]


def load_derby_field(csv_path: Path = None) -> pd.DataFrame:
    path = Path(csv_path) if csv_path else DEFAULT_SEED
    if not path.exists():
        raise FileNotFoundError(f"Seed file not found: {path}")

    df = pd.read_csv(path)
    missing = [c for c in DERBY_FIELD_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"Seed CSV missing columns: {missing}")

    conn = get_connection()
    conn.execute("DELETE FROM derby_field")
    df[DERBY_FIELD_COLS].to_sql("derby_field", conn, if_exists="append", index=False)
    conn.commit()
    conn.close()

    print(f"[loader] Loaded {len(df)} horses into derby_field from {path.name}")
    return df
