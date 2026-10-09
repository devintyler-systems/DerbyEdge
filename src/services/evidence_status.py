"""How much evidence exists, and when it will be enough.

Counts the races scored before post, the races graded against official results, and the settled paper bets, against
the thresholds below which the evaluation reports refuse a verdict.  The pace is the count divided by the days since the
first event of that kind up to today (idle days count), and the projection is today plus the remaining count at that
pace; it says "pace not established" until there are at least ``MIN_DAYS_FOR_PACE`` days and ``MIN_EVENTS_FOR_PACE`` events.
Counts are for ONE engine version, the newest stamped one (``score_runs.engine_version``); races scored by other
versions, and ``legacy`` runs from before the stamp, are listed separately and never added in.  Until a stamped run
exists the legacy cohort is shown.  Read-only.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from src.analysis.forecast_metrics import MIN_RACES_FOR_VERDICT
from src.services.paper_trading import MIN_BETS_FOR_CLV_VERDICT, MIN_BETS_FOR_VERDICT
from src.services.walk_forward_eval import load_graded_races
from src.utils.engine_version import LEGACY

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


def _cohorts(conn: sqlite3.Connection) -> tuple[str, dict[str, str]]:
    """The cohort to report on (newest stamped, else legacy) and each version's latest run time."""
    if "engine_version" not in {r[1] for r in conn.execute("PRAGMA table_info(score_runs)")}:
        return LEGACY, {}
    latest = {v: t for v, t in conn.execute(
        "SELECT engine_version, MAX(run_timestamp) FROM score_runs WHERE engine_version IS NOT NULL GROUP BY engine_version")}
    return (max(latest, key=lambda v: latest[v]) if latest else LEGACY), latest


def build_status(conn: sqlite3.Connection, *, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    version, latest = _cohorts(conn)
    stamped = "engine_version" in {r[1] for r in conn.execute("PRAGMA table_info(score_runs)")}
    ver_sql = "COALESCE(s.engine_version, 'legacy')" if stamped else "'legacy'"
    scored = [d for d in (_day(r[0]) for r in conn.execute(
        f"""SELECT rc.card_date FROM race_cards rc WHERE rc.scheduled_post_time_utc IS NOT NULL AND EXISTS (
              SELECT 1 FROM score_runs s WHERE s.card_id=rc.card_id AND s.run_timestamp < rc.scheduled_post_time_utc
              AND {ver_sql} = ?)""", (version,))) if d]
    graded_all, excluded = load_graded_races(conn)
    by_version: dict[str, int] = {}
    for g in graded_all:
        by_version[g.engine_version] = by_version.get(g.engine_version, 0) + 1
    graded = [g for g in graded_all if g.engine_version == version]
    graded_days = [g.race.when for g in graded]
    bets: list[date] = []
    clv: list[date] = []
    open_bets = 0
    try:
        for status, decided, final_clv in conn.execute(
                f"""SELECT b.status, b.decision_time, b.clv FROM paper_bets b LEFT JOIN score_runs s ON s.run_id=b.run_id
                    WHERE {ver_sql} = ?""", (version,)):
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
    return {"today": today.isoformat(), "engine_version": version, "graded_by_version": by_version,
            "last_scored_by_version": latest, "metrics": metrics,
            "excluded_from_grading": dict(excluded), "open_bets": open_bets}


def render_text(status: dict) -> str:
    v = status["engine_version"]
    L = [f"Evidence status as of {status['today']}", "",
         f"Engine version counted: {v}" + (" (no stamped score run yet: these are the pre-stamp runs)" if v == "legacy" else
                                          " (the newest; other versions are never added in)")]
    others = {k: n for k, n in status["graded_by_version"].items() if k != v}
    if others:
        L.append("Graded races under other versions (not counted): " + ", ".join(f"{k}={n}" for k, n in sorted(others.items())))
    L.append("")
    for m in status["metrics"]:
        L.append(f"  {m['label']}: {m['have']} / {m['need']}  -> {m['status']}")
        if m["note"]:
            L.append(f"      {m['note']}")
    if status["excluded_from_grading"]:
        L += ["", "Scored races left out of grading: " + ", ".join(f"{k}={v}" for k, v in sorted(status["excluded_from_grading"].items()))]
    L += ["", "A threshold being reached lets the report issue a verdict; it does not mean the verdict will be good."]
    return "\n".join(L)
