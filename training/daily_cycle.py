"""End-of-day results cycle: ingest new Equibase charts, join them to the pre-race cards, feed grading, settle open
paper bets, and write a report for every paper policy.

    python -m training.daily_cycle                         # charts under data/raw/historical_results
    python -m training.daily_cycle --root path/to/charts --no-populate

Idempotent: a chart already stored is skipped, settled bets stay settled, race_results is never overwritten.
Every step runs even if an earlier one found problems, and the problems are listed at the end.
Exit status 1 when any chart was unreadable or invalid, any card/chart join has errors, or any bet is held;
open bets whose race has no chart yet are normal and do not fail the run.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingest.equibase_chart import parse_chart_pdf  # noqa: E402
from src.services.chart_results_intake import (  # noqa: E402
    ensure_chart_result_tables, ingest_chart_card, load_chart_race, populate_race_results, reconcile_race_to_card,
    store_reconciliation,
)
from src.services.paper_trading import build_report, ensure_paper_tables, render_markdown, settle_paper_bets  # noqa: E402
from src.utils.db import DB_PATH  # noqa: E402

HELD = ("HELD_RECONCILIATION", "HELD_NOT_IN_CHART", "HELD_NO_PAYOFF")


def run_cycle(conn: sqlite3.Connection, root: Path, out_dir: Path, *, populate: bool = True, n_boot: int = 2000) -> dict:
    ensure_chart_result_tables(conn)
    ensure_paper_tables(conn)
    problems: list[str] = []
    files = sorted(root.rglob("eqb_*_fullcard.pdf")) if root.exists() else []
    charts = {"files": len(files), "new": 0, "already": 0, "races_stored": 0, "populated": 0}
    for path in files:
        raw = path.read_bytes()
        try:
            card = parse_chart_pdf(raw)
        except Exception as exc:                                   # one bad file must not stop the cycle
            problems.append(f"{path.name}: unreadable ({type(exc).__name__}: {exc})")
            continue
        for r in card.races:
            if not r.valid:
                problems.append(f"{path.name} {r.race_key}: invalid chart: {'; '.join(r.problems)}")
        if card.errors:
            problems.append(f"{path.name}: card errors {card.errors}")
        report = ingest_chart_card(conn, card, source_filename=path.name, raw_bytes=raw)
        if report.status == "ALREADY_INGESTED":
            charts["already"] += 1
            continue
        charts["new"] += 1
        for o in report.outcomes:
            if not o.result_race_id:
                continue
            charts["races_stored"] += 1
            rec = reconcile_race_to_card(conn, load_chart_race(conn, o.result_race_id), o.result_race_id)
            store_reconciliation(conn, rec)
            if rec.errors:
                problems.append(f"{o.race_key}: card and chart disagree: {', '.join(e['code'] for e in rec.errors)}")
            if populate and rec.status == "MATCHED":
                res = populate_race_results(conn, o.result_race_id)
                charts["populated"] += 1 if res["populated"] else 0
                if res["status"] == "EXISTING_DIFFERS":
                    problems.append(f"{o.race_key}: race_results already differs from the chart (not overwritten)")
    settled = settle_paper_bets(conn)
    for k in HELD:
        if settled.get(k):
            problems.append(f"{settled[k]} paper bet(s) held: {k}; see paper_bets.note")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reports = []
    for (pid,) in conn.execute("SELECT policy_id FROM paper_policies ORDER BY created_at").fetchall():
        rep = build_report(conn, pid, n_boot=n_boot)
        (out_dir / f"{stamp}_{pid}.md").write_text(render_markdown(rep), encoding="utf-8")
        (out_dir / f"{stamp}_{pid}.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        s = rep["summary"]
        reports.append({"policy_id": pid, "name": rep["name"], "settled_bets": s["n_bets"], "open_bets": rep["open_bets"],
                        "roi": s["roi"], "mean_clv": s["mean_clv"], "verdict": s["verdict"]})
    result = {"time": stamp, "charts": charts, "settled": settled, "reports": reports, "problems": problems}
    (out_dir / f"{stamp}_cycle.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(DB_PATH))
    ap.add_argument("--root", type=Path, default=ROOT / "data" / "raw" / "historical_results")
    ap.add_argument("--out", type=Path, default=ROOT / "output" / "paper_trading")
    ap.add_argument("--no-populate", action="store_true", help="do not write race_results")
    ap.add_argument("--boot", type=int, default=2000)
    args = ap.parse_args(argv)
    if not args.db.exists():
        print(f"no database at {args.db}")
        return 1
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        res = run_cycle(conn, args.root, args.out, populate=not args.no_populate, n_boot=args.boot)
    finally:
        conn.close()
    c = res["charts"]
    print(f"charts: {c['files']} file(s), {c['new']} new, {c['already']} already stored, {c['races_stored']} race(s) stored, "
          f"{c['populated']} written to race_results")
    print("paper bets settled: " + (", ".join(f"{k}={v}" for k, v in sorted(res["settled"].items())) or "nothing to do"))
    for r in res["reports"]:
        roi = "n/a" if r["roi"] is None else f"{r['roi'] * 100:.1f}%"
        clv = "n/a" if r["mean_clv"] is None else f"{r['mean_clv'] * 100:.1f}%"
        print(f"  {r['name']} {r['policy_id']}: {r['settled_bets']} settled, {r['open_bets']} open, ROI {roi}, CLV {clv} -> {r['verdict']}")
    for p in res["problems"]:
        print("PROBLEM: " + p)
    print(f"reports in {args.out}")
    held = any(res["settled"].get(k) for k in HELD)
    return 1 if res["problems"] or held else 0


if __name__ == "__main__":
    raise SystemExit(main())
