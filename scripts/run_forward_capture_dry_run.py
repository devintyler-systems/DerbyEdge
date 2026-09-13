"""Synthetic end-to-end forward-capture contract dry-run CLI.

    python scripts/run_forward_capture_dry_run.py

Uses synthetic fixture data only.  Proves the workflow

    raw pre-race artifact + result reference -> evidence ledger
    -> forward-capture prefill -> operator-completed manifest
    -> historical snapshot contract audit

and that only complete explicit operator evidence yields a contract-passing
manifest.  Never ingests, never opens SQLite, never builds features / trains /
scores / calibrates / produces market or wager artifacts.

Exit 0 only when the expected pass/fail behavior is observed; exit 2 for
malformed CLI options or fixture setup failure.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.forward_capture_dry_run import (  # noqa: E402
    DRY_RUN_STATUS,
    DryRunSetupError,
    build_synthetic_dry_run_inputs,
    execute_dry_run,
    write_dry_run_artifacts,
)

_ACCEPTANCE = ROOT / "output" / "acceptance"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic forward-capture contract dry-run")
    parser.add_argument("--output-dir", default=str(_ACCEPTANCE))
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    out = Path(args.output_dir)
    fixtures = out / "forward_capture_dry_run_fixtures"
    try:
        inputs = build_synthetic_dry_run_inputs(fixtures)
        result = execute_dry_run(inputs, out)
        artifacts = write_dry_run_artifacts(result, out)
    except DryRunSetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"status: {DRY_RUN_STATUS}")
    print(f"candidate_key:                    {result.candidate_key}")
    print(f"evidence_bundle_status:           {result.evidence_bundle_status}")
    print(f"prefill_draft_status:             {result.prefill_draft_status}")
    print(f"incomplete_draft_audit_passed:    {result.incomplete_draft_audit_passed}  (expected False)")
    print(f"completed_manifest_audit_passed:  {result.completed_manifest_audit_passed}  (expected True)")
    print("failure matrix (each expected REJECTED):")
    for name in sorted(result.failure_case_results):
        case = result.failure_case_results[name]
        mark = "ok " if case["rejected"] else "!! "
        print(f"  {mark}{name} [{case['stage']}]")
    print("prohibited side-effect checks:")
    for name in sorted(result.prohibited_side_effect_checks):
        ok = result.prohibited_side_effect_checks[name]
        print(f"  {'ok ' if ok else '!! '}{name}")
    for label, path in artifacts.items():
        print(f"{label}: {path}")

    if result.overall_ok:
        print("RESULT: PASS (expected pass/fail behavior observed; DRY_RUN_ONLY_NOT_ELIGIBLE)")
        return 0
    print("RESULT: FAIL (unexpected pass/fail behavior)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
