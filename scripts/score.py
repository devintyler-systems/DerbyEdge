"""
scripts/score.py — Score a Derby field and write the board.

Usage
-----
    python scripts/score.py                    # Derby 2026 (default)
    python scripts/score.py --card-id 3        # specific card
    python scripts/score.py --rebuild-features # rebuild feature store first

Outputs
-------
    output/derby_2026_board.csv
    output/derby_2026_board.md
    output/model_evaluation_dirt_route.md
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.models.scorer import score_race


def main() -> int:
    ap = argparse.ArgumentParser(description="DerbyEdge V1 scorer")
    ap.add_argument("--card-id", type=int, metavar="ID",
                    help="card_id to score (default: first Kentucky Derby card)")
    ap.add_argument("--rebuild-features", action="store_true",
                    help="Rebuild feature store before scoring")
    args = ap.parse_args()

    print("\nDerbyEdge scorer")
    print("=" * 44)

    if args.rebuild_features:
        from src.features.builder import build_features
        build_features(card_id=args.card_id)

    board = score_race(card_id=args.card_id)

    print("\n  2026 Kentucky Derby — Top 10 by Win Probability")
    print(f"  {'Rank':<5} {'Horse':<22} {'Post':<5} {'Win%':<7} "
          f"{'Fair Odds':<11} {'Edge':<8} {'Tag'}")
    print("  " + "-" * 66)
    for _, r in board.head(10).iterrows():
        edge_str = f"+{r['value_score']:.3f}" if r['value_score'] > 0 else f"{r['value_score']:.3f}"
        print(
            f"  {int(r['rank']):<5} {r['horse_name']:<22} {int(r['post_position']):<5} "
            f"{r['model_win_prob_pct']:.1f}%  {r['fair_odds']:>6.1f}-1    "
            f"{edge_str:<8} {r['bet_tag']}"
        )

    print(f"\n  Sum of win probabilities: "
          f"{board['model_win_prob'].sum():.6f}")
    print(f"\n  Bet candidates:  "
          f"{', '.join(board[board['bet_tag']=='bet']['horse_name'].tolist()) or 'none'}")
    print(f"  Underlays:       "
          f"{', '.join(board[board['bet_tag']=='underlay']['horse_name'].tolist()) or 'none'}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
