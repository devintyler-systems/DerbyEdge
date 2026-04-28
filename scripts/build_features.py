import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features.builder import build_features

if __name__ == "__main__":
    df = build_features()
    print(df[["horse_name", "composite_score"]].sort_values("composite_score", ascending=False).to_string(index=False))
