"""Add firstbet_pp_starts and firstbet_career_stats tables.

Safe to re-run — uses CREATE TABLE IF NOT EXISTS.
Run: python scripts/migrate_add_firstbet_pp_starts.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.db import get_connection
from src.services.firstbet_enrich import ensure_firstbet_pp_table


def main() -> None:
    conn = get_connection()
    ensure_firstbet_pp_table(conn)

    # Add quality_tier column to score_runs (safe to re-run)
    try:
        conn.execute(
            "ALTER TABLE score_runs ADD COLUMN quality_tier TEXT NOT NULL DEFAULT 'seed_only'"
        )
        conn.commit()
        print("[migrate] score_runs.quality_tier column added")
    except Exception as e:
        if "duplicate column" in str(e).lower():
            print("[migrate] score_runs.quality_tier already exists — skipped")
        else:
            raise

    n_pp  = conn.execute("SELECT COUNT(*) FROM firstbet_pp_starts").fetchone()[0]
    n_cs  = conn.execute("SELECT COUNT(*) FROM firstbet_career_stats").fetchone()[0]
    print(
        f"[migrate] firstbet_pp_starts OK ({n_pp} rows) · "
        f"firstbet_career_stats OK ({n_cs} rows)"
    )
    conn.close()


if __name__ == "__main__":
    main()
