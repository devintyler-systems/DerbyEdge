"""Read-only DK markdown parser contract-audit CLI.

    python scripts/audit_dk_parser_contract.py --fixture <path> [--fixture <path> ...]

Parses each DK markdown fixture with the current parser, measures pre-race
evidence coverage (detected / active / scratched starters, identity completeness,
ALL RACES / WORKOUTS coverage), records the existing read-only validator's
verdict, and writes deterministic artifacts under ``output/acceptance/``.

Strictly read-only: no persistence, no SQLite, no readiness / scoring / feature
changes.  Exit 0 on success, 2 on an invalid or missing fixture path.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.dk_parser_contract_audit import (  # noqa: E402
    AUDIT_LABEL,
    audit_fixtures,
    write_audit_artifacts,
)

_ACCEPTANCE = ROOT / "output" / "acceptance"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only DK markdown parser contract audit")
    parser.add_argument(
        "--fixture", dest="fixtures", action="append", required=True, metavar="PATH",
        help="Local DK markdown fixture to audit (repeatable).",
    )
    parser.add_argument("--output-dir", default=str(_ACCEPTANCE))
    parser.add_argument(
        "--as-of", default=None,
        help="Optional ISO-8601 timezone-aware as_of override (default: fixture filename date @ 12:00Z).",
    )
    args = parser.parse_args(argv)

    for raw in args.fixtures:
        path = Path(raw)
        if not path.is_file():
            print(f"error: fixture not found: {raw}", file=sys.stderr)
            return 2

    as_of = None
    if args.as_of:
        try:
            as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        except ValueError:
            print(f"error: invalid --as-of: {args.as_of}", file=sys.stderr)
            return 2
        if as_of.tzinfo is None:
            print("error: --as-of must be timezone-aware", file=sys.stderr)
            return 2

    try:
        audit = audit_fixtures(args.fixtures, as_of=as_of)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    artifacts = write_audit_artifacts(audit, args.output_dir)

    for card in audit.cards:
        ready = (
            "NOT_AVAILABLE_READ_ONLY"
            if card.scoring_ready_under_current_rules is None
            else card.scoring_ready_under_current_rules
        )
        print(
            f"{card.fixture_name}: track={card.track_name} race={card.race_number} "
            f"detected={card.detected_runner_count} active={card.active_runner_count} "
            f"scratched={card.scratched_runner_count} retained_no_history={card.retained_without_history_count} "
            f"pct_all_races={card.pct_with_all_races_section:.3f} pct_workouts={card.pct_with_workouts_section:.3f} "
            f"parser_warnings={card.parser_warning_count} scoring_ready_current_rules={ready}"
        )
    totals = audit.summary["totals"]
    print(
        f"TOTALS: cards={audit.summary['card_count']} detected={totals['detected_runner_count']} "
        f"active={totals['active_runner_count']} scratched={totals['scratched_runner_count']} "
        f"retained_no_history={totals['retained_without_history_count']}"
    )
    print(f"status: {AUDIT_LABEL} (readiness policy unchanged; feature-vector completeness NOT_AVAILABLE_READ_ONLY)")
    for name, path in artifacts.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
