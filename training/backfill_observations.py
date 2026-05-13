"""
training/backfill_observations.py

Populate starter_observations from all races that already have both a score
run and ingested results.  Safe to run repeatedly (idempotent).

Usage
-----
    python -m training.backfill_observations
    python -m training.backfill_observations --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.db import get_connection
from src.services.observations import backfill_all_observations


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill starter_observations from history")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count rows that would be written without writing them",
    )
    args = parser.parse_args()

    conn = get_connection()

    if args.dry_run:
        n = conn.execute("""
            SELECT COUNT(DISTINCT es.entry_id)
            FROM entry_scores es
            JOIN score_runs sr ON sr.run_id = es.run_id
            JOIN entries e ON e.entry_id = es.entry_id
            WHERE EXISTS (
                SELECT 1 FROM race_results rr
                WHERE rr.card_id = sr.card_id
                  AND rr.entry_id = e.entry_id
            )
        """).fetchone()[0]
        print(f"[backfill] dry-run: {n} observation rows would be written")
        conn.close()
        return

    total = backfill_all_observations(conn)
    conn.close()
    print(f"[backfill] done — {total} rows written to starter_observations")


if __name__ == "__main__":
    main()
