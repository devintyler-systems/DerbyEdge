"""How much evidence exists, and when it will be enough.

Counts the races scored before post, the races graded against official results, and the settled paper bets, against
the thresholds below which the evaluation reports refuse a verdict.  The pace is the count divided by the days since the
first event of that kind up to today (idle days count), and the projection is today plus the remaining count at that
pace; it says "pace not established" until there are at least ``MIN_DAYS_FOR_PACE`` days and ``MIN_EVENTS_FOR_PACE`` events.
Read-only.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from src.analysis.forecast_metrics import MIN_RACES_FOR_VERDICT
from src.services.paper_trading import MIN_BETS_FOR_CLV_VERDICT, MIN_BETS_FOR_VERDICT
from src.services.walk_forward_eval import load_graded_races

MIN_DAYS_FOR_PACE = 14
MIN_EVENTS_FOR_PACE = 5


def _day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _metric(label: str, need: int, days: list[date], today: date, note: str = "") -> dict:
    have = len(days)
    out = {"label": label, "have": have, "need": need, "reached": have >= need, "note": note,
           "pace_per_week": None, "projected_date": None, "status": ""}
    if have >= need:
        out["status"] = "REACHED"
        return out
    if not days:
        out["status"] = "NO DATA YET"
        return out
    span = (today - min(days)).days
    if span < MIN_DAYS_FOR_PACE or have < MIN_EVENTS_FOR_PACE:
        out["status"] = f"pace not established (needs {MIN_DAYS_FOR_PACE}+ days and {MIN_EVENTS_FOR_PACE}+ events)"
        return out
    per_day = have / span
    out["pace_per_week"] = round(per_day * 7, 1)
    out["projected_date"] = (today + timedelta(days=int(-(-(need - have) // per_day)))).isoformat()
    out["status"] = f"{need - have} to go, projected {out['projected_date']} at {out['pace_per_week']}/week"
    return out


def build_status(conn: sqlite3.Connection, *, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    scored = [d for (d,) in (
        (_day(r[0]),) for r in conn.execute(
            """SELECT rc.card_date FROM race_cards rc WHERE rc.scheduled_post_time_utc IS NOT NULL AND EXISTS (
                 SELECT 1 FROM score_runs s WHERE s.card_id=rc.card_id AND s.run_timestamp < rc.scheduled_post_time_utc)"""))
        if d]
    graded, excluded = load_graded_races(conn)
    graded_days = [g.race.when for g in graded]
    bets: list[date] = []
    clv: list[date] = []
    open_bets = 0
    try:
        for status, decided, final_clv in conn.execute("SELECT status, decision_time, clv FROM paper_bets"):
            if status == "OPEN":
                open_bets += 1
            elif status in ("WON", "LOST"):
                d = _day(decided)
                if d:
                    bets.append(d)
                    if final_clv is not None:
                        clv.append(d)
    except sqlite3.OperationalError:
        pass                                                     # no paper_bets table yet
    metrics = [
        _metric("Races scored before post", MIN_RACES_FOR_VERDICT, scored, today,
                "scored with a proven pre-post run; the raw material for grading"),
        _metric("Races graded against official results", MIN_RACES_FOR_VERDICT, graded_days, today,
                "scored before post AND with a result; below this the walk-forward report gives no verdict"),
        _metric("Settled paper bets (return verdict)", MIN_BETS_FOR_VERDICT, bets, today,
                f"{open_bets} more open, waiting for a chart"),
        _metric("Settled paper bets with closing-line value", MIN_BETS_FOR_CLV_VERDICT, clv, today,
                "the earliest and least noisy edge signal"),
    ]
    return {"today": today.isoformat(), "metrics": metrics, "excluded_from_grading": dict(excluded), "open_bets": open_bets}


def render_text(status: dict) -> str:
    L = [f"Evidence status as of {status['today']}", ""]
    for m in status["metrics"]:
        L.append(f"  {m['label']}: {m['have']} / {m['need']}  -> {m['status']}")
        if m["note"]:
            L.append(f"      {m['note']}")
    if status["excluded_from_grading"]:
        L += ["", "Scored races left out of grading: " + ", ".join(f"{k}={v}" for k, v in sorted(status["excluded_from_grading"].items()))]
    L += ["", "A threshold being reached lets the report issue a verdict; it does not mean the verdict will be good."]
    return "\n".join(L)
