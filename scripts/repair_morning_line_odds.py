"""Repair fractional morning lines stored with the old decimal convention.

    python scripts/repair_morning_line_odds.py            # dry run: list what would change
    python scripts/repair_morning_line_odds.py --apply    # write the corrected values

Only cards imported from DraftKings Markdown can be repaired (their raw document is stored).
After applying, re-run the feature build and scoring for the affected cards: feature_store
.market_implied_prob and entry_scores were computed from the wrong value and are not touched here.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.morning_line_repair import apply_morning_line_repair, find_misstored_morning_lines  # noqa: E402
from src.utils.db import DB_PATH, get_connection  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the corrections (default: dry run)")
    args = parser.parse_args()
    if not Path(DB_PATH).exists():
        print(f"no database at {DB_PATH}")
        return 1
    conn = get_connection()
    try:
        rows = find_misstored_morning_lines(conn)
        for r in rows:
            print(f"card {r['card_id']:>4}  #{r['program']:<3} {r['horse']:<26} ML {r['source_ml']:>5}  "
                  f"stored {r['stored']:<7g} -> {r['correct']:g}")
        print(f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'} with a wrong stored morning line")
        if args.apply and rows:
            print(f"updated {apply_morning_line_repair(conn, rows)}; now rebuild features and re-score "
                  f"cards {sorted({r['card_id'] for r in rows})}")
        elif rows:
            print("dry run; pass --apply to write")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
