import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.scorer import score_derby

if __name__ == "__main__":
    board = score_derby()
    print("\n--- 2026 Kentucky Derby Predictions ---")
    print(
        board[["rank", "horse_name", "post_position", "morning_line_odds",
               "win_probability", "place_probability", "show_probability"]]
        .rename(columns={
            "horse_name": "Horse",
            "post_position": "Post",
            "morning_line_odds": "ML",
            "win_probability": "Win%",
            "place_probability": "Place%",
            "show_probability": "Show%",
        })
        .to_string(index=False)
    )
