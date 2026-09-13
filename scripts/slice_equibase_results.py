"""Read-only Equibase full-card result slicer CLI.

    python scripts/slice_equibase_results.py --root data/raw/historical_results

Inventories ``eqb_*_fullcard.pdf`` charts and emits a deterministic race-level
candidate reference index.  It never writes split PDFs, never rewrites source
files, and never opens SQLite.  Exit 0 on scan success (regardless of what it
finds); exit 2 for an invalid root.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.equibase_result_slicer import (  # noqa: E402
    ResultSliceRootError,
    scan_result_roots,
    write_result_index_artifacts,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Equibase full-card result slicer")
    parser.add_argument(
        "--root", dest="roots", action="append", required=True, metavar="PATH",
        help="Local directory to scan (repeatable). Local paths only.",
    )
    parser.add_argument(
        "--output-dir", default=str(ROOT / "output" / "acceptance"),
        help="Directory for deterministic artifacts (default: output/acceptance/).",
    )
    args = parser.parse_args(argv)

    for raw in args.roots:
        if "://" in str(raw):
            print(f"error: only local filesystem paths are accepted: {raw}", file=sys.stderr)
            return 2
        path = Path(raw)
        if not path.exists() or not path.is_dir():
            print(f"error: invalid root (not an existing directory): {raw}", file=sys.stderr)
            return 2

    try:
        index = scan_result_roots(args.roots)
    except ResultSliceRootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    artifacts = write_result_index_artifacts(index, args.output_dir)
    summary = index.summary

    print(f"fullcard pdf count:          {summary['fullcard_pdf_count']}")
    print(f"race sections indexed:       {summary['race_sections_indexed']}")
    print(f"deterministic candidate keys:{summary['deterministic_candidate_key_count']}")
    print(f"uncertain sections:          {summary['uncertain_section_count']}")
    for name, path in artifacts.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
