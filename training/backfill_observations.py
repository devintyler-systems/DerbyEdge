"""
training/backfill_observations.py

Populate starter_observations from all races that already have both a score
run and ingested results.  Safe to run repeatedly (idempotent).

Usage
-----
    python -m training.backfill_observations
    python -m training.backfill_observations --dry-run
    python -m training.backfill_observations --verbose
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.db import get_connection
from src.services.observations import append_observations

# Eligible-card query — mirrors the filter inside backfill_all_observations.
_ELIGIBLE_CARDS_SQL = """
    SELECT DISTINCT sr.card_id
    FROM   score_runs sr
    WHERE  EXISTS (
        SELECT 1 FROM race_results rr
        JOIN entries e ON e.entry_id = rr.entry_id
        WHERE e.card_id = sr.card_id
          AND rr.official_finish IS NOT NULL
    )
    ORDER BY sr.card_id
"""

# Dry-run count: entries with both a score run and a matching result row.
_DRY_RUN_SQL = """
    SELECT COUNT(DISTINCT es.entry_id)
    FROM entry_scores es
    JOIN score_runs sr ON sr.run_id = es.run_id
    JOIN entries e ON e.entry_id = es.entry_id
    WHERE EXISTS (
        SELECT 1 FROM race_results rr
        WHERE rr.card_id = sr.card_id
          AND rr.entry_id = e.entry_id
          AND rr.official_finish IS NOT NULL
    )
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill starter_observations from historical scores + results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count rows that would be written without writing them",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-race progress",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    conn = get_connection()

    # ── dry-run ────────────────────────────────────────────────────────────
    if args.dry_run:
        n = conn.execute(_DRY_RUN_SQL).fetchone()[0]
        print(f"[backfill] dry-run: {n} observation row(s) would be written")
        conn.close()
        return

    # ── find eligible cards ────────────────────────────────────────────────
    card_ids = [row[0] for row in conn.execute(_ELIGIBLE_CARDS_SQL).fetchall()]

    if not card_ids:
        print(
            "[backfill] no races found with both score runs and official results"
            " — nothing to backfill"
        )
        print(
            "  Ensure results have been ingested via the Race Results tab before"
            " running backfill."
        )
        conn.close()
        return

    print(f"[backfill] {len(card_ids)} race(s) eligible — starting backfill...")

    # ── per-card backfill ──────────────────────────────────────────────────
    total_starters = 0
    races_ok       = 0
    join_misses    = []   # card_ids where append returned 0

    for cid in card_ids:
        n = append_observations(conn, cid)
        if n > 0:
            races_ok      += 1
            total_starters += n
            if args.verbose:
                print(f"  card_id={cid:>5}  {n:>3} starter(s) written")
        else:
            join_misses.append(cid)
            if args.verbose:
                print(f"  card_id={cid:>5}  [join miss — no labeled rows returned]")

    conn.close()

    # ── summary ───────────────────────────────────────────────────────────
    miss_count = len(join_misses)
    print(
        f"[backfill] done — {races_ok} race(s) processed, "
        f"{total_starters} starter(s) inserted, "
        f"{miss_count} join miss(es)"
    )
    if miss_count:
        sample = join_misses[:5]
        more   = miss_count - len(sample)
        ids    = ", ".join(str(c) for c in sample)
        suffix = f" (+{more} more)" if more else ""
        print(f"  Join misses: card_id(s) {ids}{suffix}")
        print(
            "  A join miss means the card had a score run and result rows but"
            " the entry-level join returned nothing.  Check that entry_id values"
            " in entry_scores and race_results refer to the same entries."
        )


if __name__ == "__main__":
    main()
