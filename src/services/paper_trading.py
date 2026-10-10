"""Paper trading: what the engine would have bet, settled on what the tote actually paid.

Racing is pari-mutuel, so the price you see when you decide is NOT the price you get: the payoff is set when the
pool closes.  The harness therefore keeps the two apart.

  decide   uses only what existed at the decision moment: the last stored score run and the last complete book
           capture, both no later than ``post - decision_lag``.  It never reads a result, a chart or a scratch
           flag, so replaying history gives the same bets as running it live (a test proves this).
  settle   uses the chart: the exact $2 win payoff for a winner, 0 for a loser, a refund for a horse scratched
           after the bet.  Dead heats are voided rather than guessed.
  report   return on stake with an interval, closing-line value (decision price against the final price), a
           calibration check of the model's own probabilities on the horses it backed, and two baselines settled
           on the same payoffs (every runner; the favourite) that show what the takeout alone costs.

Bets are recorded once per policy and race; the first qualifying decision stands.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import numpy as np

from src.services.chart_results_intake import (
    ensure_chart_result_tables, load_chart_race, reconcile_race_to_card, store_reconciliation,
)
from src.services.market_snapshot_intake import latest_valid_market_capture

_DDL = """
CREATE TABLE IF NOT EXISTS paper_policies (
  policy_id    TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  params_json  TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_race_decisions (
  decision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  policy_id      TEXT NOT NULL REFERENCES paper_policies(policy_id),
  card_id        INTEGER NOT NULL,
  race_key       TEXT NOT NULL,
  decision_time  TEXT NOT NULL,
  status         TEXT NOT NULL CHECK(status IN ('BET','NO_BET')),
  run_id         TEXT NOT NULL,
  capture_time   TEXT NOT NULL,
  runners_json   TEXT NOT NULL,
  UNIQUE(policy_id, card_id)
);
CREATE TABLE IF NOT EXISTS paper_bets (
  bet_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  policy_id         TEXT NOT NULL REFERENCES paper_policies(policy_id),
  card_id           INTEGER NOT NULL,
  race_key          TEXT NOT NULL,
  entry_id          INTEGER NOT NULL,
  program           TEXT,
  horse_name        TEXT,
  run_id            TEXT NOT NULL,
  decision_time     TEXT NOT NULL,
  capture_time      TEXT NOT NULL,
  capture_provider  TEXT NOT NULL,
  post_utc          TEXT NOT NULL,
  model_p           REAL NOT NULL,
  market_p          REAL NOT NULL,
  captured_decimal  REAL NOT NULL,
  edge              REAL NOT NULL,
  ev                REAL NOT NULL,
  stake             REAL NOT NULL,
  status            TEXT NOT NULL DEFAULT 'OPEN',
  final_decimal     REAL,
  win_payoff        REAL,
  return_amount     REAL,
  profit            REAL,
  clv               REAL,
  settled_at        TEXT,
  note              TEXT,
  UNIQUE(policy_id, card_id, entry_id)
);
"""

SETTLED = ("WON", "LOST", "REFUNDED_SCRATCH")


def ensure_paper_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def _ensure_policy(conn: sqlite3.Connection, policy: "PaperPolicy") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO paper_policies (policy_id, name, params_json, created_at) VALUES (?,?,?,?)",
        (policy.policy_id, policy.name, json.dumps(dataclasses.asdict(policy), sort_keys=True),
         datetime.now(timezone.utc).isoformat()))


@dataclasses.dataclass(frozen=True)
class PaperPolicy:
    name: str = "edge_ev_v1"
    probability_source: str = "model_board"          # model_board | model_pre_market
    min_edge: float = 0.025                          # model p minus the devigged market p
    min_ev: float = 0.05                             # model p * captured decimal odds - 1
    min_decimal: float = 2.0                         # captured decimal odds including the stake
    max_decimal: float = 21.0
    max_bets_per_race: int = 1
    decision_lag_minutes: float = 2.0                # a bet must be placed this long before post
    max_price_age_minutes: float = 20.0              # the price may be no older than this at decision time
    require_engine_bet_tag: bool = False             # only back horses the engine tagged 'bet'
    respect_low_conf_block: bool = True
    staking: str = "flat"                            # flat | kelly
    flat_stake: float = 2.0
    bankroll: float = 1000.0
    kelly_fraction: float = 0.25
    kelly_cap: float = 0.02

    def __post_init__(self) -> None:
        if self.probability_source not in ("model_board", "model_pre_market"):
            raise ValueError("probability_source must be model_board or model_pre_market")
        if self.staking not in ("flat", "kelly"):
            raise ValueError("staking must be flat or kelly")
        if self.max_bets_per_race < 1 or self.flat_stake <= 0 or self.min_decimal <= 1.0 or self.max_decimal < self.min_decimal:
            raise ValueError("invalid policy bounds")

    @property
    def policy_id(self) -> str:
        return hashlib.sha256(json.dumps(dataclasses.asdict(self), sort_keys=True).encode()).hexdigest()[:16]


@dataclasses.dataclass
class PaperBet:
    policy_id: str
    card_id: int
    race_key: str
    entry_id: int
    program: str | None
    horse_name: str | None
    run_id: str
    decision_time: str
    capture_time: str
    capture_provider: str
    post_utc: str
    model_p: float
    market_p: float
    captured_decimal: float
    edge: float
    ev: float
    stake: float


@dataclasses.dataclass
class DecisionReport:
    bets: list[PaperBet]
    no_bet_races: list[str]
    skipped: Counter
    races_decided: int = 0


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stake(policy: PaperPolicy, p: float, decimal: float) -> float:
    if policy.staking == "flat":
        return round(policy.flat_stake, 2)
    f = (p * decimal - 1.0) / (decimal - 1.0)
    return round(policy.bankroll * min(policy.kelly_cap, policy.kelly_fraction * max(f, 0.0)), 2)


def decide_bets(
    conn: sqlite3.Connection, policy: PaperPolicy, *, now: datetime | None = None, since: str | None = None,
    until: str | None = None, track: str | None = None,
) -> DecisionReport:
    """Choose bets from information available at the decision moment.  Reads no result, chart or scratch flag.

    ``now`` given: live mode - the decision is made at ``now`` and only races whose cut-off (post - lag) is still
    ahead are considered.  ``now`` omitted: replay - each race is decided at the earliest moment both its model run
    and its price existed, provided that is before the cut-off."""
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    lag = timedelta(minutes=policy.decision_lag_minutes)
    where, params = ["rc.scheduled_post_time_utc IS NOT NULL"], []
    if since:
        where.append("rc.card_date >= ?"), params.append(since)
    if until:
        where.append("rc.card_date <= ?"), params.append(until)
    if track:
        where.append("t.abbrev = ?"), params.append(track.upper())
    cards = conn.execute(
        f"""SELECT rc.card_id, rc.card_date, rc.race_number, rc.scheduled_post_time_utc, t.abbrev
            FROM race_cards rc JOIN tracks t ON t.track_id=rc.track_id
            WHERE {' AND '.join(where)} ORDER BY rc.scheduled_post_time_utc, rc.card_id""", params).fetchall()
    skipped: Counter = Counter()
    bets: list[PaperBet] = []
    no_bet: list[str] = []
    ensure_paper_tables(conn)
    _ensure_policy(conn, policy)
    decided = {r[0] for r in conn.execute("SELECT card_id FROM paper_race_decisions WHERE policy_id=?", (policy.policy_id,))}
    races_decided = 0
    for card_id, card_date, race_number, post_raw, abbrev in cards:
        key = f"{abbrev}|{card_date}|R{race_number}"
        post = _ts(post_raw)
        if post is None:
            skipped["BAD_POST_TIME"] += 1
            continue
        if card_id in decided:
            skipped["ALREADY_DECIDED"] += 1
            continue
        cutoff = post - lag
        if now is not None and now > cutoff:
            skipped["TOO_CLOSE_TO_POST_OR_PAST"] += 1
            continue
        limit = now if now is not None else cutoff
        runs = [r for r in conn.execute(
            "SELECT run_id, run_timestamp FROM score_runs WHERE card_id=? ORDER BY run_timestamp, run_id", (card_id,))
            if (_ts(r[1]) or limit + timedelta(days=1)) <= limit]
        if not runs:
            skipped["NO_SCORE_RUN_BEFORE_CUTOFF"] += 1
            continue
        run_id, run_ts = runs[-1]
        scored = conn.execute(
            """SELECT s.entry_id, s.win_probability, s.p_model_pre_market, s.bet_tag, s.low_conf_bet_block, s.horse_name,
                      e.program_number
               FROM entry_scores s JOIN entries e ON e.entry_id=s.entry_id WHERE s.run_id=? ORDER BY s.post_position""",
            (run_id,)).fetchall()
        col = 1 if policy.probability_source == "model_board" else 2
        raw_p = [r[col] for r in scored]
        if len(scored) < 2 or any(v is None or v < 0 or v != v for v in raw_p) or sum(raw_p) <= 0:
            skipped["MODEL_PROBABILITIES_INCOMPLETE"] += 1
            continue
        total_p = sum(raw_p)
        capture = latest_valid_market_capture(conn, card_id, [r[0] for r in scored], not_after=limit.isoformat())
        if capture is None:
            skipped["NO_VALID_PRICE"] += 1
            continue
        capture_time, run_time = _ts(capture["time"]), _ts(run_ts)
        decision_time = now if now is not None else max(capture_time, run_time)
        if (decision_time - capture_time) > timedelta(minutes=policy.max_price_age_minutes):
            skipped["STALE_PRICE"] += 1
            continue
        implied = capture["implied"]
        market_total = sum(implied.values())
        runners, candidates = [], []
        for (entry_id, _p, _pre, tag, blocked, name, program), pv in zip(scored, raw_p):
            p = pv / total_p
            decimal = 1.0 / implied[entry_id]
            m = implied[entry_id] / market_total
            ev, edge = p * decimal - 1.0, p - m
            runners.append({"entry_id": entry_id, "program": program, "decimal": decimal, "market_p": m, "model_p": p})
            if policy.respect_low_conf_block and blocked:
                continue
            if policy.require_engine_bet_tag and tag != "bet":
                continue
            if ev >= policy.min_ev and edge >= policy.min_edge and policy.min_decimal <= decimal <= policy.max_decimal:
                candidates.append((ev, entry_id, name, program, p, m, decimal, edge))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        chosen = candidates[:policy.max_bets_per_race]
        races_decided += 1
        conn.execute(
            """INSERT OR IGNORE INTO paper_race_decisions (policy_id, card_id, race_key, decision_time, status, run_id,
               capture_time, runners_json) VALUES (?,?,?,?,?,?,?,?)""",
            (policy.policy_id, card_id, key, decision_time.isoformat(), "BET" if chosen else "NO_BET", run_id,
             capture["time"], json.dumps(runners)))
        if not chosen:
            no_bet.append(key)
        for ev, entry_id, name, program, p, m, decimal, edge in chosen:
            stake = _stake(policy, p, decimal)
            if stake <= 0:
                continue
            bets.append(PaperBet(policy.policy_id, card_id, key, entry_id, program, name, run_id,
                                 decision_time.isoformat(), capture["time"], capture["provider"], post.isoformat(),
                                 p, m, decimal, edge, ev, stake))
    conn.commit()
    return DecisionReport(bets, no_bet, skipped, races_decided)


def record_bets(conn: sqlite3.Connection, policy: PaperPolicy, bets: Sequence[PaperBet]) -> int:
    ensure_paper_tables(conn)
    _ensure_policy(conn, policy)
    n = 0
    for b in bets:
        cur = conn.execute(
            """INSERT OR IGNORE INTO paper_bets (policy_id, card_id, race_key, entry_id, program, horse_name, run_id,
               decision_time, capture_time, capture_provider, post_utc, model_p, market_p, captured_decimal, edge, ev, stake)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (b.policy_id, b.card_id, b.race_key, b.entry_id, b.program, b.horse_name, b.run_id, b.decision_time,
             b.capture_time, b.capture_provider, b.post_utc, b.model_p, b.market_p, b.captured_decimal, b.edge, b.ev, b.stake))
        n += cur.rowcount
    conn.commit()
    return n


def place_paper_bets(conn: sqlite3.Connection, policy: PaperPolicy, **kwargs: Any) -> DecisionReport:
    """decide + record, atomically: the race decisions and the bets are written together or not at all."""
    ensure_paper_tables(conn)
    _ensure_policy(conn, policy)
    conn.commit()
    report = decide_bets(conn, policy, **kwargs)
    record_bets(conn, policy, report.bets)
    return report


# ---- settlement -----------------------------------------------------------------------------------
def settle_paper_bets(conn: sqlite3.Connection, policy_id: str | None = None) -> dict[str, int]:
    """Settle open bets from the stored charts.  A bet stays open until its race has a chart whose join to the
    pre-race card reconciles cleanly."""
    ensure_paper_tables(conn)
    ensure_chart_result_tables(conn)
    where, params = "status='OPEN'", []
    if policy_id:
        where += " AND policy_id=?"
        params.append(policy_id)
    open_bets = conn.execute(
        f"SELECT bet_id, race_key, program, captured_decimal, stake, entry_id FROM paper_bets WHERE {where} ORDER BY bet_id",
        params).fetchall()
    counts: Counter = Counter()
    now = datetime.now(timezone.utc).isoformat()
    for bet_id, race_key, program, captured_decimal, stake, _entry in open_bets:
        row = conn.execute(
            "SELECT result_race_id, flags_json FROM result_races WHERE race_key=? AND is_current=1", (race_key,)).fetchone()
        if row is None:
            counts["NO_CHART_YET"] += 1
            continue
        rid, flags = row[0], json.loads(row[1])
        if "DEAD_HEAT" in flags:
            conn.execute("UPDATE paper_bets SET status='VOID_DEAD_HEAT', return_amount=stake, profit=0, settled_at=?, "
                         "note='dead heat: payoff split not modelled; stake returned' WHERE bet_id=?", (now, bet_id))
            counts["VOID_DEAD_HEAT"] += 1
            continue
        rec = reconcile_race_to_card(conn, load_chart_race(conn, rid), rid)
        store_reconciliation(conn, rec)
        if rec.status != "MATCHED":
            counts["HELD_RECONCILIATION"] += 1
            conn.execute("UPDATE paper_bets SET note=? WHERE bet_id=?",
                         ("held: card and chart disagree: " + ", ".join(e["code"] for e in rec.errors) if rec.errors
                          else "held: no matching card", bet_id))
            continue
        s = conn.execute(
            "SELECT finish_order, decimal_odds, win_payoff, horse_key FROM result_starters WHERE result_race_id=? AND program=?",
            (rid, program)).fetchone()
        if s is None:
            name = conn.execute("SELECT horse_name FROM paper_bets WHERE bet_id=?", (bet_id,)).fetchone()[0]
            scratched = any(rec_w["code"] == "LATE_SCRATCH" and rec_w["program"] == program for rec_w in rec.warnings)
            if scratched:
                conn.execute("UPDATE paper_bets SET status='REFUNDED_SCRATCH', return_amount=stake, profit=0, "
                             "settled_at=?, note=? WHERE bet_id=?", (now, f"{name} scratched after the bet; stake refunded", bet_id))
                counts["REFUNDED_SCRATCH"] += 1
            else:
                counts["HELD_NOT_IN_CHART"] += 1
                conn.execute("UPDATE paper_bets SET note='held: runner is neither a chart starter nor a chart scratch' WHERE bet_id=?", (bet_id,))
            continue
        finish, final_decimal, win_payoff, _hk = s
        clv = captured_decimal / final_decimal - 1.0
        if finish == 1:
            if win_payoff is None:
                counts["HELD_NO_PAYOFF"] += 1
                conn.execute("UPDATE paper_bets SET note='held: winner has no win payoff in the chart' WHERE bet_id=?", (bet_id,))
                continue
            ret = round(stake * win_payoff / 2.0, 2)
            conn.execute(
                "UPDATE paper_bets SET status='WON', final_decimal=?, win_payoff=?, return_amount=?, profit=?, clv=?, settled_at=? WHERE bet_id=?",
                (final_decimal, win_payoff, ret, round(ret - stake, 2), clv, now, bet_id))
            counts["WON"] += 1
        else:
            conn.execute(
                "UPDATE paper_bets SET status='LOST', final_decimal=?, return_amount=0, profit=?, clv=?, settled_at=? WHERE bet_id=?",
                (final_decimal, -stake, clv, now, bet_id))
            counts["LOST"] += 1
    conn.commit()
    return dict(counts)


# ---- report ---------------------------------------------------------------------------------------
MIN_BETS_FOR_VERDICT = 100
MIN_BETS_FOR_CLV_VERDICT = 50


def _boot(values: np.ndarray, stat, *, n_boot: int, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = len(values)
    idx = rng.integers(0, n, size=(n_boot, n))
    stats = np.array([stat(values[i]) for i in idx])
    return float(np.quantile(stats, 0.025)), float(np.quantile(stats, 0.975))


def _drawdown(profits: Sequence[float]) -> float:
    peak = cum = worst = 0.0
    for p in profits:
        cum += p
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def summarize_bets(rows: Sequence[dict], *, n_boot: int = 2000, seed: int = 0) -> dict:
    real = [r for r in rows if r["status"] in ("WON", "LOST")]
    refunds = [r for r in rows if r["status"] == "REFUNDED_SCRATCH"]
    out: dict[str, Any] = {"n_bets": len(real), "n_refunded_scratch": len(refunds)}
    if not real:
        return {**out, "staked": 0.0, "returned": 0.0, "profit": 0.0, "roi": None, "roi_ci": None, "hit_rate": None,
                "expected_hit_rate": None, "mean_decimal": None, "mean_clv": None, "clv_ci": None, "pct_positive_clv": None,
                "max_drawdown": 0.0, "verdict": "INSUFFICIENT_DATA (no settled bets)", "clv_verdict": "INSUFFICIENT_DATA",
                "bets_needed_to_detect_5pct_roi": None}
    stake = np.array([r["stake"] for r in real])
    profit = np.array([r["profit"] for r in real])
    ret = stake + profit
    clv = np.array([r["clv"] for r in real])
    won = np.array([1.0 if r["status"] == "WON" else 0.0 for r in real])
    p = np.array([r["model_p"] for r in real])
    roi = float(profit.sum() / stake.sum())
    pairs = np.column_stack([stake, profit])
    lo, hi = _boot(pairs, lambda a: a[:, 1].sum() / a[:, 0].sum(), n_boot=n_boot, seed=seed)
    clo, chi = _boot(clv, np.mean, n_boot=n_boot, seed=seed + 1)
    n = len(real)
    if n < MIN_BETS_FOR_VERDICT:
        verdict = f"INSUFFICIENT_DATA (n={n} < {MIN_BETS_FOR_VERDICT})"
    elif lo > 0:
        verdict = "PROFITABLE_AFTER_TAKEOUT"
    elif hi < 0:
        verdict = "LOSING"
    else:
        verdict = "NOT_DISTINGUISHABLE_FROM_BREAKEVEN"
    if n < MIN_BETS_FOR_CLV_VERDICT:
        clv_verdict = f"INSUFFICIENT_DATA (n={n} < {MIN_BETS_FOR_CLV_VERDICT})"
    elif clo > 0:
        clv_verdict = "BEATING_THE_CLOSE"
    elif chi < 0:
        clv_verdict = "WORSE_THAN_THE_CLOSE"
    else:
        clv_verdict = "NOT_DISTINGUISHABLE_FROM_THE_CLOSE"
    sd = float(np.std(profit / stake, ddof=1)) if n > 1 else 0.0
    needed = int(math.ceil((1.96 * sd / 0.05) ** 2)) if sd > 0 else None
    expected_wins = float(p.sum())
    sd_wins = float(math.sqrt((p * (1 - p)).sum()))
    return {**out, "staked": float(stake.sum()), "returned": float(ret.sum()), "profit": float(profit.sum()), "roi": roi,
            "roi_ci": [lo, hi], "hit_rate": float(won.mean()), "expected_hit_rate": float(p.mean()),
            "wins": int(won.sum()), "expected_wins": expected_wins,
            "wins_z": (float(won.sum()) - expected_wins) / sd_wins if sd_wins > 0 else None,
            "mean_decimal": float(np.mean([r["captured_decimal"] for r in real])),
            "mean_clv": float(clv.mean()), "clv_ci": [clo, chi], "pct_positive_clv": float((clv > 0).mean()),
            "max_drawdown": _drawdown(profit.tolist()), "verdict": verdict, "clv_verdict": clv_verdict,
            "bets_needed_to_detect_5pct_roi": needed}


def _baselines(conn: sqlite3.Connection, policy_id: str, stake: float) -> dict[str, dict]:
    """Settle two reference strategies on the same decided races and the same payoffs: back every runner, back the
    favourite (shortest captured price).  Both include the takeout, so they show what betting costs by itself."""
    all_rows, fav_rows = [], []
    for race_key, runners_json in conn.execute(
            "SELECT race_key, runners_json FROM paper_race_decisions WHERE policy_id=? ORDER BY decision_id", (policy_id,)):
        cur = conn.execute("SELECT result_race_id, flags_json FROM result_races WHERE race_key=? AND is_current=1", (race_key,)).fetchone()
        if cur is None or "DEAD_HEAT" in json.loads(cur[1]):
            continue
        starters = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT program, finish_order, win_payoff FROM result_starters WHERE result_race_id=?", (cur[0],))}
        runners = [r for r in json.loads(runners_json) if r["program"] in starters]
        if not runners:
            continue

        def settle(r: dict) -> dict:
            finish, payoff = starters[r["program"]]
            won = finish == 1 and payoff is not None
            return {"status": "WON" if won else "LOST", "stake": stake, "profit": (stake * payoff / 2 - stake) if won else -stake,
                    "model_p": r["model_p"], "captured_decimal": r["decimal"], "clv": 0.0}

        all_rows += [settle(r) for r in runners]
        fav_rows.append(settle(min(runners, key=lambda r: (r["decimal"], r["program"]))))
    return {"every_runner": summarize_bets(all_rows, n_boot=500), "favourite": summarize_bets(fav_rows, n_boot=500)}


def build_report(conn: sqlite3.Connection, policy_id: str, *, n_boot: int = 2000, seed: int = 0) -> dict:
    ensure_paper_tables(conn)
    pol = conn.execute("SELECT name, params_json FROM paper_policies WHERE policy_id=?", (policy_id,)).fetchone()
    if pol is None:
        raise KeyError(f"unknown policy {policy_id}")
    params = json.loads(pol[1])
    cols = ["bet_id", "race_key", "horse_name", "program", "decision_time", "status", "stake", "profit", "model_p",
            "market_p", "captured_decimal", "final_decimal", "clv", "edge", "ev", "win_payoff", "note"]
    rows = [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM paper_bets WHERE policy_id=? ORDER BY decision_time, bet_id", (policy_id,))]
    status_counts = Counter(r["status"] for r in rows)
    decided = conn.execute("SELECT COUNT(*), SUM(status='BET') FROM paper_race_decisions WHERE policy_id=?", (policy_id,)).fetchone()
    settled = [r for r in rows if r["status"] in SETTLED]
    return {
        "policy_id": policy_id, "name": pol[0], "policy": params, "races_decided": decided[0], "races_with_a_bet": decided[1] or 0,
        "bets_by_status": dict(status_counts), "summary": summarize_bets(settled, n_boot=n_boot, seed=seed),
        "baselines": _baselines(conn, policy_id, float(params["flat_stake"])), "bets": rows,
        "open_bets": status_counts.get("OPEN", 0),
    }


def _f(x, nd=3, pct=False):
    if x is None:
        return "—"
    return f"{x * 100:.1f}%" if pct else f"{x:.{nd}f}"


def render_markdown(rep: dict) -> str:
    s, pol = rep["summary"], rep["policy"]
    L = [f"# Paper trading: {rep['name']} (`{rep['policy_id']}`)", "",
         f"Races decided: **{rep['races_decided']}**, with a bet in **{rep['races_with_a_bet']}**. "
         f"Bets: {rep['bets_by_status']}.", "",
         "Decisions use only the last model run and book capture available at least "
         f"{pol['decision_lag_minutes']:g} minutes before post. Bets are settled on the exact $2 payoff printed in the chart; "
         "the price at decision time is **not** the price paid (pari-mutuel).", ""]
    if s["n_bets"] < MIN_BETS_FOR_VERDICT:
        L += [f"> **INSUFFICIENT DATA**: {s['n_bets']} settled bets (< {MIN_BETS_FOR_VERDICT}). Treat every number below as a "
              "description of those bets, not as evidence the policy works.", ""]
    L += ["## Result", "", "| bets | staked | returned | profit | ROI | 95% CI | hit rate | expected hit rate | max drawdown |",
          "|---|---|---|---|---|---|---|---|---|",
          f"| {s['n_bets']} | {_f(s['staked'], 2)} | {_f(s['returned'], 2)} | {_f(s['profit'], 2)} | {_f(s['roi'], pct=True)} | "
          f"{('[' + _f(s['roi_ci'][0], pct=True) + ', ' + _f(s['roi_ci'][1], pct=True) + ']') if s['roi_ci'] else '—'} | "
          f"{_f(s['hit_rate'], pct=True)} | {_f(s['expected_hit_rate'], pct=True)} | {_f(s['max_drawdown'], 2)} |", "",
          f"Verdict: **{s['verdict']}**. Bets needed to detect a 5% ROI at this noise: {s['bets_needed_to_detect_5pct_roi'] or '—'}.", ""]
    if s.get("wins_z") is not None:
        L += [f"Calibration of the backed horses: {s['wins']} wins vs {s['expected_wins']:.1f} expected from the model's own "
              f"probabilities (z = {s['wins_z']:.2f}). A large negative z means the model is overrating the horses it backs.", ""]
    L += ["## Closing-line value", "",
          f"Mean CLV (decision price / final price - 1): **{_f(s['mean_clv'], pct=True)}**, "
          f"95% CI {('[' + _f(s['clv_ci'][0], pct=True) + ', ' + _f(s['clv_ci'][1], pct=True) + ']') if s['clv_ci'] else '—'}, "
          f"{_f(s['pct_positive_clv'], pct=True)} of bets beat the close. Verdict: **{s['clv_verdict']}**. "
          "Positive CLV is the earliest, least noisy sign of an edge: beating the close does not need hundreds of results.", "",
          "## What betting costs by itself (same races, same payoffs, flat stake)", "",
          "| strategy | bets | ROI | 95% CI |", "|---|---|---|---|"]
    for name, b in rep["baselines"].items():
        ci = f"[{_f(b['roi_ci'][0], pct=True)}, {_f(b['roi_ci'][1], pct=True)}]" if b["roi_ci"] else "—"
        L.append(f"| {name.replace('_', ' ')} | {b['n_bets']} | {_f(b['roi'], pct=True)} | {ci} |")
    L += ["", "A policy that cannot beat these is paying the takeout for nothing.", ""]
    return "\n".join(L)
