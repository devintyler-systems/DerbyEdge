"""Pre-threshold bet-type picks (WIN / PLACE / SHOW and combinations) for one scored race.

A pick is a model-vs-morning-line comparison per bet type, not a validated edge:
* place and show probabilities come from the win probabilities by the Harville model (place sums to 2, show to 3);
  Harville overrates favourites and underrates longshots at place/show, so it understates the horses it will most
  often recommend (a conservative bias, but unverified);
* the market side is the same Harville transform of the morning-line probabilities, because no live place/show pool
  is captured; the pick is therefore "the model likes this horse for SHOW more than the morning line implies";
* each pick carries a break-even payout per $2 (2 / probability): the lowest posted price at which the bet is worth
  making on the model's own numbers.  The board shows model probability, break-even payout and the morning-line price side
  by side for the owner's own judgment, each unvalidated; the app computes no stake and calls no pick a recommended wager.
Every pick is labelled PRE-THRESHOLD: the inputs are the uncalibrated seed baseline and fewer than the documented
graded-bet count exist.  Picks are stored before post (``bet_picks``) so grading can use only what was shown; races
with no stored picks can be re-derived for review but are marked RETROSPECTIVE and never counted.  Thresholds below
are untuned defaults; changing them changes ``PICKS_VERSION`` so cohorts are never pooled.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

PICKS_VERSION = "pre_threshold_picks_v1"
BET_TYPES = ("WIN", "PLACE", "SHOW")
MIN_WIN_PROB, MIN_WIN_LEAD = 0.08, 0.04
MIN_PLACE_PROB, MIN_PLACE_LEAD = 0.30, 0.05
MIN_SHOW_PROB, MIN_SHOW_LEAD = 0.35, 0.05
MAX_PICKS = 3
LONGSHOT_RATIO = 2.0
MIN_PICKS_FOR_RECORD = 30
BASE_STAKE = 2.0
LABEL = "PRE-THRESHOLD: not validated evidence"
ENGINE_FORECAST = "ENGINE_FORECAST"
ML_BASELINE = "MORNING_LINE_BASELINE"
ML_BASELINE_NOTE = ("MORNING-LINE BASELINE: the engine produced no independent forecast for this race (it collapsed to the "
                    "morning line), so there is no model-vs-market comparison and no pick is made.")

DDL = """
CREATE TABLE IF NOT EXISTS bet_picks (
  pick_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id         TEXT NOT NULL,
  card_id        INTEGER NOT NULL,
  entry_id       INTEGER NOT NULL,
  horse_name     TEXT NOT NULL,
  bet_type       TEXT NOT NULL CHECK(bet_type IN ('WIN','PLACE','SHOW')),
  model_prob     REAL NOT NULL,
  market_prob    REAL NOT NULL,
  breakeven_payout REAL NOT NULL,
  picks_version  TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  source         TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  UNIQUE(run_id, entry_id, bet_type, picks_version)
);
"""


def harville_place_show(win: list[float]) -> tuple[list[float], list[float]]:
    """P(finish top 2) and P(finish top 3) per runner from win probabilities (renormalised to sum 1)."""
    total = sum(win)
    if total <= 0 or len(win) < 2:
        raise ValueError("win probabilities must be positive with at least two runners")
    p = [w / total for w in win]
    n = len(p)
    place, show = [], []
    for i in range(n):
        pl = p[i]
        sh = p[i]
        for j in range(n):
            if j == i:
                continue
            d1 = 1.0 - p[j]
            pl += p[j] * p[i] / d1
            sh += p[j] * p[i] / d1
            for k in range(n):
                if k in (i, j):
                    continue
                d2 = 1.0 - p[j] - p[k]
                if d2 > 1e-12:
                    sh += p[j] * p[k] / d1 * p[i] / d2
        place.append(min(pl, 1.0))
        show.append(min(sh, 1.0))
    if n == 2:
        show = [1.0, 1.0]
    return place, show


def _finite(value: object) -> float | None:
    try:
        v = float(value)                                  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


SCOPE_NOTE = ("Bet-type picks are limited to the uncalibrated seed-baseline forecast that the eligibility gate withholds; "
              "this race's forecast is outside that scope, so none is shown.")


def picks_allowed(board, model_type: str | None) -> bool:
    """True only for an uncalibrated seed-baseline run whose Win % the gate blanked but whose stored probability exists."""
    from src.services.forecast_class import DIAGNOSTIC_SEED, forecast_class
    if board is None or len(board) == 0 or "diagnostic_win_probability" not in board.columns:
        return False
    import pandas as pd
    gated = pd.to_numeric(board.get("win_probability"), errors="coerce")
    diag = pd.to_numeric(board["diagnostic_win_probability"], errors="coerce")
    return forecast_class(model_type) == DIAGNOSTIC_SEED and not bool(gated.notna().any()) and bool(diag.notna().any())


def forecast_source(runners: list[dict]) -> str:
    """ENGINE_FORECAST when runners carry model probabilities that differ from the morning line, else ML_BASELINE."""
    pairs = [(_finite(r.get("model_win")), _finite(r.get("market_win"))) for r in runners]
    pairs = [(m, k) for m, k in pairs if m is not None and k is not None and m > 0 and k > 0]
    if len(pairs) < 2:
        return ML_BASELINE
    ms, ks = sum(m for m, _ in pairs), sum(k for _, k in pairs)
    return ML_BASELINE if max(abs(m / ms - k / ks) for m, k in pairs) < 1e-6 else ENGINE_FORECAST


def recommend_picks(runners: list[dict]) -> list[dict]:
    """Typed picks for one race.

    ``runners``: dicts with ``entry_id``, ``horse_name``, ``post_position``, ``model_win`` (diagnostic or valid win
    probability) and ``market_win`` (morning-line implied probability).  Runners without both numbers are skipped;
    fewer than 5 usable runners yields no picks (place/show are not meaningful in a tiny field).
    """
    rows = [r for r in runners if _finite(r.get("model_win")) is not None and (_finite(r.get("market_win")) or 0) > 0
            and _finite(r.get("model_win")) > 0]
    if len(rows) < 5 or forecast_source(rows) == ML_BASELINE:
        return []                                  # a morning-line baseline cannot disagree with the morning line
    mw = [float(r["model_win"]) for r in rows]
    kw = [float(r["market_win"]) for r in rows]
    msum, ksum = sum(mw), sum(kw)
    mw, kw = [v / msum for v in mw], [v / ksum for v in kw]
    mp, ms = harville_place_show(mw)
    kp, ks = harville_place_show(kw)
    picks = []
    for i, r in enumerate(rows):
        probs = {"WIN": (mw[i], kw[i], MIN_WIN_PROB, MIN_WIN_LEAD),
                 "PLACE": (mp[i], kp[i], MIN_PLACE_PROB, MIN_PLACE_LEAD),
                 "SHOW": (ms[i], ks[i], MIN_SHOW_PROB, MIN_SHOW_LEAD)}
        types = [t for t in BET_TYPES if probs[t][0] >= probs[t][2] and probs[t][0] - probs[t][1] >= probs[t][3]]
        if not types:
            continue
        best_lead = max(probs[t][0] - probs[t][1] for t in types)
        longshot = any(probs[t][0] > LONGSHOT_RATIO * probs[t][1] for t in types)
        picks.append({
            "entry_id": int(r["entry_id"]), "horse_name": r["horse_name"], "post_position": r.get("post_position"),
            "bet_types": types, "label": "+".join(types), "source": ENGINE_FORECAST,
            "morning_line": _finite(r.get("morning_line")), "longshot_warning": longshot, "best_lead": best_lead,
            "no_history": bool(r.get("no_history")),
            "legs": {t: {"model_prob": probs[t][0], "market_prob": probs[t][1],
                         "lead_over_ml": probs[t][0] - probs[t][1], "breakeven_payout": BASE_STAKE / probs[t][0]}
                     for t in types},
        })
    picks.sort(key=lambda p: -p["best_lead"])
    return picks[:MAX_PICKS]


def runners_from_board(board, no_history_ids=()) -> list[dict]:
    """Runner dicts from the Race Board frame: the valid Win % when present, else the stored diagnostic one.

    ``no_history_ids``: entry ids with no past performances; they stay eligible but each pick carries a warning."""
    no_history = {int(i) for i in no_history_ids}
    out = []
    for _, row in board.iterrows():
        win = _finite(row.get("win_probability"))
        if win is None:
            win = _finite(row.get("diagnostic_win_probability"))
        out.append({"entry_id": row["entry_id"], "horse_name": row["horse_name"],
                    "post_position": row.get("post_position"), "model_win": win,
                    "morning_line": _finite(row.get("morning_line_odds")),
                    "market_win": _finite(row.get("market_implied_prob")),
                    "no_history": int(row["entry_id"]) in no_history})
    return out


LONGSHOT_NOTE = (f"LONGSHOT WARNING: the model's probability is more than {LONGSHOT_RATIO:.0f}x the market's. "
                 "Those are the horses where an unvalidated model and the market disagree most.")


def picks_frame(picks: list[dict]):
    """The Race Board table: horse, morning line, bet type(s), model probability beside the morning-line-implied one,
    break-even payout per $2 and any longshot warning.  Each figure is unvalidated; no stake is computed."""
    import pandas as pd
    rows = []
    for p in picks:
        ml = p.get("morning_line")
        rows.append({
            "Horse": p["horse_name"], "Post": p["post_position"],
            "Morning line": f"{ml:.0f}-1" if ml is not None else "—", "Bet": p["label"],
            **{f"{t}: model / ML-implied": (f"{p['legs'][t]['model_prob'] * 100:.0f}% / {p['legs'][t]['market_prob'] * 100:.0f}%"
                                            if t in p["legs"] else "—") for t in BET_TYPES},
            "Break-even payout per $2": " · ".join(f"{t} ≥ ${p['legs'][t]['breakeven_payout']:.2f}" for t in p["bet_types"]),
            "Warning": " · ".join(w for w in (
                "LONGSHOT (model > 2x market)" if p.get("longshot_warning") else "",
                "NO PP HISTORY (default-driven)" if p.get("no_history") else "") if w),
            "Source": "engine forecast (pre-threshold)" if p.get("source") == ENGINE_FORECAST else "morning-line baseline",
        })
    return pd.DataFrame(rows)


def persist_picks(conn: sqlite3.Connection, run_id: str, card_id: int, picks: list[dict]) -> int:
    """Store the picks shown for a run (idempotent); returns rows newly written."""
    conn.executescript(DDL)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(score_runs)")}
    engine = (conn.execute("SELECT COALESCE(engine_version, 'legacy') FROM score_runs WHERE run_id=?", (run_id,)).fetchone()
              if "engine_version" in cols else None)
    engine_version = engine[0] if engine else "legacy"
    written = 0
    for pick in picks:
        for bet_type, leg in pick["legs"].items():
            cur = conn.execute(
                """INSERT OR IGNORE INTO bet_picks (run_id, card_id, entry_id, horse_name, bet_type, model_prob,
                       market_prob, breakeven_payout, picks_version, engine_version, source, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, card_id, pick["entry_id"], pick["horse_name"], bet_type, leg["model_prob"],
                 leg["market_prob"], leg["breakeven_payout"], PICKS_VERSION, engine_version,
                 pick.get("source", ENGINE_FORECAST), now))
            written += cur.rowcount
    conn.commit()
    return written


def persist_picks_pre_post(conn: sqlite3.Connection, run_id: str, card_id: int, picks: list[dict]) -> tuple[int, str]:
    """Store picks only when the run was made before the scheduled post (the as-of rule); returns (rows, reason)."""
    from src.services.walk_forward_eval import _ts
    row = conn.execute(
        "SELECT sr.run_timestamp, rc.scheduled_post_time_utc FROM score_runs sr "
        "JOIN race_cards rc ON rc.card_id = sr.card_id WHERE sr.run_id=? AND rc.card_id=?", (run_id, card_id)).fetchone()
    if not row or _ts(row[1]) is None:
        return 0, "no scheduled post time: picks shown but not recorded"
    if _ts(row[0]) is None or _ts(row[0]) >= _ts(row[1]):
        return 0, "run made at or after post: picks shown for review only, not recorded"
    return persist_picks(conn, run_id, card_id, picks), "recorded"


def stored_picks(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    conn.executescript(DDL)
    return [dict(zip(("entry_id", "horse_name", "bet_type", "model_prob", "market_prob", "breakeven_payout",
                      "picks_version", "engine_version", "source", "created_at"), r)) for r in conn.execute(
        """SELECT entry_id, horse_name, bet_type, model_prob, market_prob, breakeven_payout, picks_version,
                  engine_version, source, created_at
           FROM bet_picks WHERE run_id=? ORDER BY pick_id""", (run_id,))]


_NEEDS = {"WIN": 1, "PLACE": 2, "SHOW": 3}


def settle_legs(legs: list[dict], finish_by_entry: dict[int, int | None]) -> list[dict]:
    """Hit or miss for each stored leg against the official finish (WIN needs 1st, PLACE top 2, SHOW top 3).

    A runner with no official finish (scratched or missing from the results) is VOID, never a loss.  Payoff-based
    ROI needs the chart's place/show payoffs, which the Results Import does not store yet, so none is claimed.
    """
    out = []
    for leg in legs:
        finish = finish_by_entry.get(int(leg["entry_id"]))
        status = "VOID" if not finish else ("HIT" if finish <= _NEEDS[leg["bet_type"]] else "MISS")
        out.append({**leg, "finish": finish, "status": status})
    return out


def summarize(settled: list[dict]) -> dict[str, dict]:
    """Per bet type: legs, hits, hit rate, and the model's average stated probability (a calibration check)."""
    out: dict[str, dict] = {}
    for bet_type in BET_TYPES:
        legs = [s for s in settled if s["bet_type"] == bet_type and s["status"] != "VOID"]
        if legs:
            out[bet_type] = {"legs": len(legs), "hits": sum(s["status"] == "HIT" for s in legs),
                             "hit_rate": sum(s["status"] == "HIT" for s in legs) / len(legs),
                             "mean_model_prob": sum(s["model_prob"] for s in legs) / len(legs)}
    return out


def chart_payoffs_by_entry(conn: sqlite3.Connection, card_id: int) -> dict[int, tuple[float | None, float | None, float | None]]:
    """{entry_id: (win, place, show) $2 payoffs} from the stored Equibase chart for this card's race, or {} if none."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"result_races", "result_starters"} <= tables:
        return {}
    race = conn.execute(
        """SELECT rr.result_race_id FROM race_cards rc JOIN tracks t ON t.track_id = rc.track_id
           JOIN result_races rr ON rr.track_code = t.abbrev AND rr.race_date = rc.card_date
                                AND rr.race_number = rc.race_number AND rr.is_current = 1
           WHERE rc.card_id=?""", (card_id,)).fetchone()
    if not race:
        return {}
    pay = {str(prog).upper(): (w, pl, sh) for prog, w, pl, sh in conn.execute(
        "SELECT program, win_payoff, place_payoff, show_payoff FROM result_starters WHERE result_race_id=?", (race[0],))}
    return {int(eid): pay[str(prog or "").upper()] for eid, prog in conn.execute(
        "SELECT entry_id, program_number FROM entries WHERE card_id=?", (card_id,)) if str(prog or "").upper() in pay}


def _wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = hits / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def track_record(conn: sqlite3.Connection) -> dict:
    """Graded track record of the stored picks, per bet type, for the newest engine version only.

    Hit rate is compared with the model's own mean probability.  Return is at the actual $2 chart payoffs, only over legs
    whose race has a stored chart (otherwise wins would be unknowable while losses are known, biasing the figure), with a
    95% normal-approximation interval on the mean net return per $2.  Below ``MIN_PICKS_FOR_RECORD`` graded legs a
    bet type reports INSUFFICIENT DATA and no rate.
    """
    conn.executescript(DDL)
    versions = conn.execute(
        "SELECT engine_version FROM bet_picks WHERE picks_version=? AND source=? "
        "GROUP BY engine_version ORDER BY MAX(created_at) DESC", (PICKS_VERSION, ENGINE_FORECAST)).fetchall()
    out: dict = {"picks_version": PICKS_VERSION, "engine_version": versions[0][0] if versions else None,
                 "other_engine_versions_not_counted": [v[0] for v in versions[1:]], "by_type": {}}
    if not versions:
        return out
    legs = conn.execute(
        """SELECT card_id, entry_id, bet_type, model_prob FROM bet_picks
           WHERE picks_version=? AND source=? AND engine_version=?""", (PICKS_VERSION, ENGINE_FORECAST, versions[0][0])).fetchall()
    payoff_cache: dict[int, dict] = {}
    graded: dict[str, list[tuple[bool, float, float | None]]] = {t: [] for t in BET_TYPES}
    for card_id, entry_id, bet_type, model_prob in legs:
        row = conn.execute("SELECT COALESCE(official_finish, finish_position), is_scratched FROM race_results "
                           "WHERE card_id=? AND entry_id=?", (card_id, entry_id)).fetchone() \
            if "race_results" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} else None
        if not row or row[1] or not row[0]:
            continue                                                     # not resulted, scratched, or void
        hit = row[0] <= _NEEDS[bet_type]
        payoffs = payoff_cache.setdefault(card_id, chart_payoffs_by_entry(conn, card_id))
        net: float | None = None
        if payoffs:
            paid = payoffs.get(entry_id, (None, None, None))[BET_TYPES.index(bet_type)]
            net = (paid - BASE_STAKE) if hit and paid else (-BASE_STAKE if not hit else None)
        graded[bet_type].append((hit, model_prob, net))
    for bet_type, rows in graded.items():
        n = len(rows)
        if not n:
            continue
        rec: dict = {"graded": n, "hits": sum(h for h, _, _ in rows), "sufficient": n >= MIN_PICKS_FOR_RECORD}
        if rec["sufficient"]:
            rec["hit_rate"] = rec["hits"] / n
            rec["hit_rate_ci"] = _wilson(rec["hits"], n)
            rec["expected_hit_rate"] = sum(m for _, m, _ in rows) / n
            nets = [x for _, _, x in rows if x is not None]
            rec["money_legs"] = len(nets)
            if len(nets) >= MIN_PICKS_FOR_RECORD:
                mean = sum(nets) / len(nets)
                sd = math.sqrt(sum((x - mean) ** 2 for x in nets) / (len(nets) - 1))
                half = 1.96 * sd / math.sqrt(len(nets))
                rec["return_per_2"] = mean
                rec["return_ci"] = (mean - half, mean + half)
        out["by_type"][bet_type] = rec
    return out


def render_track_record(rec: dict) -> list[str]:
    """One line per bet type for the Race Board, always carrying the PRE-THRESHOLD label."""
    if not rec["by_type"]:
        return [f"Track record ({LABEL}): no graded picks yet. INSUFFICIENT DATA (needs {MIN_PICKS_FOR_RECORD} per bet type)."]
    lines = []
    for bet_type, r in rec["by_type"].items():
        if not r["sufficient"]:
            lines.append(f"{bet_type}: {r['graded']} pick{'s' if r['graded'] != 1 else ''} graded ({r['hits']} hit). INSUFFICIENT DATA "
                         f"(needs {MIN_PICKS_FOR_RECORD}). {LABEL}.")
            continue
        lo, hi = r["hit_rate_ci"]
        text = (f"{bet_type}: {r['graded']} picks graded, hit rate {r['hit_rate'] * 100:.0f}% (95% CI {lo * 100:.0f}-{hi * 100:.0f}%) "
                f"vs the model's own expected {r['expected_hit_rate'] * 100:.0f}%")
        if "return_per_2" in r:
            rl, rh = r["return_ci"]
            text += (f"; return at chart payoffs {r['return_per_2']:+.2f} per $2 staked (95% CI {rl:+.2f} to {rh:+.2f}, "
                     f"{r['money_legs']} legs)")
        elif r["money_legs"] == 0:
            text += "; return not computed: no stored chart payoffs for these races (hit rate still counts)"
        else:
            text += f"; return INSUFFICIENT DATA ({r['money_legs']} legs with stored chart payoffs)"
        lines.append(text + f". {LABEL}.")
    return lines
