"""Walk-forward evaluation of the engine's stored win probabilities against the morning line, the live market
and the closing odds.  Read-only.

    python -m training.walk_forward
    python -m training.walk_forward --since 2026-09-01 --track CD --min-races 30
    python -m training.walk_forward --list-versions         # how many graded races each engine version has
    python -m training.walk_forward --engine-version legacy # races scored before engine versions were stamped

One engine version per run, never pooled: by default the newest stamped version (the engine in use now); until a
stamped run exists, the legacy races are graded and the report says so.

Writes report.md, summary.json and per_race.csv under output/walk_forward/<UTC timestamp>/ and prints the report.
Exit status is 1 when fewer than --min-races races could be graded (the default of 0 never fails on size).
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.walk_forward_eval import (  # noqa: E402
    evaluate, load_graded_races, per_race_rows, render_markdown,
)
from src.utils.db import DB_PATH  # noqa: E402


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(DB_PATH))
    ap.add_argument("--out-dir", type=Path, default=ROOT / "output" / "walk_forward")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--track")
    ap.add_argument("--include-unproven", action="store_true",
                    help="also grade races with no scheduled post time (their 'before post' status is unproven)")
    ap.add_argument("--reference", default="morning_line")
    ap.add_argument("--candidate", default="model_board")
    ap.add_argument("--engine-version", help="'legacy', or a version from --list-versions (default: the newest stamped one)")
    ap.add_argument("--list-versions", action="store_true")
    ap.add_argument("--min-races", type=int, default=0)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"no database at {args.db}")
        return 1
    conn = sqlite3.connect(f"file:{args.db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        graded, excluded = load_graded_races(
            conn, include_unproven=args.include_unproven, since=args.since, until=args.until, track=args.track)
    finally:
        conn.close()
    counts = Counter(g.engine_version for g in graded)
    newest: dict[str, str] = {}
    for g in graded:
        newest[g.engine_version] = max(newest.get(g.engine_version, ""), g.run_timestamp)
    if args.list_versions:
        for v, n in sorted(counts.items(), key=lambda kv: newest[kv[0]]):
            print(f"{v}: {n} graded race(s), last scored {newest[v]}")
        if not counts:
            print("no graded races")
        return 0
    stamped = [v for v in counts if v != "legacy"]
    note = ""
    if args.engine_version:
        version = args.engine_version
    elif stamped:
        version = max(stamped, key=lambda v: newest[v])
    else:
        version = "legacy"
        note = "No score run carries an engine version yet, so the legacy races are shown. "
    others = {v: n for v, n in counts.items() if v != version}
    graded = [g for g in graded if g.engine_version == version]
    if others:
        excluded = Counter(excluded)
        excluded["OTHER_ENGINE_VERSION"] = sum(others.values())
    result = evaluate(graded, excluded, reference=args.reference, candidate=args.candidate, n_boot=args.boot, seed=args.seed)
    result["engine_version"] = version
    header = (f"**Engine version: `{version}`**" + (" (scored before versions were stamped; the engine behind these runs is unknown)"
              if version == "legacy" else "") + ". " + note
              + ("Other versions are not pooled: " + ", ".join(f"{v}={n}" for v, n in sorted(others.items())) + "." if others else ""))
    out = args.out_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    report = header + "\n\n" + render_markdown(result)
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(result, indent=2, default=_json_default), encoding="utf-8")
    rows = per_race_rows(graded)
    with (out / "per_race.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["race"])
        w.writeheader()
        w.writerows(rows)
    print(report)
    print(f"\nwritten to {out}")
    if len(graded) < args.min_races:
        print(f"FAIL: {len(graded)} graded race(s) < --min-races {args.min_races}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
