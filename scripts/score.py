"""
scripts/score.py — Score a race card and write its board.

Usage
-----
    python scripts/score.py                    # Derby 2026 (default)
    python scripts/score.py --card-id 3        # specific card
    python scripts/score.py --rebuild-features # rebuild feature store first

Outputs
-------
    output/runs/{track}_{date}_r{race}/board.csv
    output/runs/{track}_{date}_r{race}/board.md
    output/runs/{track}_{date}_r{race}/metadata.json
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.models.scorer import score_race
from src.services.run_mode import ensure_scoring_eligible
from src.utils.db import get_connection, get_derby_card_id


def _load_card_metadata(card_id: int) -> dict:
    """Return the identity fields used in the human-facing score header."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT t.name AS track_name, t.abbrev AS track_abbrev,
                   rc.card_date, rc.race_number, rc.stakes_name
            FROM race_cards rc
            JOIN tracks t ON t.track_id = rc.track_id
            WHERE rc.card_id = ?
            """,
            (card_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError(f"No race card found for card_id={card_id}")
    return dict(row)


def main() -> int:
    ap = argparse.ArgumentParser(description="DerbyEdge V1 scorer")
    ap.add_argument("--card-id", type=int, metavar="ID",
                    help="card_id to score (default: first Kentucky Derby card)")
    ap.add_argument("--rebuild-features", action="store_true",
                    help="Rebuild feature store before scoring")
    args = ap.parse_args()

    print("\nDerbyEdge scorer")
    print("=" * 44)

    card_id = args.card_id if args.card_id is not None else get_derby_card_id()
    if card_id is None:
        raise RuntimeError("No default Kentucky Derby card found — pass --card-id.")
    card_meta = _load_card_metadata(card_id)

    gate_conn = get_connection()
    try:
        run_state = ensure_scoring_eligible(
            gate_conn, card_id, runs_root=ROOT / "data" / "runs"
        )
    finally:
        gate_conn.close()
    print(f"  Run mode: {run_state.mode.value}")

    if args.rebuild_features:
        from src.features.builder import build_features
        build_features(card_id=card_id)

    board = score_race(card_id=card_id)

    stakes = card_meta["stakes_name"] or "Race"
    print(
        f"\n  {card_meta['track_name']} ({card_meta['track_abbrev']}) — "
        f"{card_meta['card_date']} Race {card_meta['race_number']} · {stakes}"
    )
    print("  Top 10 by Win Probability")
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
