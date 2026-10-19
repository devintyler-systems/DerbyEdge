"""Headless Race Board: run one race bundle through the app's import, scoring and eligibility gate and print the board.

For runs away from the user's PC (a Claude session with the uploaded bundle).  It uses the SAME code as the app:
``dispatch_primary_race_card_import`` (validation), ``persist_validated_draftkings_markdown`` + ``persist_race_bundle_extras``
(storage), ``scripts/build_features.py`` + ``scripts/score.py`` (scoring), ``contain_ineligible_board`` (the fail-closed gate)
and ``bet_picks`` (pre-threshold picks).  Only the board query below is a copy of ``load_board`` in ``src/app/app.py``; if the
app's board query changes, change it here too.

Everything runs against a THROWAWAY database (``DERBYEDGE_DB``); it refuses to touch ``db/derbyedge.db``.  Consequences, printed
under every board: nothing is saved to the real database, so the run is not evidence (it never reaches ``training.status``,
walk-forward or the stored picks), and there is no track-record line.

    python scripts/race_board_cli.py path/to/MNR_Full_Race_Data_R7_10-15-26.md [--captured-at 2026-10-15T20:00:00+00:00]

Exit codes: 0 board printed, 2 import blocked (the named reasons are printed), 3 not score-eligible, 4 feature build or scoring failed.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REAL_DB = (ROOT / "db" / "derbyedge.db").resolve()


def _table(headers: list[str], rows: list[list[str]]) -> str:
    cells = [headers] + rows
    widths = [max(len(str(r[i])) for r in cells) for i in range(len(headers))]
    line = lambda r: "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)) + " |"
    return "\n".join([line(headers), "|" + "|".join("-" * (w + 2) for w in widths) + "|"] + [line(r) for r in rows])


def _ml(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{int(round(v))}-1" if abs(v - round(v)) < 1e-9 else f"{v:g}-1"


def _pct(value, digits: int = 1) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _step(script: str, card_id: int, env: dict) -> tuple[bool, str]:
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--card-id", str(card_id)],
                            capture_output=True, text=True, cwd=str(ROOT), timeout=180, env=env)
    return result.returncode == 0, (result.stdout + ("\n" + result.stderr if result.stderr.strip() else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("bundle", type=Path, help="race bundle .md (DK Advanced + DK Basic + TwinSpires summary [+ tabs, RACE STATS])")
    ap.add_argument("--captured-at", help="ISO-8601 timestamp with timezone; default now (UTC). The capture time is the as-of.")
    ap.add_argument("--db", type=Path, help="scratch database file (default: a new temporary one); never db/derbyedge.db")
    ap.add_argument("--name", help="filename to use for the race date/track/number (default: the bundle's own name)")
    args = ap.parse_args(argv)

    if args.db is not None and args.db.resolve() == REAL_DB:
        print("REFUSED: this tool never touches db/derbyedge.db (use another --db path).")
        return 2
    tmp = None
    if args.db is None:
        tmp = tempfile.TemporaryDirectory(prefix="race_board_")
        db_path = Path(tmp.name) / "scratch.db"
    else:
        db_path = args.db.resolve()
    os.environ["DERBYEDGE_DB"] = str(db_path)           # before any src import: the pipeline subprocesses inherit it
    env = dict(os.environ)

    import pandas as pd
    from src.app.race_import import RaceImportDispatchError, dispatch_primary_race_card_import, markdown_import_summary
    from src.services import bet_picks as bp
    from src.services.no_history import NO_HISTORY_NOTE, no_history_entry_ids
    from src.services.draftkings_markdown_intake import markdown_card_score_readiness, persist_validated_draftkings_markdown
    from src.services.race_bundle_intake import persist_race_bundle_extras
    from src.services.score_delivery import contain_ineligible_board
    from src.models.confidence import HIGH_THRESHOLD, LOW_THRESHOLD
    from src.utils.db import DB_PATH, init_db

    assert Path(DB_PATH).resolve() != REAL_DB, "DERBYEDGE_DB did not take effect"
    captured = datetime.fromisoformat(args.captured_at) if args.captured_at else datetime.now(timezone.utc)
    if captured.tzinfo is None:
        print("--captured-at needs a timezone offset (for example +00:00).")
        return 2
    name = args.name or args.bundle.name

    # 1. validate (no writes)
    try:
        dispatch = dispatch_primary_race_card_import(name, args.bundle.read_bytes(), as_of=captured, captured_at=captured)
    except RaceImportDispatchError as exc:
        print(f"IMPORT BLOCKED: {exc}")
        return 2
    if dispatch.source_kind != "markdown" or dispatch.card is None or dispatch.validation is None:
        print("IMPORT BLOCKED: this tool takes the Markdown race bundle, not a PDF.")
        return 2
    summary = markdown_import_summary(dispatch)
    bundle = dispatch.bundle
    race = dispatch.card.race
    title = f"{summary['track']} R{summary['race_number']} {summary['target_race_date']}"
    print(f"RACE BOARD (headless) — {title} · {summary['surface_and_condition']} · {summary['distance']}")
    if not dispatch.validation.passed:
        print("IMPORT BLOCKED — CARD VALIDATION FAILED (nothing was scored):")
        for err in dispatch.validation.errors:
            print(f"  • {err}")
        return 2
    b = summary.get("bundle", {})
    scratched = b.get("scratched_or_inactive") or []
    print(f"Import: PASS · {summary['parsed_runner_count']} runners · {len(scratched)} scratched"
          + (f" ({'; '.join(scratched)})" if scratched else "")
          + f" · {summary['past_performance_count']} PPs / {summary['workout_count']} workouts")
    if bundle is not None:
        print(f"Sections: {', '.join(b.get('sections_found', []))}"
              f" · RACE STATS {'yes' if b.get('race_stats_present') else 'no'}"
              f" · Speed/Class/Pace tabs: {', '.join(t[3:] for t in b.get('twinspires_tabs_present', [])) or 'none'}")
        if bundle.late_capture:
            print(f"CAPTURE: {captured.isoformat(timespec='seconds')} is AT/AFTER post {bundle.post_utc} ({bundle.post_source}) — "
                  "LATE CAPTURE: scored for reading only, never gradable, no live market/pace features.")
        elif bundle.post_utc:
            print(f"Capture: {captured.isoformat(timespec='seconds')} · post {bundle.post_utc} ({bundle.post_source}) · pre-post")
        else:
            print(f"Capture: {captured.isoformat(timespec='seconds')} · post time unavailable ({bundle.post_source})")
    for w in dispatch.validation.warnings:
        if not w.startswith("LATE_CAPTURE"):
            print(f"Warning: {w}")

    # 2. persist (same calls as the app, into the scratch database)
    with contextlib.redirect_stdout(io.StringIO()):          # init_db prints a status line
        init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    persisted = persist_validated_draftkings_markdown(
        conn, dispatch.card, dispatch.validation, source_filename=name,
        scheduled_post_utc=bundle.post_utc if bundle else None, captured_at=bundle.captured_at if bundle else None)
    card_id = persisted.card_id
    if bundle is not None:
        extras = persist_race_bundle_extras(conn, bundle, card_id)
        print(f"Bundle extras: TwinSpires {extras.twinspires_status} · tabs {extras.ts_tab_status or 'none'} · "
              f"market snapshot {extras.market_snapshot_status}")
        for w in extras.warnings:
            print(f"Warning: {w}")

    # 3. build + score
    ready = markdown_card_score_readiness(conn, card_id)
    if not ready.score_eligible:
        print("NOT SCORED — the card is not score-eligible:")
        for blocker in ready.blockers:
            print(f"  • {blocker.message}")
        return 3
    for script in ("build_features.py", "score.py"):
        ok, out = _step(script, card_id, env)
        if not ok:
            print(f"{script} FAILED (nothing to show). Last output:\n" + "\n".join(out.strip().splitlines()[-15:]))
            return 4

    # 4. the board, through the app's gate
    run = conn.execute("SELECT run_id, model_type, run_timestamp FROM score_runs WHERE card_id=? "
                       "ORDER BY run_timestamp DESC, run_id DESC LIMIT 1", (card_id,)).fetchone()
    df = pd.read_sql(
        """SELECT es.rank, es.horse_name, es.post_position, es.entry_id, es.morning_line_odds,
                  es.win_probability, es.place_probability, es.show_probability, es.fair_odds, es.value_score, es.bet_tag,
                  es.pace_fit_score, es.market_implied_prob, es.p_ml_implied, es.confidence_flag, es.confidence_score,
                  es.missing_data_flag
           FROM entry_scores es JOIN score_runs sr ON sr.run_id = es.run_id
           WHERE es.run_id = ? AND sr.card_id = ? ORDER BY es.rank""", conn, params=(run["run_id"], card_id))
    board, eligibility = contain_ineligible_board(df, Path(DB_PATH), card_id)
    reasons = sorted({r for audit in eligibility.values() for r in audit.get("reason_codes", [])})
    feats = pd.read_sql("SELECT * FROM feature_store WHERE card_id=? ORDER BY post_position", conn, params=(card_id,))
    print(f"\nScoring: run {run['run_id']} · model type {run['model_type']} · {run['run_timestamp']}")
    if not feats.empty and "pace_state" in feats.columns:
        f0 = feats.iloc[0]
        state = str(f0.get("pace_state"))
        print(f"Pace: {state.replace('PACE_', '')} — {f0.get('classified_runner_count')}/{f0.get('active_runner_count')} classified"
              + (f" — {f0.get('pace_band')} pressure" if state == "PACE_READY" else
                 "; unknown runners have no pace fit" if state == "PACE_PARTIAL" else "; pace excluded from the forecast"))
    no_history = no_history_entry_ids(feats)
    pp_counts = ({int(e): (None if pd.isna(c) else int(c)) for e, c in zip(feats["entry_id"], feats["dk_history_start_count"])}
                 if not feats.empty and "dk_history_start_count" in feats.columns else {})
    valid_win = pd.to_numeric(board.get("win_probability"), errors="coerce").notna().any()
    print("\n" + ("FORECAST" if valid_win else "DIAGNOSTIC FORECAST — NOT VALID FOR BETTING")
          + ("" if valid_win else ": the stored model probabilities of an uncalibrated seed baseline. The eligibility gate withholds "
             "Win %, fair odds, edge and bet tags until the scoring context passes it."))
    if reasons:
        print("Gate reasons: " + " · ".join(reasons))
    rows = []
    for _, r in board.iterrows():
        diag = r.get("diagnostic_win_probability")
        ml_imp = r.get("p_ml_implied")
        try:
            diff = f"{(float(diag) - float(ml_imp)) * 100:+.1f}"
        except (TypeError, ValueError):
            diff = "—"
        score = r.get("confidence_score")
        conf = "—" if pd.isna(score) else (
            f"{'LOW' if float(score) < LOW_THRESHOLD else 'MEDIUM' if float(score) < HIGH_THRESHOLD else 'HIGH'} ({float(score):.2f})")
        n_pp = pp_counts.get(int(r["entry_id"]))
        rows.append([int(r["rank"]), r["horse_name"], int(r["post_position"]), _ml(r.get("morning_line_odds")),
                     _pct(diag), _pct(ml_imp), diff, conf,
                     "NONE" if int(r["entry_id"]) in no_history else ("—" if n_pp is None else n_pp)])
    print(_table(["Rank", "Horse", "Post", "ML", "Diagnostic Win %", "ML-implied %", "Diff (pts)", "Confidence", "PPs"], rows))
    nh_names = [r[1] for r in rows if r[8] == "NONE"]
    if nh_names:
        print(f"NO PP HISTORY: {', '.join(nh_names)}. {NO_HISTORY_NOTE}.")
    low = [r[1] for r in rows if str(r[7]).startswith("LOW")]
    if low:
        print(f"Low confidence (score < {LOW_THRESHOLD}): {len(low)} of {len(rows)} entries")

    # 5. pre-threshold picks (same functions as the Race Board)
    print(f"\nBet-type picks — {bp.LABEL.upper()}")
    print("Based on an uncalibrated diagnostic forecast the eligibility gate withholds. Fewer than 30 graded picks exist per bet "
          "type, so none of this is proven. Model probability, break-even payout per $2 and the morning-line price are shown side "
          "by side for your own judgment; each is unvalidated. No stake is computed, and none of this is a recommended wager.")
    allowed = bp.picks_allowed(board, run["model_type"])
    runners = bp.runners_from_board(board, no_history) if allowed else []
    picks = bp.recommend_picks(runners)
    source = bp.forecast_source(runners)
    if not allowed:
        print(bp.SCOPE_NOTE)
    elif source == bp.ML_BASELINE:
        print(bp.ML_BASELINE_NOTE)
    elif not picks:
        print("No WIN / PLACE / SHOW pick clears the thresholds for this race.")
    else:
        frame = bp.picks_frame(picks)
        print(_table(list(frame.columns), [[str(v) for v in row] for row in frame.itertuples(index=False)]))
        longshots = [p["horse_name"] for p in picks if p.get("longshot_warning")]
        if longshots:
            print(f"{bp.LONGSHOT_NOTE} ({', '.join(longshots)})")
    print(f"Picks version {bp.PICKS_VERSION}: untuned default thresholds; place/show probabilities are Harville estimates; the market "
          "side is the morning line (no live place/show pool captured).")

    print("\nNOT SAVED: this ran against a throwaway database, not the user's derbyedge.db. It is not evidence (not in training.status, "
          "walk-forward or the stored picks) and carries no track-record line; a later upload of the same file at home would be a "
          "late capture.")
    if tmp is not None:
        conn.close()
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
