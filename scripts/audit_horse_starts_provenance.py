"""CLI wrapper for the read-only horse_starts provenance audit."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.horse_starts_provenance_audit import run_horse_starts_provenance_audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only horse_starts durable-provenance audit")
    parser.add_argument("--family")
    parser.add_argument("--db", type=Path, default=ROOT / "db" / "derbyedge.db")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "acceptance")
    args = parser.parse_args()
    result = run_horse_starts_provenance_audit(args.db, args.output_dir, family=args.family)
    print("family,total,repairable,partial,no_artifact,postrace,first_blocker")
    for row in result["summaries"]:
        print("{model_family},{total_rows},{repairable_count},{repairable_partial_count},{ineligible_no_artifact_count},{ineligible_postrace_count},{first_repair_blocker}".format(**row))
    print(f"schema_gap={result['schema']['schema_gap'] or 'none'}")
    print(result["paths"]["json"])
    return 0 if any(row["classification"] == "REPAIRABLE" for row in result["rows"]) else (1 if result["rows"] and all(row["classification"] == "INELIGIBLE_NO_ARTIFACT" for row in result["rows"]) else 0)


if __name__ == "__main__":
    raise SystemExit(main())
