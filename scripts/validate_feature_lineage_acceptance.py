"""Read-only persisted runtime-lineage acceptance gate."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.feature_lineage_acceptance import (  # noqa: E402
    read_feature_store_row,
    validate_persisted_feature_lineage,
    write_acceptance_artifacts,
)
from src.models.trainer import load_model_artifact  # noqa: E402
from src.utils.db import DB_PATH  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate persisted feature lineage without modifying the database."
    )
    parser.add_argument("--card-id", type=int, required=True)
    parser.add_argument("--entry-id", type=int, required=True)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "acceptance")
    parser.add_argument(
        "--model-path", type=Path, default=ROOT / "saved_models" / "dirt_route_v1.pkl",
        help="Read-only model artifact used only to mirror display weights (optional).",
    )
    args = parser.parse_args()

    try:
        feature_row = read_feature_store_row(args.db_path, args.card_id, args.entry_id)
    except (LookupError, OSError, sqlite3.Error) as exc:
        print(f"FAIL card_id={args.card_id} entry_id={args.entry_id}: {exc}")
        return 1

    importances: dict[str, float] = {}
    if args.model_path.exists():
        artifact = load_model_artifact(args.model_path)
        importances = dict(getattr(artifact, "feature_importances", {}) or {})
    result = validate_persisted_feature_lineage(
        feature_row, card_id=args.card_id, entry_id=args.entry_id,
        entry_importances=importances, diagnostics_importances=importances,
    )
    entry_path, diagnostics_path, summary_path = write_acceptance_artifacts(
        result, feature_row, args.output_dir,
    )
    print(f"card_id={args.card_id} entry_id={args.entry_id}")
    print("market_implied_prob is morning-line-derived; it is not a current-market wager input.")
    print(f"entry_details_csv={entry_path}")
    print(f"model_diagnostics_csv={diagnostics_path}")
    print(f"summary_json={summary_path}")
    if result["passed"]:
        print(f"PASS shared_feature_count={result['shared_feature_count']}")
        return 0
    print(f"FAIL violations={len(result['mismatches'])}")
    for issue in result["mismatches"]:
        print(json.dumps(issue, sort_keys=True, default=str))
    return 1


if __name__ == "__main__":
    sys.exit(main())
