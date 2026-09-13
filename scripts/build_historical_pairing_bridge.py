"""Read-only historical pairing-bridge CLI.

    python scripts/build_historical_pairing_bridge.py \
        --card-inventory output/acceptance/historical_source_recon_inventory.csv \
        --result-index   output/acceptance/equibase_result_race_index.csv

Attempts exact ``track|YYYY-MM-DD|R{n}`` candidate-key pairing between the recon
card inventory and the Equibase race-level result index.  It never ingests,
mutates SQLite, or makes anything eligible.  Every output row is labelled
``RECON_ONLY_NOT_ELIGIBLE``.  Exit 0 on success; exit 2 for a missing input file.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.historical_pairing_bridge import (  # noqa: E402
    BridgeInputError,
    build_bridge,
    read_card_inventory_csv,
    read_result_index_csv,
    write_bridge_artifacts,
)

_ACCEPTANCE = ROOT / "output" / "acceptance"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only historical pairing bridge")
    parser.add_argument(
        "--card-inventory",
        default=str(_ACCEPTANCE / "historical_source_recon_inventory.csv"),
        help="Path to historical_source_recon_inventory.csv",
    )
    parser.add_argument(
        "--result-index",
        default=str(_ACCEPTANCE / "equibase_result_race_index.csv"),
        help="Path to equibase_result_race_index.csv",
    )
    parser.add_argument("--output-dir", default=str(_ACCEPTANCE))
    args = parser.parse_args(argv)

    try:
        card_rows = read_card_inventory_csv(args.card_inventory)
        result_rows = read_result_index_csv(args.result_index)
    except BridgeInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = build_bridge(card_rows, result_rows)
    artifacts = write_bridge_artifacts(report, args.output_dir)
    summary = report.summary

    print(f"bridge rows:            {summary['row_count']}")
    print(f"paired (exact key):     {summary['paired_exact_key_count']}")
    print(f"card only:              {summary['card_only_count']}")
    print(f"result only:            {summary['result_only_count']}")
    print(f"key mismatch:           {summary['key_mismatch_count']}")
    print(f"ambiguous result key:   {summary['ambiguous_result_key_count']}")
    print(f"contract-complete pairs:{summary['contract_complete_pair_count']}")
    for name, path in artifacts.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
