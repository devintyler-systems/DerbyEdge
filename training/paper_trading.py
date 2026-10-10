"""Paper trading: record what the engine would have bet, settle it on the chart payoffs, report.

    python -m training.paper_trading place                  # live: races whose cut-off (post - 2 min) is still ahead
    python -m training.paper_trading place --backfill       # replay: every race, decided at its last pre-cutoff run + price
    python -m training.paper_trading place --dry-run        # show the bets, write nothing
    python -m training.paper_trading settle                 # settle open bets from the stored Equibase charts
    python -m training.paper_trading report                 # one report per policy -> output/paper_trading/

Policy flags (place / report select the policy by its parameters; ``--policy-id`` selects a stored one for report):
    --min-edge 0.025 --min-ev 0.05 --max-bets-per-race 1 --staking flat|kelly --probability-source model_board|model_pre_market
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.paper_trading import (  # noqa: E402
    PaperPolicy, build_report, decide_bets, ensure_paper_tables, place_paper_bets, render_markdown, settle_paper_bets,
)
from src.utils.db import DB_PATH  # noqa: E402


def _policy(args: argparse.Namespace) -> PaperPolicy:
    fields = {"min_edge": args.min_edge, "min_ev": args.min_ev, "max_bets_per_race": args.max_bets_per_race,
              "staking": args.staking, "probability_source": args.probability_source,
              "require_engine_bet_tag": args.require_bet_tag}
    return PaperPolicy(**{k: v for k, v in fields.items() if v is not None and v is not False})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(DB_PATH))
    ap.add_argument("--out", type=Path, default=ROOT / "output" / "paper_trading")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("place", "report"):
        p = sub.add_parser(name)
        p.add_argument("--min-edge", type=float)
        p.add_argument("--min-ev", type=float)
        p.add_argument("--max-bets-per-race", type=int)
        p.add_argument("--staking", choices=["flat", "kelly"])
        p.add_argument("--probability-source", choices=["model_board", "model_pre_market"])
        p.add_argument("--require-bet-tag", action="store_true")
    pl = sub.choices["place"]
    pl.add_argument("--backfill", action="store_true", help="replay history instead of acting live")
    pl.add_argument("--now", help="ISO time with offset to act at (default: the real current time)")
    pl.add_argument("--dry-run", action="store_true")
    pl.add_argument("--since")
    pl.add_argument("--until")
    pl.add_argument("--track")
    sub.choices["report"].add_argument("--policy-id")
    sub.choices["report"].add_argument("--boot", type=int, default=2000)
    sub.add_parser("settle")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"no database at {args.db}")
        return 1
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        if args.cmd == "place":
            policy = _policy(args)
            now = None if args.backfill else (datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc))
            kwargs = {"now": now, "since": args.since, "until": args.until, "track": args.track}
            if args.dry_run:
                conn.execute("SAVEPOINT dry")
                ensure_paper_tables(conn)
                rep = decide_bets(conn, policy, **kwargs)
                conn.execute("ROLLBACK TO dry")
            else:
                rep = place_paper_bets(conn, policy, **kwargs)
            print(f"policy {policy.name} {policy.policy_id}: {rep.races_decided} race(s) decided, {len(rep.bets)} bet(s)"
                  f"{' (dry run, nothing written)' if args.dry_run else ''}")
            for b in rep.bets:
                print(f"  {b.race_key} #{b.program} {b.horse_name}: p={b.model_p:.3f} market={b.market_p:.3f} "
                      f"price={b.captured_decimal:.2f} edge={b.edge:+.3f} ev={b.ev:+.3f} stake=${b.stake:.2f} "
                      f"at {b.decision_time} (price from {b.capture_provider} {b.capture_time})")
            if rep.skipped:
                print("  skipped: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.skipped.items())))
            return 0
        if args.cmd == "settle":
            counts = settle_paper_bets(conn)
            print("settled: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"))
            return 0
        ensure_paper_tables(conn)
        if args.policy_id:
            ids = [args.policy_id]
        elif any(v is not None and v is not False for v in (args.min_edge, args.min_ev, args.max_bets_per_race, args.staking,
                                                              args.probability_source, args.require_bet_tag)):
            ids = [_policy(args).policy_id]
        else:
            ids = [r[0] for r in conn.execute("SELECT policy_id FROM paper_policies ORDER BY created_at")]
        if not ids:
            print("no paper policies recorded yet; run `place` first")
            return 1
        args.out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for pid in ids:
            rep = build_report(conn, pid, n_boot=args.boot)
            text = render_markdown(rep)
            (args.out / f"{stamp}_{pid}.md").write_text(text, encoding="utf-8")
            (args.out / f"{stamp}_{pid}.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
            print(text)
        print(f"written to {args.out}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
