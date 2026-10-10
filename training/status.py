"""Evidence status: scored / graded races and settled paper bets against the verdict thresholds, with a projected date.

    python -m training.status
    python -m training.status --json
Read-only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.evidence_status import build_status, render_text  # noqa: E402
from src.utils.db import DB_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(DB_PATH))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.db.exists():
        print(f"no database at {args.db}")
        return 1
    conn = sqlite3.connect(f"file:{args.db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        status = build_status(conn)
    finally:
        conn.close()
    print(json.dumps(status, indent=2) if args.json else render_text(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
