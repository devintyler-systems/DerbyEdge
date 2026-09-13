"""Capture a documented pre-post DK Basic or TwinSpires ODDS export."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.market_snapshot_intake import (  # noqa: E402
    MarketSnapshotError, ingest_market_snapshot,
)
from src.utils.db import get_connection  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest one proven pre-post current-ODDS capture")
    parser.add_argument("--card-id", type=int, required=True)
    parser.add_argument("--provider", choices=("draftkings", "twinspires"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--captured-at", required=True,
                        help="documented timezone-aware ISO-8601 capture time; file mtime is ignored")
    args = parser.parse_args()
    conn = get_connection()
    try:
        count = ingest_market_snapshot(
            conn, args.card_id, args.source, provider=args.provider,
            captured_at=args.captured_at,
        )
    except MarketSnapshotError as exc:
        parser.exit(1, f"market snapshot rejected: {exc}\n")
    finally:
        conn.close()
    print(f"card_id={args.card_id} provider={args.provider} captured_at={args.captured_at} rows_inserted={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
