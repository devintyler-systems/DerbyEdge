"""Validate a future historical pre-race snapshot manifest without opening the DB."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.historical_snapshot_contract_audit import audit_manifest, write_acceptance_result


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only historical pre-race snapshot contract audit")
    parser.add_argument("--manifest", required=True, help="Path to a historical snapshot manifest JSON file")
    args = parser.parse_args()
    result = audit_manifest(args.manifest)
    output = write_acceptance_result(result, args.manifest, ROOT / "output" / "acceptance")
    print(f"contract_pass={result['contract_pass']}")
    print(f"rejection_codes={','.join(result['rejection_codes']) or 'NONE'}")
    print(f"artifact_sha256_status={result['evaluated_conditions'].get('artifact_sha256', {}).get('status', 'NOT_EVALUATED')}")
    print(f"acceptance_output={output}")
    return 0 if result["contract_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
