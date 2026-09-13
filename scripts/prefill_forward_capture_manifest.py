"""Read-only forward-capture manifest prefill CLI.

    python scripts/prefill_forward_capture_manifest.py \
        --dk-card <path-to-future-DK-pre-race-card> \
        --result-row-json <path-to-equibase-result-row-json>

Emits a DRAFT worksheet (identity fields prefilled, operator gaps listed) under
``output/acceptance/``.  It never ingests, never opens SQLite, and its draft
cannot pass the contract audit without operator-supplied evidence.

Exit 0 on successful prefill generation; exit 2 on invalid inputs.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.forward_capture_prefill import (  # noqa: E402
    DRAFT_LABEL,
    ForwardCapturePrefillError,
    load_result_row,
    prefill_forward_capture,
    write_prefill_artifacts,
)

_ACCEPTANCE = ROOT / "output" / "acceptance"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only forward-capture manifest prefill")
    parser.add_argument("--dk-card", required=True, help="Path to the future DK pre-race card file")
    parser.add_argument(
        "--result-row-json", required=True,
        help="Path to a JSON file holding one Equibase result-index row (or {\"rows\": [...]})",
    )
    parser.add_argument("--output-dir", default=str(_ACCEPTANCE))
    args = parser.parse_args(argv)

    try:
        row = load_result_row(Path(args.result_row_json))
        result = prefill_forward_capture(Path(args.dk_card), row)
    except ForwardCapturePrefillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    artifacts = write_prefill_artifacts(result, args.output_dir)
    print(f"draft_status:        {DRAFT_LABEL}")
    print(f"candidate_key:       {result.candidate_key}")
    print(f"prefilled_fields:    {len(result.prefilled)}")
    print(f"operator_gaps:       {len(result.gaps)}")
    for name, path in artifacts.items():
        print(f"{name}: {path}")
    print("NOTE: DRAFT_NOT_ELIGIBLE — fill every operator gap and pass "
          "scripts/audit_historical_snapshot_contract.py before any ingestion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
