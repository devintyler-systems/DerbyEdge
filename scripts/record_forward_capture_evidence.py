"""Read-only forward-capture evidence ledger CLI.

    python scripts/record_forward_capture_evidence.py --entry-json path/to/entry.json
    python scripts/record_forward_capture_evidence.py --entry-json a.json --entry-json b.json

Validates each operator evidence-entry worksheet, enriches it with deterministic
file/hash data when the local artifact exists, and writes a stable-ordered ledger
under ``output/acceptance/``.  It never ingests, never opens SQLite, and never
generates a passing manifest.  Exit 0 on success, 2 on invalid input.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.forward_capture_evidence import (  # noqa: E402
    DRAFT_LABEL,
    ForwardCaptureEvidenceError,
    build_bundle_summaries,
    build_evidence_entry,
    load_entry_documents,
    write_evidence_artifacts,
)

_ACCEPTANCE = ROOT / "output" / "acceptance"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only forward-capture evidence ledger")
    parser.add_argument(
        "--entry-json", dest="entries", action="append", required=True, metavar="PATH",
        help="Path to an evidence-entry JSON (repeatable). May hold one object or a list.",
    )
    parser.add_argument("--output-dir", default=str(_ACCEPTANCE))
    args = parser.parse_args(argv)

    try:
        documents = load_entry_documents(args.entries)
        entries = [build_evidence_entry(doc) for doc in documents]
    except ForwardCaptureEvidenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summaries = build_bundle_summaries(entries)
    artifacts = write_evidence_artifacts(entries, summaries, args.output_dir)

    incomplete = sum(
        1 for s in summaries
        if not (s.pre_race_capture_present and s.result_capture_present) or s.unresolved_fields
    )
    print(f"status:               {DRAFT_LABEL}")
    print(f"entries recorded:     {len(entries)}")
    print(f"race bundles:         {len(summaries)}")
    print(f"incomplete bundles:   {incomplete}")
    for name, path in artifacts.items():
        print(f"{name}: {path}")
    print("NOTE: EVIDENCE_LEDGER_ONLY — does not satisfy the historical snapshot contract.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
