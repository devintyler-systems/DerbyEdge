"""Walk-forward evaluation of the win probabilities the engine actually produced.

The model needs no retraining to be tested: every score run is stored with the time it was made, so each
race is graded with the forecast that existed BEFORE post, against the official result, in date order, next to
the benchmarks a bettor could have used.  Nothing is recomputed from information that arrived later.

Forecasts compared (all renormalised over the horses that actually started, identically for every forecast):

  model_board       the probability the board displayed (``entry_scores.win_probability``)
  model_pre_market  the model before any market input (``p_model_pre_market``), when stored
  morning_line      morning-line odds with the overround removed
  live_market       the last complete, valid pre-post book capture, overround removed
  closing_tote      final tote odds, overround removed  -- HINDSIGHT: it does not exist at decision time, it is
                    the yardstick for "how good is the market by the end", not a forecast anyone could use
  uniform           1/n

A race is graded only if it has an official result with exactly one winner, at least two starters, and a score run
made before its scheduled post.  Races with no scheduled post time cannot prove that, and are excluded unless
``include_unproven`` is set.  Every exclusion is counted with its reason.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Callable, Sequence

from src.analysis.forecast_metrics import (
    MIN_RACES_FOR_VERDICT, Race, calibration_table, chronological_folds, paired_comparison, race_log_loss,
    running_log_loss, summarize,
)
from src.models.policy import bucket_field_size
from src.services.market_snapshot_intake import latest_valid_market_snapshot
from src.services.model_family import classify_model_family

FORECASTS = ("model_board", "model_pre_market", "morning_line", "live_market", "closing_tote", "uniform")
HINDSIGHT = frozenset({"closing_tote"})
THIN_SEGMENT_RACES = 10


@dataclasses.dataclass
class GradedRace:
    race: Race
    card_id: int
    track: str
    race_number: int
    family: str
    field_bucket: str
    run_id: str
    run_timestamp: str
    post_utc: str | None
    engine_version: str              # score_runs.engine_version of the graded run; 'legacy' = scored before the stamp existed
    as_of: str                       # PROVEN (a run before the scheduled post) | UNPROVEN (no post time on record)
    starters: list[str]              # programs, in forecast order


@dataclasses.dataclass
class RaceInput:
    """A race as a retrainable model may see it at prediction time: no result."""
    key: str
    when: date
    family: str
    field_bucket: str
    starters: list[str]
    forecasts: dict[str, list[float]]


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalise(values: Sequence[float | None]) -> list[float] | None:
    if any(v is None or v != v or v < 0 for v in values):
        return None
    total = sum(values)                      # type: ignore[arg-type]
    return [v / total for v in values] if total > 0 else None     # type: ignore[operator]


def _inverse(values: Sequence[float | None]) -> list[float] | None:
    if any(v is None or v <= 0 for v in values):
        return None
    return _normalise([1.0 / v for v in values])               # type: ignore[operator]


def load_graded_races(
    conn: sqlite3.Connection, *, include_unproven: bool = False, since: str | None = None, until: str | None = None,
    track: str | None = None, engine_version: str | None = None,
) -> tuple[list[GradedRace], Counter]:
    """Return the gradable races (oldest first) and a count of why the others were excluded."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='race_results'").fetchone() is None:
        return [], Counter({"NO_RESULTS_TABLE": 1})
    where, params = ["EXISTS (SELECT 1 FROM race_results rr WHERE rr.card_id=rc.card_id)"], []
    if since:
        where.append("rc.card_date >= ?"), params.append(since)
    if until:
        where.append("rc.card_date <= ?"), params.append(until)
    if track:
        where.append("t.abbrev = ?"), params.append(track.upper())
    cards = conn.execute(
        f"""SELECT rc.card_id, rc.card_date, rc.race_number, rc.scheduled_post_time_utc, rc.surface, rc.distance_furlongs,
                   rc.stakes_name, rc.race_class, t.abbrev
            FROM race_cards rc JOIN tracks t ON t.track_id=rc.track_id
            WHERE {' AND '.join(where)} ORDER BY rc.card_date, t.abbrev, rc.race_number""", params).fetchall()
    has_stamp = "engine_version" in {r[1] for r in conn.execute("PRAGMA table_info(score_runs)")}
    stamp_sql = "COALESCE(engine_version, 'legacy')" if has_stamp else "'legacy'"
    graded: list[GradedRace] = []
    excluded: Counter = Counter()
    for card_id, card_date, race_number, post, surface, furlongs, stakes, race_class, abbrev in cards:
        results = conn.execute(
            """SELECT rr.entry_id, rr.official_finish, rr.is_scratched, rr.official_odds_decimal, e.morning_line_odds,
                      COALESCE(e.program_number, CAST(e.post_position AS TEXT))
               FROM race_results rr JOIN entries e ON e.entry_id=rr.entry_id
               WHERE rr.card_id=? AND rr.is_scratched=0 ORDER BY e.post_position, e.entry_id""", (card_id,)).fetchall()
        if len(results) < 2:
            excluded["FIELD_TOO_SMALL_OR_NO_RESULT"] += 1
            continue
        winners = [i for i, r in enumerate(results) if r[1] == 1]
        if not winners:
            excluded["NO_OFFICIAL_WINNER"] += 1
            continue
        if len(winners) > 1:
            excluded["DEAD_HEAT"] += 1
            continue
        runs = conn.execute(
            f"SELECT run_id, run_timestamp, {stamp_sql} FROM score_runs WHERE card_id=? ORDER BY run_timestamp, run_id", (card_id,)).fetchall()
        if not runs:
            excluded["NO_SCORE_RUN"] += 1
            continue
        post_dt = _ts(post)
        if post_dt is not None:
            before = [r for r in runs if (_ts(r[1]) or post_dt) < post_dt]
            if not before:
                excluded["ALL_SCORE_RUNS_AFTER_POST"] += 1
                continue
            run_id, run_ts, version = before[-1]
            as_of = "PROVEN"
        else:
            if not include_unproven:
                excluded["AS_OF_UNPROVEN_NO_POST_TIME"] += 1
                continue
            run_id, run_ts, version = runs[-1]
            as_of = "UNPROVEN"
        if engine_version is not None and version != engine_version:
            excluded["OTHER_ENGINE_VERSION"] += 1
            continue
        entry_ids = [r[0] for r in results]
        scores = {r[0]: r for r in conn.execute(
            "SELECT entry_id, win_probability, p_model_pre_market FROM entry_scores WHERE run_id=?", (run_id,))}
        fc: dict[str, list[float]] = {"uniform": [1.0 / len(results)] * len(results)}
        board = _normalise([scores[e][1] if e in scores else None for e in entry_ids])
        if board:
            fc["model_board"] = board
        pre = _normalise([scores[e][2] if e in scores else None for e in entry_ids])
        if pre:
            fc["model_pre_market"] = pre
        ml = _inverse([None if r[4] is None else float(r[4]) + 1.0 for r in results])       # to-one odds -> decimal
        if ml:
            fc["morning_line"] = ml
        tote = _inverse([r[3] for r in results])
        if tote:
            fc["closing_tote"] = tote
        active = [r[0] for r in conn.execute(
            "SELECT entry_id FROM entries WHERE card_id=? AND scratch_flag=0", (card_id,))]
        live = latest_valid_market_snapshot(conn, card_id, active)
        if live:
            live_p = _normalise([live.get(e) for e in entry_ids])
            if live_p:
                fc["live_market"] = live_p
        family = classify_model_family(surface, furlongs, stakes, race_class)
        key = f"{abbrev}|{card_date}|R{race_number}"
        graded.append(GradedRace(
            race=Race(key, date.fromisoformat(card_date), winners[0], fc), card_id=card_id, track=abbrev,
            race_number=race_number, family=family, field_bucket=bucket_field_size(len(results)), run_id=run_id,
            run_timestamp=run_ts, post_utc=post, engine_version=version, as_of=as_of, starters=[str(r[5]) for r in results]))
    return graded, excluded


# ---- evaluation ----------------------------------------------------------------------------------
def _segment(graded: Sequence[GradedRace], attr: str, reference: str, candidate: str) -> dict[str, dict]:
    groups: dict[str, list[Race]] = defaultdict(list)
    for g in graded:
        groups[getattr(g, attr)].append(g.race)
    out = {}
    for value, races in sorted(groups.items()):
        both = [r for r in races if candidate in r.forecasts and reference in r.forecasts]
        out[value] = {
            "n_races": len(races), "n_paired": len(both), "thin": len(both) < THIN_SEGMENT_RACES,
            candidate: summarize(both, candidate), reference: summarize(both, reference),
            "delta_log_loss": (summarize(both, candidate)["log_loss"] - summarize(both, reference)["log_loss"]) if both else None,
        }
    return out


def evaluate(
    graded: Sequence[GradedRace], excluded: Counter | None = None, *, reference: str = "morning_line",
    candidate: str = "model_board", forecasts: Sequence[str] = FORECASTS, n_boot: int = 2000, seed: int = 0,
) -> dict:
    races = [g.race for g in sorted(graded, key=lambda g: (g.race.when, g.race.key))]
    names = [f for f in forecasts if any(f in r.forecasts for r in races)]
    n_starters = sum(len(g.starters) for g in graded)
    result: dict = {
        "population": {
            "n_graded_races": len(graded), "n_starters": n_starters, "n_winners": len(graded),
            "date_min": min((g.race.when for g in graded), default=None),
            "date_max": max((g.race.when for g in graded), default=None),
            "n_as_of_unproven": sum(1 for g in graded if g.as_of == "UNPROVEN"),
            "excluded": dict(excluded or {}),
            "min_races_for_verdict": MIN_RACES_FOR_VERDICT,
        },
        "reference": reference, "candidate": candidate,
        "headline": {n: summarize(races, n) for n in names},
        "calibration": {n: calibration_table(races, n) for n in names},
        "paired": [paired_comparison(races, n, reference, n_boot=n_boot, seed=seed) for n in names
                   if n != reference and any(reference in r.forecasts for r in races)],
        "running_log_loss": running_log_loss(races, [candidate, reference]) if candidate in names and reference in names else [],
        "by_family": _segment(graded, "family", reference, candidate),
        "by_field_size": _segment(graded, "field_bucket", reference, candidate),
        "hindsight_forecasts": sorted(HINDSIGHT & set(names)),
    }
    return result


def per_race_rows(graded: Sequence[GradedRace]) -> list[dict]:
    rows = []
    for g in sorted(graded, key=lambda g: (g.race.when, g.race.key)):
        for name, probs in g.race.forecasts.items():
            rows.append({"race": g.race.key, "date": g.race.when.isoformat(), "family": g.family,
                         "field_size": len(g.starters), "engine_version": g.engine_version, "run_id": g.run_id, "as_of": g.as_of, "forecast": name,
                         "winner_program": g.starters[g.race.winner_idx], "p_winner": probs[g.race.winner_idx],
                         "log_loss": race_log_loss(probs, g.race.winner_idx), "hindsight": name in HINDSIGHT})
    return rows


# ---- retrainable models: out-of-fold forecasts, never random splits --------------------------------
FitPredict = Callable[[Sequence[GradedRace], Sequence[RaceInput]], "dict[str, list[float]]"]


def walk_forward_oof(
    graded: Sequence[GradedRace], fit_predict: FitPredict, *, name: str = "oof_model", n_folds: int = 5,
    min_train: int = 30, embargo_days: int = 1,
) -> tuple[list[GradedRace], dict]:
    """Out-of-fold forecasts from an expanding window.  ``fit_predict(train, test)`` sees full results for the
    training races only; the test races arrive WITHOUT a winner.  Returns copies of the graded races with the new
    forecast attached (only for races that fell in a test fold) and a fold report."""
    ordered = sorted(graded, key=lambda g: (g.race.when, g.race.key))
    folds = chronological_folds([g.race.when for g in ordered], n_folds=n_folds, min_train=min_train,
                                embargo_days=embargo_days)
    predictions: dict[str, list[float]] = {}
    report = []
    for train_idx, test_idx in folds:
        train = [ordered[i] for i in train_idx]
        test = [ordered[i] for i in test_idx]
        if max(g.race.when for g in train).toordinal() >= min(g.race.when for g in test).toordinal() - embargo_days + 0:
            raise AssertionError("training data reaches into the test period")        # cannot happen; guards the folds
        inputs = [RaceInput(g.race.key, g.race.when, g.family, g.field_bucket, g.starters,
                            {k: list(v) for k, v in g.race.forecasts.items()}) for g in test]
        out = fit_predict(train, inputs)
        for g in test:
            probs = out.get(g.race.key)
            if probs is None:
                continue
            if len(probs) != len(g.starters) or abs(sum(probs) - 1.0) > 1e-6 or any(p < 0 for p in probs):
                raise ValueError(f"{name}: invalid probabilities for {g.race.key}")
            predictions[g.race.key] = list(probs)
        report.append({"train_races": len(train), "test_races": len(test),
                       "train_through": max(g.race.when for g in train).isoformat(),
                       "test_from": min(g.race.when for g in test).isoformat()})
    out_races = []
    for g in ordered:
        fc = dict(g.race.forecasts)
        if g.race.key in predictions:
            fc[name] = predictions[g.race.key]
        out_races.append(dataclasses.replace(g, race=Race(g.race.key, g.race.when, g.race.winner_idx, fc)))
    return out_races, {"folds": report, "n_predicted": len(predictions)}


# ---- report ---------------------------------------------------------------------------------------
def _f(x, nd=4):
    return "—" if x is None else f"{x:.{nd}f}"


def render_markdown(result: dict) -> str:
    pop, ref, cand = result["population"], result["reference"], result["candidate"]
    L = [f"# Walk-forward evaluation", "",
         f"Races graded: **{pop['n_graded_races']}** ({pop['n_starters']} starters), "
         f"{pop['date_min'] or '—'} to {pop['date_max'] or '—'}. Each race is graded with the last score run made "
         f"**before its scheduled post**; probabilities are renormalised over the horses that started.", ""]
    if pop["excluded"]:
        L += ["Excluded: " + ", ".join(f"{k}={v}" for k, v in sorted(pop["excluded"].items())), ""]
    if pop["n_as_of_unproven"]:
        L += [f"**{pop['n_as_of_unproven']} graded race(s) have no scheduled post time, so 'forecast before post' is unproven.**", ""]
    if pop["n_graded_races"] < pop["min_races_for_verdict"]:
        L += [f"> **INSUFFICIENT DATA**: fewer than {pop['min_races_for_verdict']} graded races. Numbers below are "
              "descriptive only and no forecast can be called better or worse.", ""]
    L += ["## Headline (each forecast on the races where it exists)", "",
          "| forecast | races | log loss | Brier | top-1 | mean winner rank |", "|---|---|---|---|---|---|"]
    for name, s in result["headline"].items():
        tag = " (hindsight)" if name in result["hindsight_forecasts"] else ""
        L.append(f"| {name}{tag} | {s['n_races']} | {_f(s['log_loss'])} | {_f(s['brier'])} | {_f(s['top1'], 3)} | {_f(s['mean_winner_rank'], 2)} |")
    L += ["", f"## Paired against `{ref}` (negative = better than the reference)", "",
          "| forecast | races | Δ log loss | 95% CI | Δ Brier | verdict | races to detect a 1% gain |", "|---|---|---|---|---|---|---|"]
    for p in result["paired"]:
        ll, br = p["delta_log_loss"], p["delta_brier"]
        ci = f"[{_f(ll['lo'])}, {_f(ll['hi'])}]" if ll["lo"] is not None else "—"
        need = p["races_needed_to_detect_1pct_log_loss_gain"]
        L.append(f"| {p['candidate']} | {p['n_races']} | {_f(ll['mean'])} | {ci} | {_f(br['mean'])} | {p['verdict']} | {need if need else '—'} |")
    L += ["", "`closing_tote` is hindsight (final odds are not known at decision time); it is the yardstick for how good "
          "the market ends up, not a forecast that could have been used.", ""]
    for title, key in (("Segments by race family", "by_family"), ("Segments by field size", "by_field_size")):
        seg = result[key]
        if seg:
            L += [f"## {title} ({cand} vs {ref})", "", "| segment | races | paired | Δ log loss | note |", "|---|---|---|---|---|"]
            for k, v in seg.items():
                L.append(f"| {k} | {v['n_races']} | {v['n_paired']} | {_f(v['delta_log_loss'])} | {'thin (<' + str(THIN_SEGMENT_RACES) + ')' if v['thin'] else ''} |")
            L.append("")
    for name in (cand, ref):
        cal = result["calibration"].get(name)
        if cal and cal["n_starters"]:
            L += [f"## Calibration: {name} ({cal['n_starters']} starters, {cal['n_winners']} winners, ECE {_f(cal['ece'])})", "",
                  "| predicted bin | starters | mean predicted | observed win rate |", "|---|---|---|---|"]
            for b in cal["bins"]:
                if b["n"]:
                    L.append(f"| {b['lo']:.2f}–{b['hi']:.2f} | {b['n']} | {_f(b['mean_predicted'])} | {_f(b['observed_rate'])} |")
            L.append("")
    return "\n".join(L)
