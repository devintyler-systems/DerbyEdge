"""Persist validated Equibase charts and reconcile them to pre-race cards.

Result tables are independent of ``race_cards``: a chart can be stored for a race we never ingested
pre-race (a history corpus), and is joined to a card by the race key (track code, date, race number) and
program number when one exists.

* ``result_sources``      one row per chart file (SHA-256, parser version, raw bytes, outcome)
* ``result_races``        one row per race version; exactly one ``is_current`` per race key
* ``result_starters`` / ``result_scratches`` / ``result_payoffs``  the race's contents
* ``result_card_reconciliations``  what the join to a pre-race card found

Only races that passed every parser self-check are stored.  Re-ingesting the same file is a no-op; a different
chart for the same race (for example one published after a stewards' change) supersedes the old version, which
is kept.  Nothing here modifies a pre-race card.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.ingest.equibase_chart import (
    PARSER_VERSION, ChartCard, ChartPayoff, ChartRace, ChartScratch, ChartStarter, parse_chart_pdf,
)
from src.services.race_card_builder import find_race_card

_DDL = """
CREATE TABLE IF NOT EXISTS result_sources (
  source_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256           TEXT NOT NULL,
  parser_version   TEXT NOT NULL,
  filename         TEXT NOT NULL,
  raw_bytes        BLOB NOT NULL,
  n_races          INTEGER NOT NULL,
  n_stored         INTEGER NOT NULL,
  status           TEXT NOT NULL CHECK(status IN ('INGESTED','PARTIAL','REJECTED')),
  errors_json      TEXT NOT NULL,
  ingested_at      TEXT NOT NULL,
  UNIQUE(sha256, parser_version)
);
CREATE TABLE IF NOT EXISTS result_races (
  result_race_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id          INTEGER NOT NULL REFERENCES result_sources(source_id),
  race_key           TEXT NOT NULL,
  track_code         TEXT NOT NULL,
  race_date          TEXT NOT NULL,
  race_number        INTEGER NOT NULL,
  content_hash       TEXT NOT NULL,
  is_current         INTEGER NOT NULL CHECK(is_current IN (0,1)),
  race_type          TEXT, conditions TEXT, distance_text TEXT, distance_furlongs REAL, surface TEXT,
  purse INTEGER, value_of_race INTEGER, purse_by_place_json TEXT NOT NULL,
  weather TEXT, track_condition TEXT, off_time TEXT, start_comment TEXT, timing_method TEXT,
  fractional_times_json TEXT NOT NULL, final_time TEXT, final_time_seconds REAL,
  winner_name TEXT, win_pool_total INTEGER, flags_json TEXT NOT NULL,
  ingested_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_result_races_current ON result_races(race_key) WHERE is_current=1;
CREATE INDEX IF NOT EXISTS ix_result_races_key ON result_races(track_code, race_date, race_number);
CREATE TABLE IF NOT EXISTS result_starters (
  starter_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  result_race_id     INTEGER NOT NULL REFERENCES result_races(result_race_id),
  program            TEXT NOT NULL,
  horse_name         TEXT NOT NULL,
  horse_key          TEXT NOT NULL,
  jockey TEXT, trainer TEXT, owner TEXT, weight INTEGER, medication_equipment TEXT, post_position INTEGER,
  finish_order       INTEGER NOT NULL,
  run_order          INTEGER NOT NULL,
  disqualified       INTEGER NOT NULL,
  placed_from        INTEGER,
  last_raced TEXT, calls_raw TEXT, comment TEXT,
  odds_to_one        REAL NOT NULL,
  decimal_odds       REAL NOT NULL,
  favorite           INTEGER NOT NULL,
  win_payoff REAL, place_payoff REAL, show_payoff REAL,
  UNIQUE(result_race_id, program)
);
CREATE TABLE IF NOT EXISTS result_scratches (
  scratch_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  result_race_id     INTEGER NOT NULL REFERENCES result_races(result_race_id),
  horse_name TEXT NOT NULL, horse_key TEXT NOT NULL, reason TEXT NOT NULL, also_eligible INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS result_payoffs (
  payoff_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  result_race_id     INTEGER NOT NULL REFERENCES result_races(result_race_id),
  seq INTEGER NOT NULL, wager_base TEXT NOT NULL, wager_type TEXT, winning_numbers TEXT,
  payoff REAL, pool INTEGER, carryover INTEGER, raw TEXT NOT NULL, parsed INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS result_card_reconciliations (
  reconciliation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  result_race_id     INTEGER NOT NULL REFERENCES result_races(result_race_id),
  card_id            INTEGER,
  status             TEXT NOT NULL CHECK(status IN ('MATCHED','MISMATCH','NO_CARD')),
  errors_json        TEXT NOT NULL,
  warnings_json      TEXT NOT NULL,
  checked_at         TEXT NOT NULL,
  UNIQUE(result_race_id)
);
"""


def ensure_chart_result_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _horse_key(name: str) -> str:
    """Name key tolerant of the (GB)/(IRE) country suffix charts append and DraftKings omits."""
    return _squash(re.sub(r"\s*\([A-Z]{2,3}\)\s*$", "", name or ""))


# ---- content identity ----------------------------------------------------------------------------
def race_content_hash(race: ChartRace) -> str:
    payload = dataclasses.asdict(race)
    payload["race_date"] = race.race_date.isoformat() if race.race_date else None
    payload["purse_by_place"] = {str(k): v for k, v in sorted(race.purse_by_place.items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ---- ingest --------------------------------------------------------------------------------------
@dataclasses.dataclass
class RaceOutcome:
    race_key: str
    status: str                       # INSERTED | UNCHANGED | SUPERSEDED | REJECTED
    result_race_id: int | None = None
    problems: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ChartIngestReport:
    filename: str
    sha256: str
    status: str                       # INGESTED | PARTIAL | REJECTED | ALREADY_INGESTED
    outcomes: list[RaceOutcome]
    errors: list[str]

    @property
    def stored(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("INSERTED", "SUPERSEDED"))


def _insert_race(conn: sqlite3.Connection, source_id: int, race: ChartRace, content_hash: str, now: str) -> int:
    cur = conn.execute(
        """INSERT INTO result_races
           (source_id, race_key, track_code, race_date, race_number, content_hash, is_current, race_type, conditions,
            distance_text, distance_furlongs, surface, purse, value_of_race, purse_by_place_json, weather,
            track_condition, off_time, start_comment, timing_method, fractional_times_json, final_time,
            final_time_seconds, winner_name, win_pool_total, flags_json, ingested_at)
           VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (source_id, race.race_key, race.track_code, race.race_date.isoformat(), race.race_number, content_hash,
         race.race_type, race.conditions, race.distance_text, race.distance_furlongs, race.surface, race.purse,
         race.value_of_race, json.dumps({str(k): v for k, v in sorted(race.purse_by_place.items())}),
         race.weather, race.track_condition, race.off_time, race.start_comment, race.timing_method,
         json.dumps(race.fractional_times), race.final_time, race.final_time_seconds, race.winner_name,
         race.win_pool_total, json.dumps(race.flags), now),
    )
    rid = int(cur.lastrowid)
    for s in race.starters:
        conn.execute(
            """INSERT INTO result_starters
               (result_race_id, program, horse_name, horse_key, jockey, trainer, owner, weight, medication_equipment,
                post_position, finish_order, run_order, disqualified, placed_from, last_raced, calls_raw, comment,
                odds_to_one, decimal_odds, favorite, win_payoff, place_payoff, show_payoff)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, s.program, s.horse_name, _horse_key(s.horse_name), s.jockey, s.trainer, s.owner, s.weight,
             s.medication_equipment, s.post_position, s.finish_order, s.run_order, int(s.disqualified), s.placed_from,
             s.last_raced, s.calls_raw, s.comment, s.odds_to_one, s.decimal_odds, int(s.favorite),
             s.win_payoff, s.place_payoff, s.show_payoff),
        )
    for x in race.scratches:
        conn.execute(
            "INSERT INTO result_scratches (result_race_id, horse_name, horse_key, reason, also_eligible) VALUES (?,?,?,?,?)",
            (rid, x.horse_name, _horse_key(x.horse_name), x.reason, int(x.also_eligible)))
    for n, p in enumerate(race.payoffs, start=1):
        conn.execute(
            """INSERT INTO result_payoffs (result_race_id, seq, wager_base, wager_type, winning_numbers, payoff, pool,
               carryover, raw, parsed) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rid, n, p.wager_base, p.wager_type, p.winning_numbers, p.payoff, p.pool, p.carryover, p.raw, int(p.parsed)))
    return rid


def ingest_chart_card(
    conn: sqlite3.Connection, card: ChartCard, *, source_filename: str, raw_bytes: bytes,
) -> ChartIngestReport:
    ensure_chart_result_tables(conn)
    sha = hashlib.sha256(raw_bytes).hexdigest()
    if hashlib.sha256(raw_bytes).hexdigest() != card.source_sha256:
        raise ValueError("raw bytes do not match the parsed chart's SHA-256")
    existing = conn.execute(
        "SELECT source_id FROM result_sources WHERE sha256=? AND parser_version=?", (sha, card.parser_version)
    ).fetchone()
    if existing:
        return ChartIngestReport(source_filename, sha, "ALREADY_INGESTED", [], [])
    now = _now()
    if card.errors:                        # a card-level structural fault means the split into races is not trusted
        conn.execute(
            """INSERT INTO result_sources (sha256, parser_version, filename, raw_bytes, n_races, n_stored, status,
               errors_json, ingested_at) VALUES (?,?,?,?,?,0,'REJECTED',?,?)""",
            (sha, card.parser_version, source_filename, raw_bytes, len(card.races), json.dumps(card.errors), now))
        conn.commit()
        return ChartIngestReport(source_filename, sha, "REJECTED",
                                 [RaceOutcome(r.race_key, "REJECTED", problems=list(card.errors)) for r in card.races],
                                 list(card.errors))
    outcomes: list[RaceOutcome] = []
    conn.execute("SAVEPOINT chart_ingest")
    try:
        cur = conn.execute(
            """INSERT INTO result_sources (sha256, parser_version, filename, raw_bytes, n_races, n_stored, status,
               errors_json, ingested_at) VALUES (?,?,?,?,?,0,'INGESTED','[]',?)""",
            (sha, card.parser_version, source_filename, raw_bytes, len(card.races), now))
        source_id = int(cur.lastrowid)
        for race in card.races:
            if not race.valid:
                outcomes.append(RaceOutcome(race.race_key, "REJECTED", problems=list(race.problems)))
                continue
            ch = race_content_hash(race)
            current = conn.execute(
                "SELECT result_race_id, content_hash FROM result_races WHERE race_key=? AND is_current=1", (race.race_key,)
            ).fetchone()
            if current and current[1] == ch:
                outcomes.append(RaceOutcome(race.race_key, "UNCHANGED", int(current[0])))
                continue
            if current:
                conn.execute("UPDATE result_races SET is_current=0 WHERE result_race_id=?", (current[0],))
            rid = _insert_race(conn, source_id, race, ch, now)
            outcomes.append(RaceOutcome(race.race_key, "SUPERSEDED" if current else "INSERTED", rid))
        stored = sum(1 for o in outcomes if o.status in ("INSERTED", "SUPERSEDED"))
        rejected = sum(1 for o in outcomes if o.status == "REJECTED")
        status = "PARTIAL" if rejected else "INGESTED"
        conn.execute("UPDATE result_sources SET n_stored=?, status=?, errors_json=? WHERE source_id=?",
                     (stored, status, json.dumps([f"{o.race_key}: {'; '.join(o.problems)}" for o in outcomes if o.problems]),
                      source_id))
        conn.execute("RELEASE SAVEPOINT chart_ingest")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT chart_ingest")
        conn.execute("RELEASE SAVEPOINT chart_ingest")
        raise
    conn.commit()
    return ChartIngestReport(source_filename, sha, status, outcomes, [])


def ingest_chart_pdf(conn: sqlite3.Connection, path: str | Path) -> ChartIngestReport:
    raw = Path(path).read_bytes()
    return ingest_chart_card(conn, parse_chart_pdf(raw), source_filename=Path(path).name, raw_bytes=raw)


# ---- load back -----------------------------------------------------------------------------------
def load_chart_race(conn: sqlite3.Connection, result_race_id: int) -> ChartRace:
    r = conn.execute("SELECT * FROM result_races WHERE result_race_id=?", (result_race_id,))
    cols = [d[0] for d in r.description]
    row = dict(zip(cols, r.fetchone()))
    race = ChartRace(
        track_name=row["track_code"], track_code=row["track_code"], race_date=date.fromisoformat(row["race_date"]),
        race_number=row["race_number"], race_type=row["race_type"], conditions=row["conditions"],
        distance_text=row["distance_text"], distance_furlongs=row["distance_furlongs"], surface=row["surface"],
        purse=row["purse"], value_of_race=row["value_of_race"],
        purse_by_place={int(k): v for k, v in json.loads(row["purse_by_place_json"]).items()},
        weather=row["weather"], track_condition=row["track_condition"], off_time=row["off_time"],
        start_comment=row["start_comment"], timing_method=row["timing_method"],
        fractional_times=json.loads(row["fractional_times_json"]), final_time=row["final_time"],
        final_time_seconds=row["final_time_seconds"], winner_name=row["winner_name"],
        win_pool_total=row["win_pool_total"], flags=json.loads(row["flags_json"]),
    )
    for s in conn.execute(
        """SELECT finish_order, program, horse_name, jockey, weight, medication_equipment, post_position, last_raced,
                  calls_raw, odds_to_one, favorite, comment, run_order, disqualified, placed_from, trainer, owner,
                  win_payoff, place_payoff, show_payoff
           FROM result_starters WHERE result_race_id=? ORDER BY finish_order""", (result_race_id,)):
        race.starters.append(ChartStarter(
            finish_order=s[0], program=s[1], horse_name=s[2], jockey=s[3], weight=s[4], medication_equipment=s[5],
            post_position=s[6], last_raced=s[7], calls_raw=s[8], odds_to_one=s[9], favorite=bool(s[10]), comment=s[11],
            run_order=s[12], disqualified=bool(s[13]), placed_from=s[14], trainer=s[15], owner=s[16],
            win_payoff=s[17], place_payoff=s[18], show_payoff=s[19]))
    for x in conn.execute("SELECT horse_name, reason, also_eligible FROM result_scratches WHERE result_race_id=? ORDER BY scratch_id", (result_race_id,)):
        race.scratches.append(ChartScratch(x[0], x[1], bool(x[2])))
    for p in conn.execute(
        """SELECT wager_base, wager_type, winning_numbers, payoff, pool, carryover, raw, parsed
           FROM result_payoffs WHERE result_race_id=? ORDER BY seq""", (result_race_id,)):
        race.payoffs.append(ChartPayoff(p[0], p[1], p[2], p[3], p[4], p[5], p[6], bool(p[7])))
    return race


# ---- reconciliation with the pre-race card -------------------------------------------------------
_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def _person_tokens(name: str | None) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", (name or "").lower()))
    return {t for t in tokens - _SUFFIX if len(t) > 1}


def person_matches(chart_name: str | None, card_name: str | None) -> bool:
    """``"Ortiz, Jr., Irad"`` == ``"Irad Ortiz, Jr."``; initials are ignored, a missing first name still matches."""
    a, b = _person_tokens(chart_name), _person_tokens(card_name)
    return bool(a and b and (a == b or a <= b or b <= a))


@dataclasses.dataclass
class Reconciliation:
    result_race_id: int | None
    card_id: int | None
    status: str                                   # MATCHED | MISMATCH | NO_CARD
    errors: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    warnings: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def codes(self, kind: str = "warnings") -> list[str]:
        return [x["code"] for x in getattr(self, kind)]


def reconcile_race_to_card(conn: sqlite3.Connection, race: ChartRace, result_race_id: int | None = None) -> Reconciliation:
    """Join a chart to the pre-race card for the same race key and compare them runner by runner.

    Errors mean the card and the chart describe different fields (the join cannot be trusted); warnings are
    real-world changes after the card was captured (late scratch, jockey change, surface change)."""
    card_id = find_race_card(conn, race.track_code or "", race.race_date.isoformat(), race.race_number)
    if card_id is None:
        return Reconciliation(result_race_id, None, "NO_CARD")
    out = Reconciliation(result_race_id, card_id, "MATCHED")

    def err(code: str, program: str | None, detail: str) -> None:
        out.errors.append({"code": code, "program": program, "detail": detail})

    def warn(code: str, program: str | None, detail: str) -> None:
        out.warnings.append({"code": code, "program": program, "detail": detail})

    rows = conn.execute(
        """SELECT e.entry_id, e.program_number, e.post_position, e.scratch_flag, e.weight, h.name,
                  jo.full_name, tr.full_name
           FROM entries e JOIN horses h ON h.horse_id=e.horse_id
           LEFT JOIN people jo ON jo.person_id=e.jockey_id LEFT JOIN people tr ON tr.person_id=e.trainer_id
           WHERE e.card_id=?""", (card_id,)).fetchall()
    entries = {(str(r[1] or "").upper()): r for r in rows}
    chart = {s.program.upper(): s for s in race.starters}
    scratch_by_key = {_horse_key(x.horse_name): x for x in race.scratches}

    for prog, e in entries.items():
        _eid, _p, post, scratched, weight, name, jockey, trainer = e
        s = chart.get(prog)
        if not scratched and s is None:
            sc = scratch_by_key.get(_horse_key(name))
            if sc is not None:
                warn("LATE_SCRATCH", prog, f"{name} was active on the card; chart scratch: {sc.reason}")
            else:
                err("CARD_RUNNER_NOT_IN_CHART", prog, f"{name} is active on the card but neither ran nor was scratched in the chart")
            continue
        if s is None:
            continue
        if scratched:
            err("CARD_SCRATCHED_BUT_RAN", prog, f"{name} is scratched on the card but ran (finished {s.finish_order})")
            continue
        if _horse_key(name) != _horse_key(s.horse_name):
            err("NAME_MISMATCH", prog, f"card {name!r} vs chart {s.horse_name!r}")
        if post is not None and s.post_position is not None and post != s.post_position:
            warn("POST_MISMATCH", prog, f"card post {post} vs chart post {s.post_position}")
        if weight and s.weight and abs(int(weight) - s.weight) >= 1:
            warn("WEIGHT_CHANGE", prog, f"card {weight} vs chart {s.weight}")
        if jockey and not person_matches(s.jockey, jockey):
            warn("JOCKEY_CHANGE", prog, f"card {jockey!r} vs chart {s.jockey!r}")
        if trainer and not person_matches(s.trainer, trainer):
            warn("TRAINER_CHANGE", prog, f"card {trainer!r} vs chart {s.trainer!r}")
    for prog, s in chart.items():
        if prog not in entries:
            err("CHART_STARTER_NOT_ON_CARD", prog, f"{s.horse_name} ran but is not on the card")

    race_row = conn.execute("SELECT distance_yards, surface FROM race_cards WHERE card_id=?", (card_id,)).fetchone()
    if race_row:
        card_f = (race_row[0] or 0) / 220.0
        if race.distance_furlongs and card_f and abs(card_f - race.distance_furlongs) > 0.1:
            warn("DISTANCE_CHANGE", None, f"card {card_f:.2f} f vs chart {race.distance_furlongs} f")
        if race.surface and race_row[1] and race.surface != race_row[1]:
            warn("SURFACE_CHANGE", None, f"card {race_row[1]} vs chart {race.surface}")
    if out.errors:
        out.status = "MISMATCH"
    return out


def store_reconciliation(conn: sqlite3.Connection, rec: Reconciliation) -> None:
    conn.execute(
        """INSERT INTO result_card_reconciliations (result_race_id, card_id, status, errors_json, warnings_json, checked_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(result_race_id) DO UPDATE SET card_id=excluded.card_id, status=excluded.status,
             errors_json=excluded.errors_json, warnings_json=excluded.warnings_json, checked_at=excluded.checked_at""",
        (rec.result_race_id, rec.card_id, rec.status, json.dumps(rec.errors), json.dumps(rec.warnings), _now()))
    conn.commit()


def reconcile_all(conn: sqlite3.Connection) -> list[Reconciliation]:
    """(Re)join every current chart race to its pre-race card, if one exists now."""
    ensure_chart_result_tables(conn)
    out = []
    for (rid,) in conn.execute("SELECT result_race_id FROM result_races WHERE is_current=1 ORDER BY race_date, race_key").fetchall():
        rec = reconcile_race_to_card(conn, load_chart_race(conn, rid), rid)
        store_reconciliation(conn, rec)
        out.append(rec)
    return out


# ---- feed the existing grading table -------------------------------------------------------------
def populate_race_results(
    conn: sqlite3.Connection, result_race_id: int, *, replace: bool = False,
) -> dict[str, Any]:
    """Write a MATCHED chart into ``race_results`` (the table score-run grading reads).

    * ``official_odds_decimal`` is decimal odds including the stake (odds-to-one + 1), the unit that table uses.
    * ``finish_position`` / ``official_finish`` are the OFFICIAL place; a horse placed down by the stewards keeps its
      official place and has ``is_disqualified = 1``.
    * A horse active on the card but scratched late gets a row with ``is_scratched = 1``; the card itself is not
      changed, so the pre-race snapshot stays what it was.
    Refuses unless the reconciliation is MATCHED; never overwrites existing rows unless ``replace``."""
    from src.services.results_intake import _ensure_table

    _ensure_table(conn)
    race = load_chart_race(conn, result_race_id)
    rec = reconcile_race_to_card(conn, race, result_race_id)
    store_reconciliation(conn, rec)
    if rec.status != "MATCHED":
        return {"populated": 0, "status": rec.status, "reason": "no matching card" if rec.status == "NO_CARD"
                else "reconciliation errors: " + "; ".join(e["code"] for e in rec.errors)}
    card_id = rec.card_id
    rows = conn.execute(
        "SELECT e.entry_id, e.horse_id, e.program_number, e.post_position, e.scratch_flag FROM entries e WHERE e.card_id=?",
        (card_id,)).fetchall()
    chart = {s.program.upper(): s for s in race.starters}
    late = {c["program"] for c in rec.warnings if c["code"] == "LATE_SCRATCH"}
    planned = []
    for entry_id, horse_id, program, post, scratched in rows:
        prog = str(program or "").upper()
        if scratched:
            continue
        if prog in chart:
            s = chart[prog]
            dec = s.decimal_odds
            am = int(round((dec - 1.0) * 100)) if dec >= 2.0 else int(round(-100.0 / (dec - 1.0)))
            planned.append((card_id, entry_id, horse_id, s.post_position or post, s.finish_order, s.finish_order, 0,
                            int(s.disqualified), dec, am, None, None, None,
                            race.final_time if s.finish_order == 1 else None,
                            race.purse_by_place.get(s.finish_order), s.comment))
        elif prog in late:
            planned.append((card_id, entry_id, horse_id, post, None, None, 1, 0, None, None, None, None, None, None, None,
                            "late scratch (chart)"))
    have = conn.execute("SELECT COUNT(*) FROM race_results WHERE card_id=?", (card_id,)).fetchone()[0]
    if have and not replace:
        same = conn.execute(
            "SELECT entry_id, finish_position FROM race_results WHERE card_id=? AND is_scratched=0", (card_id,)).fetchall()
        wanted = {(p[1], p[4]) for p in planned if p[6] == 0}
        if {(a, b) for a, b in same} == wanted:
            return {"populated": 0, "status": "UNCHANGED", "reason": "race_results already holds the same finish order"}
        return {"populated": 0, "status": "EXISTING_DIFFERS",
                "reason": f"race_results already has {have} row(s) for card {card_id} that differ; pass replace=True to overwrite"}
    if have:
        conn.execute("DELETE FROM race_results WHERE card_id=?", (card_id,))
    now = _now()
    conn.executemany(
        """INSERT INTO race_results (card_id, entry_id, horse_id, post_position, finish_position, official_finish,
           is_scratched, is_disqualified, official_odds_decimal, official_odds_american, beaten_lengths, speed_figure,
           beyer_figure, final_time, earned_purse, comment, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [p + (now,) for p in planned])
    conn.commit()
    return {"populated": len(planned), "status": "POPULATED", "card_id": card_id,
            "late_scratches": sorted(late), "reason": ""}
