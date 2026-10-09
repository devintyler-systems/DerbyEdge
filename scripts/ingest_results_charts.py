"""Ingest Equibase full-card result charts (``eqb_<TRK>_<date>_fullcard.pdf``).

    python scripts/ingest_results_charts.py --root data/raw/historical_results --check
    python scripts/ingest_results_charts.py --root data/raw/historical_results
    python scripts/ingest_results_charts.py --root data/raw/historical_results --populate

--check     parse and validate every chart and, if a database exists, compare each race with its pre-race card
            (read-only; writes nothing)
(default)   store valid races in the result tables and record the reconciliation with each pre-race card
--populate  also feed MATCHED races into race_results for score-run grading (never overwrites unless --replace)

Exit status is 1 if any race failed its self-checks, any card is structurally wrong, or any card reconciliation
has errors; late scratches and jockey/surface changes are reported as warnings and do not fail the run.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingest.equibase_chart import parse_chart_pdf  # noqa: E402
from src.services.chart_results_intake import (  # noqa: E402
    ingest_chart_card, populate_race_results, reconcile_race_to_card, store_reconciliation, load_chart_race,
)
from src.utils.db import DB_PATH, get_connection  # noqa: E402


def _charts(root: Path) -> list[Path]:
    return sorted(root.rglob("eqb_*_fullcard.pdf"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT / "data" / "raw" / "historical_results")
    ap.add_argument("--check", action="store_true", help="validate and compare only; write nothing")
    ap.add_argument("--populate", action="store_true", help="feed MATCHED races into race_results")
    ap.add_argument("--replace", action="store_true", help="with --populate, overwrite differing race_results rows")
    args = ap.parse_args()

    files = _charts(args.root)
    if not files:
        print(f"no eqb_*_fullcard.pdf under {args.root}")
        return 1
    failed = 0
    conn = None
    if args.check:
        if Path(DB_PATH).exists():
            conn = sqlite3.connect(f"file:{Path(DB_PATH).as_posix()}?mode=ro", uri=True)
    else:
        conn = get_connection()
    try:
        for path in files:
            raw = path.read_bytes()
            try:
                card = parse_chart_pdf(raw)
            except Exception as exc:               # a corrupt / non-PDF file must fail the run, not crash it
                print(f"{path.name}: UNREADABLE ({type(exc).__name__}: {exc})")
                failed += 1
                continue
            invalid = [r for r in card.races if not r.valid]
            print(f"{path.name}: {len(card.races)} races, {len(card.races) - len(invalid)} valid"
                  + (f", CARD ERRORS {card.errors}" if card.errors else ""))
            for r in invalid:
                print(f"   INVALID {r.race_key}: {'; '.join(r.problems)}")
            failed += len(invalid) + len(card.errors)
            if args.check:
                if conn is not None and not card.errors:
                    for r in card.races:
                        if r.valid:
                            rec = reconcile_race_to_card(conn, r)
                            if rec.status != "NO_CARD":
                                print(f"   {r.race_key}: {rec.status} warnings={rec.codes()} errors={rec.codes('errors')}")
                                failed += len(rec.errors)
                continue
            report = ingest_chart_card(conn, card, source_filename=path.name, raw_bytes=raw)
            counts: dict[str, int] = {}
            for o in report.outcomes:
                counts[o.status] = counts.get(o.status, 0) + 1
            print(f"   {report.status} {counts}")
            rids = [o.result_race_id for o in report.outcomes if o.result_race_id]
            if report.status == "ALREADY_INGESTED":
                rids = [row[0] for row in conn.execute(
                    "SELECT result_race_id FROM result_races WHERE is_current=1 AND source_id=("
                    "SELECT source_id FROM result_sources WHERE sha256=? ORDER BY source_id DESC LIMIT 1)", (report.sha256,))]
            for rid in rids:
                rec = reconcile_race_to_card(conn, load_chart_race(conn, rid), rid)
                store_reconciliation(conn, rec)
                if rec.status != "NO_CARD":
                    print(f"   race {rid}: {rec.status} warnings={rec.codes()} errors={rec.codes('errors')}")
                    failed += len(rec.errors)
                if args.populate:
                    res = populate_race_results(conn, rid, replace=args.replace)
                    if res["status"] not in ("NO_CARD",):
                        print(f"   race {rid}: race_results {res['status']} {res['reason']}")
    finally:
        if conn is not None:
            conn.close()
    print("FAIL" if failed else "OK", f"({failed} problem(s))")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
