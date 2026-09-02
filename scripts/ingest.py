"""
scripts/ingest.py — Load a Derby field CSV into the V1 normalized schema.

Usage
-----
    python scripts/ingest.py                              # Derby 2026 seed (default)
    python scripts/ingest.py --csv path/to/field.csv     # custom file

After running, query live entries via:
    SELECT * FROM v_entries_live ORDER BY post_position;
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingest.loader import load_derby_seed
from src.ingest.validate import run_validation
from src.utils.db import get_connection


def main() -> int:
    ap = argparse.ArgumentParser(description="DerbyEdge V1 field ingest")
    ap.add_argument(
        "--csv",
        metavar="PATH",
        help="Source CSV (default: data/seeds/derby_2026_field.csv)",
    )
    ap.add_argument(
        "--meta",
        metavar="PATH",
        help="Race metadata JSON; defaults to Derby 2026 metadata",
    )
    ap.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip post-load validation checks",
    )
    args = ap.parse_args()

    csv_path = Path(args.csv) if args.csv else None
    meta = None
    if args.meta:
        meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))

    print("\nDerbyEdge ingest")
    print("=" * 44)

    conn = get_connection()

    # ── load ─────────────────────────────────────────────────────────────────
    result = load_derby_seed(csv_path=csv_path, conn=conn, meta=meta)
    conn.commit()

    print(f"  [loader]  Source : {result.csv_path}")
    print(f"  [loader]  card_id: {result.card_id}  track_id: {result.track_id}")
    print(f"  [loader]  Horses    : {result.horses_new} new, {result.horses_existing} existing")
    print(f"  [loader]  Entries   : {result.entries_new} new, {result.entries_existing} existing")
    print(f"  [loader]  Odds snaps: {result.odds_snapshots}")
    if result.warnings:
        for w in result.warnings:
            print(f"  [loader]  WARN: {w}")

    # ── validate ─────────────────────────────────────────────────────────────
    if not args.skip_validate:
        src_checks, db_checks = run_validation(
            conn=conn,
            card_id=result.card_id,
            csv_path=Path(result.csv_path),
            load_result=result,
            expected_field=meta["expected_field"] if meta else 20,
        )
        fails = [c for c in src_checks + db_checks if c.status == "FAIL"]
        if fails:
            print("\n  VALIDATION FAILURES:")
            for c in fails:
                print(f"    FAIL: {c.name} — {c.detail}")
            conn.close()
            return 1

    conn.close()
    print("\n  Done. Query live entries:")
    print("    SELECT * FROM v_entries_live ORDER BY post_position;\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
