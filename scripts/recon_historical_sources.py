"""Read-only historical-source reconnaissance CLI.

    python scripts/recon_historical_sources.py --root <path> [--root <path> ...]

Inventories local candidate race-card and result artifacts and emits a
deterministic, source-neutral acquisition queue.  It never ingests, parses into
canonical tables, mutates SQLite, trains, scores, calibrates, or changes
eligibility.  Exit 0 when the scan succeeds (regardless of what it finds);
exit 2 for an invalid root.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.historical_source_recon import (  # noqa: E402
    ReconRootError,
    scan_roots,
    write_recon_artifacts,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only historical-source reconnaissance")
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
        path = Path(raw)
        if "://" in str(raw):
            print(f"error: only local filesystem paths are accepted: {raw}", file=sys.stderr)
            return 2
        if not path.exists() or not path.is_dir():
            print(f"error: invalid root (not an existing directory): {raw}", file=sys.stderr)
            return 2

    try:
        report = scan_roots(args.roots)
    except ReconRootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    artifacts = write_recon_artifacts(report, args.output_dir)
    summary = report.summary

    print(f"candidate-card count:            {summary['candidate_card_count']}")
    print(f"candidate-result count:          {summary['candidate_result_count']}")
    print(f"deterministic pair count:        {summary['deterministic_pair_count']}")
    print(f"contract-complete candidates:    {summary['contract_complete_candidate_count']}")
    print(f"unknown / unsupported:           {summary['unknown_candidate_count']} / {summary['unsupported_count']}")
    for name, path in artifacts.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
