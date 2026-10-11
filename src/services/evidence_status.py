"""How much evidence exists, and when it will be enough.

Counts the races scored before post, the races graded against official results, and the settled paper bets, against
the thresholds below which the evaluation reports refuse a verdict.  The pace is the count divided by the days since the
first event of that kind up to today (idle days count), and the projection is today plus the remaining count at that
pace; it says "pace not established" until there are at least ``MIN_DAYS_FOR_PACE`` days and ``MIN_EVENTS_FOR_PACE`` events.
A race counts as scored or graded only if the run used for grading (the last one before post) has a real win
probability for every active runner; races where the engine collapsed to the morning line (NULL ``win_probability``)
are reported separately, with the stored ``model_collapse_status``.
Counts are for ONE engine version, the newest stamped one (``score_runs.engine_version``); races scored by other
versions, and ``legacy`` runs from before the stamp, are listed separately and never added in.  Until a stamped run
exists the legacy cohort is shown.  Read-only.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from src.analysis.forecast_metrics import MIN_RACES_FOR_VERDICT
from src.services.paper_trading import MIN_BETS_FOR_CLV_VERDICT, MIN_BETS_FOR_VERDICT
from src.services.walk_forward_eval import DIAGNOSTIC_SEED, _ts, forecast_class, load_graded_races
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


NO_STATUS = "(none stored)"


def _has_model_probabilities(conn: sqlite3.Connection, run_id: str) -> bool:
    """True when every active runner of the run has a real win probability (the test ``model_board`` grading applies)."""
    rows = [r[0] for r in conn.execute(
        """SELECT s.win_probability FROM entry_scores s LEFT JOIN entries e ON e.entry_id=s.entry_id
           WHERE s.run_id=? AND COALESCE(e.scratch_flag, 0)=0""", (run_id,))]
    return bool(rows) and all(v is not None and v == v and v >= 0 for v in rows) and sum(rows) > 0


def _scored_races(conn: sqlite3.Connection, version: str, stamped: bool) -> tuple[list[date], int, Counter, Counter]:
    """Races whose last pre-post run (the run grading would use) is of ``version``.

    Returns (days of races with real model probabilities, how many scored races collapsed to the morning line, the
    stored ``model_collapse_status`` of every scored race in the cohort, the forecast class of the races with
    real probabilities).
    """
    stamp = "COALESCE(engine_version, 'legacy')" if stamped else "'legacy'"
    has_status = "model_collapse_status" in {r[1] for r in conn.execute("PRAGMA table_info(score_runs)")}
    status_sql = "model_collapse_status" if has_status else "NULL"
    type_sql = "model_type" if "model_type" in {r[1] for r in conn.execute("PRAGMA table_info(score_runs)")} else "NULL"
    classes: Counter = Counter()
    days: list[date] = []
    collapsed = 0
    statuses: Counter = Counter()
    for card_id, card_date, post in conn.execute(
            "SELECT card_id, card_date, scheduled_post_time_utc FROM race_cards WHERE scheduled_post_time_utc IS NOT NULL"):
        post_dt = _ts(post)
        runs = [r for r in conn.execute(
            f"SELECT run_id, run_timestamp, {stamp}, {status_sql}, {type_sql} FROM score_runs WHERE card_id=? "
            "ORDER BY run_timestamp, run_id",
            (card_id,)) if post_dt is not None and (_ts(r[1]) or post_dt) < post_dt]
        if not runs or runs[-1][2] != version:
            continue
        run_id, _, _, collapse_status, model_type = runs[-1]
        statuses[collapse_status or NO_STATUS] += 1
        if _has_model_probabilities(conn, run_id):
            if (d := _day(card_date)):
                days.append(d)
            classes[forecast_class(model_type)] += 1
        else:
            collapsed += 1
    return days, collapsed, statuses, classes


def build_status(conn: sqlite3.Connection, *, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    version, latest = _cohorts(conn)
    stamped = "engine_version" in {r[1] for r in conn.execute("PRAGMA table_info(score_runs)")}
    ver_sql = "COALESCE(s.engine_version, 'legacy')" if stamped else "'legacy'"
    scored, scored_collapsed, collapse_statuses, scored_classes = _scored_races(conn, version, stamped)
    graded_all, excluded = load_graded_races(conn)
    excluded = Counter(excluded)
    no_probs = [g for g in graded_all if "model_board" not in g.race.forecasts]
    if no_probs:
        excluded["NO_MODEL_PROBABILITIES"] += len(no_probs)       # graded-eligible, but the run had no real win probabilities
    graded_all = [g for g in graded_all if "model_board" in g.race.forecasts]
    by_version: dict[str, int] = {}
    for g in graded_all:
        by_version[g.engine_version] = by_version.get(g.engine_version, 0) + 1
    graded = [g for g in graded_all if g.engine_version == version]
    graded_days = [g.race.when for g in graded]
    graded_classes = Counter(g.forecast_class for g in graded)
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
                "scored with a proven pre-post run AND real model win probabilities; the raw material for grading"),
        _metric("Races graded against official results", MIN_RACES_FOR_VERDICT, graded_days, today,
                "scored before post AND with a result; below this the walk-forward report gives no verdict"),
        _metric("Settled paper bets (return verdict)", MIN_BETS_FOR_VERDICT, bets, today,
                f"{open_bets} more open, waiting for a chart"),
        _metric("Settled paper bets with closing-line value", MIN_BETS_FOR_CLV_VERDICT, clv, today,
                "the earliest and least noisy edge signal"),
    ]
    return {"today": today.isoformat(), "engine_version": version, "graded_by_version": by_version,
            "last_scored_by_version": latest, "metrics": metrics,
            "excluded_from_grading": dict(excluded), "open_bets": open_bets,
            "scored_collapsed_to_morning_line": scored_collapsed, "model_collapse_status": dict(collapse_statuses),
            "forecast_class": {"scored": dict(scored_classes), "graded": dict(graded_classes)}}


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
    L += ["", f"Scored races that collapsed to the morning line (no model probabilities, not counted above): "
              f"{status['scored_collapsed_to_morning_line']}"]
    if status["model_collapse_status"]:
        L.append("Stored model_collapse_status of the scored runs: " +
                 ", ".join(f"{k}={n}" for k, n in sorted(status["model_collapse_status"].items())))
    fc = status.get("forecast_class", {})
    if fc.get("scored") or fc.get("graded"):
        L += ["", "Forecast class of the counted races (scored / graded): " + ", ".join(
            f"{k}={fc['scored'].get(k, 0)}/{fc['graded'].get(k, 0)}" for k in sorted(set(fc["scored"]) | set(fc["graded"])))]
        if fc["scored"].get(DIAGNOSTIC_SEED) or fc["graded"].get(DIAGNOSTIC_SEED):
            L.append("  DIAGNOSTIC_SEED_BASELINE = uncalibrated seed forecasts that the app's eligibility gate withholds for "
                     "display and betting; they are counted here as evidence only.")
    if status["excluded_from_grading"]:
        L += ["", "Scored races left out of grading: " + ", ".join(f"{k}={v}" for k, v in sorted(status["excluded_from_grading"].items()))]
    L += ["", "A threshold being reached lets the report issue a verdict; it does not mean the verdict will be good."]
    return "\n".join(L)
