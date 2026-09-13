"""CLI for the DerbyEdge read-only model-readiness audit."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.model_readiness_audit import FAMILIES, run_model_readiness_audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only DerbyEdge historical model-readiness audit")
    scope = parser.add_mutually_exclusive_group(required=False)
    scope.add_argument("--all-families", action="store_true", help="audit every supported model family (default)")
    scope.add_argument("--family", choices=FAMILIES)
    parser.add_argument("--surface", choices=("dirt", "turf", "synthetic", "all_weather"))
    parser.add_argument("--distance-bucket", choices=("sprint", "route"))
    parser.add_argument("--as-of-cutoff", help="inclusive ISO-8601 decision timestamp cutoff")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "acceptance")
    parser.add_argument("--db", type=Path, default=ROOT / "db" / "derbyedge.db")
    args = parser.parse_args()
    result = run_model_readiness_audit(args.db, args.output_dir, family=args.family, surface=args.surface, distance_bucket=args.distance_bucket, as_of_cutoff=args.as_of_cutoff)
    print("Read-only model readiness audit complete")
    for row in result["family_summaries"]:
        print(f"{row['model_family']}: races={row['race_count_total']} eligible={row['race_count_training_eligible']} first_blocker={row['first_blocker']}")
    print(result["paths"]["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
