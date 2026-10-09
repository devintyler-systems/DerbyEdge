"""P2: persist validated charts, join them to pre-race cards, feed race_results."""
from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.ingest.equibase_chart import ChartRace, extract_chart_text, parse_chart_pdf, parse_chart_text
from src.services.chart_results_intake import (
    ensure_chart_result_tables, ingest_chart_card, ingest_chart_pdf, load_chart_race, person_matches,
    populate_race_results, reconcile_all, reconcile_race_to_card, race_content_hash,
)

ROOT = Path(__file__).resolve().parents[1]
PDFS = sorted((ROOT / "data" / "raw" / "historical_results").glob("2026/04/*/eqb_CD_*_fullcard.pdf"))
D25, D28 = PDFS[0], PDFS[2]


@pytest.fixture(scope="module")
def parsed():
    return {p.name: parse_chart_pdf(p) for p in PDFS}


@pytest.fixture(scope="module")
def texts():
    return {p.name: extract_chart_text(p.read_bytes()) for p in PDFS}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("\n".join(
        l for l in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in l))
    ensure_chart_result_tables(conn)
    return conn


def _race(parsed, name: str, number: int) -> ChartRace:
    return next(r for r in parsed[name].races if r.race_number == number)


def _dk_name(chart_name: str) -> str:
    parts = [p.strip() for p in chart_name.split(",")]
    return " ".join([parts[-1], parts[0]] + parts[1:-1]) if len(parts) > 1 else chart_name


def _make_card(conn, race: ChartRace, *, extra_active: tuple[str, str] | None = None, mutate=None) -> int:
    """A pre-race card equal to the chart's field (as DraftKings would have it), optionally altered."""
    track_id = conn.execute("SELECT track_id FROM tracks WHERE abbrev='CD'").fetchone()
    if track_id is None:
        conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('Churchill Downs','CD')")
        track_id = conn.execute("SELECT track_id FROM tracks WHERE abbrev='CD'").fetchone()
    conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size) VALUES (?,?,?,?,?,?)",
        (track_id[0], race.race_date.isoformat(), race.race_number, int(race.distance_furlongs * 220), race.surface, len(race.starters)))
    card_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def person(name: str, role: str) -> int:
        conn.execute("INSERT OR IGNORE INTO people (full_name, role) VALUES (?, ?)", (name, role))
        return conn.execute("SELECT person_id FROM people WHERE full_name=? AND role=?", (name, role)).fetchone()[0]

    rows = [(s.program, s.horse_name, s.post_position, s.weight, _dk_name(s.jockey), _dk_name(s.trainer or "")) for s in race.starters]
    if extra_active:
        rows.append((extra_active[0], extra_active[1], 20, 118, "Some Rider", "Some Trainer"))
    for prog, name, post, wt, jock, trn in rows:
        conn.execute("INSERT OR IGNORE INTO horses (name) VALUES (?)", (name,))
        hid = conn.execute("SELECT horse_id FROM horses WHERE name=?", (name,)).fetchone()[0]
        conn.execute(
            """INSERT INTO entries (card_id, horse_id, trainer_id, jockey_id, post_position, program_number, weight,
               morning_line_odds, scratch_flag) VALUES (?,?,?,?,?,?,?,?,0)""",
            (card_id, hid, person(trn, "trainer"), person(jock, "jockey"), post, prog, wt, 5.0))
    if mutate:
        mutate(conn, card_id)
    conn.commit()
    return card_id


def _ingest_one(conn, parsed, name: str) -> dict[int, int]:
    path = next(p for p in PDFS if p.name == name)
    rep = ingest_chart_card(conn, parsed[name], source_filename=name, raw_bytes=path.read_bytes())
    return {int(o.race_key.rsplit("R", 1)[1]): o.result_race_id for o in rep.outcomes if o.result_race_id}


# ---- ingest --------------------------------------------------------------------------------------

def test_three_cards_store_28_races_and_reingest_is_a_noop(parsed):
    conn = _db()
    for p in PDFS:
        rep = ingest_chart_pdf(conn, p)
        assert rep.status == "INGESTED" and rep.stored == len(parsed[p.name].races)
    assert conn.execute("SELECT COUNT(*) FROM result_races WHERE is_current=1").fetchone()[0] == 28
    assert conn.execute("SELECT COUNT(*) FROM result_starters").fetchone()[0] == sum(
        len(r.starters) for c in parsed.values() for r in c.races)
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
              ("result_sources", "result_races", "result_starters", "result_scratches", "result_payoffs")]
    assert [ingest_chart_pdf(conn, p).status for p in PDFS] == ["ALREADY_INGESTED"] * 3
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
             ("result_sources", "result_races", "result_starters", "result_scratches", "result_payoffs")]
    assert before == after


def test_provenance_raw_bytes_and_hash_are_kept(parsed):
    conn = _db()
    ingest_chart_pdf(conn, D25)
    row = conn.execute("SELECT sha256, parser_version, filename, raw_bytes FROM result_sources").fetchone()
    assert row["sha256"] == hashlib.sha256(D25.read_bytes()).hexdigest() == hashlib.sha256(bytes(row["raw_bytes"])).hexdigest()
    assert row["filename"] == D25.name and row["parser_version"].startswith("equibase_chart/")


def test_database_round_trip_preserves_every_field(parsed):
    conn = _db()
    ids = _ingest_one(conn, parsed, D25.name)
    for number, rid in ids.items():
        orig = _race(parsed, D25.name, number)
        back = load_chart_race(conn, rid)
        a, b = dataclasses.asdict(orig), dataclasses.asdict(back)
        for k in ("track_name", "problems"):
            a.pop(k), b.pop(k)
        assert a == b, number


def test_an_invalid_race_is_rejected_and_the_rest_are_stored(texts):
    t = texts[D25.name].replace("1.01* took", "3.01* took", 1)            # breaks race 2's odds/payoff identity
    raw = b"tampered-source"
    card = parse_chart_text(t, source_sha256=hashlib.sha256(raw).hexdigest())
    conn = _db()
    rep = ingest_chart_card(conn, card, source_filename="x.pdf", raw_bytes=raw)
    assert rep.status == "PARTIAL"
    bad = [o for o in rep.outcomes if o.status == "REJECTED"]
    assert [o.race_key for o in bad] == ["CD|2026-04-25|R2"] and "disagree" in bad[0].problems[0]
    assert conn.execute("SELECT COUNT(*) FROM result_races").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM result_races WHERE race_number=2").fetchone()[0] == 0


def test_a_structurally_wrong_card_stores_no_races(texts):
    t = texts[D25.name]
    block = t[t.index("CHURCHILL DOWNS - April 25, 2026 - Race 3"):t.index("CHURCHILL DOWNS - April 25, 2026 - Race 4")]
    raw = b"duplicated-race"
    card = parse_chart_text(t + "\n" + block, source_sha256=hashlib.sha256(raw).hexdigest())
    conn = _db()
    rep = ingest_chart_card(conn, card, source_filename="dup.pdf", raw_bytes=raw)
    assert rep.status == "REJECTED" and rep.stored == 0
    assert conn.execute("SELECT COUNT(*) FROM result_races").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM result_sources").fetchone()[0] == "REJECTED"


def test_a_corrected_chart_supersedes_the_old_version_and_keeps_it(texts):
    conn = _db()
    t = texts[D25.name]
    raw1, raw2 = b"first-version", b"second-version"
    first = parse_chart_text(t, source_sha256=hashlib.sha256(raw1).hexdigest())
    ingest_chart_card(conn, first, source_filename="a.pdf", raw_bytes=raw1)
    edited = t.replace("Weather: Clear, 75° Track: Fast\nOff at: 1:21", "Weather: Clear, 75° Track: Fast\nOff at: 1:22", 1)
    second = parse_chart_text(edited, source_sha256=hashlib.sha256(raw2).hexdigest())
    rep = ingest_chart_card(conn, second, source_filename="b.pdf", raw_bytes=raw2)
    status = {o.race_key.rsplit("R", 1)[1]: o.status for o in rep.outcomes}
    assert status["2"] == "SUPERSEDED" and {v for k, v in status.items() if k != "2"} == {"UNCHANGED"}
    rows = conn.execute("SELECT is_current, off_time FROM result_races WHERE race_number=2 ORDER BY result_race_id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(0, "1:21"), (1, "1:22")]
    assert race_content_hash(first.races[1]) != race_content_hash(second.races[1])


# ---- reconciliation ------------------------------------------------------------------------------

def test_no_card_is_reported_not_an_error(parsed):
    conn = _db()
    rid = _ingest_one(conn, parsed, D25.name)[2]
    rec = reconcile_race_to_card(conn, load_chart_race(conn, rid), rid)
    assert (rec.status, rec.card_id, rec.errors) == ("NO_CARD", None, [])


def test_a_card_equal_to_the_chart_field_matches_cleanly(parsed):
    conn = _db()
    race = _race(parsed, D25.name, 2)
    _make_card(conn, race)
    rec = reconcile_race_to_card(conn, race)
    assert rec.status == "MATCHED" and rec.errors == [] and rec.warnings == [], rec.warnings


def test_a_horse_active_on_the_card_but_scratched_in_the_chart_is_a_late_scratch_warning(parsed):
    conn = _db()
    race = _race(parsed, D25.name, 2)                                        # chart scratch: Be My Pal
    _make_card(conn, race, extra_active=("9", "Be My Pal"))
    rec = reconcile_race_to_card(conn, race)
    assert rec.status == "MATCHED"
    assert [(w["code"], w["program"]) for w in rec.warnings] == [("LATE_SCRATCH", "9")]
    assert "PrivVet-Illness" in rec.warnings[0]["detail"]


def test_card_and_chart_that_describe_different_fields_are_a_mismatch(parsed):
    race = _race(parsed, D25.name, 2)

    def case(mutate=None, extra=None):
        conn = _db()
        _make_card(conn, race, extra_active=extra, mutate=mutate)
        return reconcile_race_to_card(conn, race)

    # a runner on the card that neither ran nor was scratched in the chart
    rec = case(extra=("9", "Ghost Horse"))
    assert rec.status == "MISMATCH" and rec.codes("errors") == ["CARD_RUNNER_NOT_IN_CHART"]
    # a chart starter the card does not have
    rec = case(mutate=lambda c, cid: c.execute("DELETE FROM entries WHERE card_id=? AND program_number='7'", (cid,)))
    assert rec.codes("errors") == ["CHART_STARTER_NOT_ON_CARD"]
    # the card has a runner scratched that actually ran
    rec = case(mutate=lambda c, cid: c.execute("UPDATE entries SET scratch_flag=1 WHERE card_id=? AND program_number='7'", (cid,)))
    assert rec.codes("errors") == ["CARD_SCRATCHED_BUT_RAN"]
    # same program, different horse
    rec = case(mutate=lambda c, cid: c.execute(
        "UPDATE horses SET name='Somebody Else' WHERE horse_id=(SELECT horse_id FROM entries WHERE card_id=? AND program_number='5')", (cid,)))
    assert rec.codes("errors") == ["NAME_MISMATCH"]


def test_real_world_changes_since_the_card_are_warnings(parsed):
    race = _race(parsed, D25.name, 2)
    conn = _db()

    def mutate(c, cid):
        c.execute("UPDATE entries SET weight=122 WHERE card_id=? AND program_number='1'", (cid,))
        c.execute("UPDATE entries SET post_position=9 WHERE card_id=? AND program_number='5'", (cid,))
        pid = c.execute("INSERT INTO people (full_name, role) VALUES ('Someone New', 'jockey')").lastrowid
        c.execute("UPDATE entries SET jockey_id=? WHERE card_id=? AND program_number='7'", (pid, cid))
        c.execute("UPDATE race_cards SET surface='turf', distance_yards=? WHERE card_id=?", (int(6 * 220), cid))

    _make_card(conn, race, mutate=mutate)
    rec = reconcile_race_to_card(conn, race)
    assert rec.status == "MATCHED"
    assert sorted(rec.codes()) == ["DISTANCE_CHANGE", "JOCKEY_CHANGE", "POST_MISMATCH", "SURFACE_CHANGE", "WEIGHT_CHANGE"]


@pytest.mark.parametrize("chart,card,ok", [
    ("Ortiz, Jose", "Jose Ortiz", True), ("Ortiz, Jr., Irad", "Irad Ortiz, Jr.", True),
    ("Ortiz, Jr., Irad", "Irad Ortiz Jr", True), ("Zayas, Edgard", "Edgard J. Zayas", True),
    ("Kenneally, Eddie", "Eddie Kenneally", True), ("Ortiz, Jose", "Irad Ortiz, Jr.", False),
    ("Saez, Luis", "Gabriel Saez", False), (None, "Jose Ortiz", False),
])
def test_person_matching(chart, card, ok):
    assert person_matches(chart, card) is ok


def test_reconcile_all_stores_a_row_per_race_and_picks_up_a_card_added_later(parsed):
    conn = _db()
    ids = _ingest_one(conn, parsed, D25.name)
    assert {r.status for r in reconcile_all(conn)} == {"NO_CARD"}
    _make_card(conn, _race(parsed, D25.name, 2))
    recs = {r.result_race_id: r for r in reconcile_all(conn)}
    assert recs[ids[2]].status == "MATCHED" and sum(1 for r in recs.values() if r.status == "NO_CARD") == 9
    assert conn.execute("SELECT COUNT(*) FROM result_card_reconciliations").fetchone()[0] == 10
    assert conn.execute("SELECT status FROM result_card_reconciliations WHERE result_race_id=?", (ids[2],)).fetchone()[0] == "MATCHED"


# ---- race_results -------------------------------------------------------------------------------

def test_populate_writes_official_results_in_the_units_the_grader_expects(parsed):
    conn = _db()
    race = _race(parsed, D25.name, 2)
    card_id = _make_card(conn, race, extra_active=("9", "Be My Pal"))
    rid = _ingest_one(conn, parsed, D25.name)[2]
    res = populate_race_results(conn, rid)
    assert res["status"] == "POPULATED" and res["populated"] == 8 and res["late_scratches"] == ["9"]
    rows = {r["program_number"]: r for r in conn.execute(
        """SELECT e.program_number, rr.* FROM race_results rr JOIN entries e ON e.entry_id=rr.entry_id
           WHERE rr.card_id=?""", (card_id,))}
    w = rows["1"]
    assert (w["finish_position"], w["official_finish"], w["is_scratched"], w["is_disqualified"]) == (1, 1, 0, 0)
    assert w["official_odds_decimal"] == pytest.approx(2.01)             # 1.01-1 -> 2.01 incl. stake == $2 payoff / 2
    assert w["official_odds_american"] == 101 and w["final_time"] == "51.50" and w["earned_purse"] == 52096
    assert rows["7"]["earned_purse"] == 9200 and rows["6"]["earned_purse"] == 1164
    late = rows["9"]
    assert (late["is_scratched"], late["finish_position"], late["official_odds_decimal"]) == (1, None, None)
    # the pre-race card is untouched
    assert conn.execute("SELECT scratch_flag FROM entries WHERE card_id=? AND program_number='9'", (card_id,)).fetchone()[0] == 0


def test_populate_is_idempotent_and_never_silently_overwrites(parsed):
    conn = _db()
    card_id = _make_card(conn, _race(parsed, D25.name, 2))
    rid = _ingest_one(conn, parsed, D25.name)[2]
    assert populate_race_results(conn, rid)["status"] == "POPULATED"
    assert populate_race_results(conn, rid)["status"] == "UNCHANGED"
    conn.execute("UPDATE race_results SET finish_position=2 WHERE card_id=? AND finish_position=1", (card_id,))
    differs = populate_race_results(conn, rid)
    assert differs["status"] == "EXISTING_DIFFERS" and differs["populated"] == 0
    assert populate_race_results(conn, rid, replace=True)["status"] == "POPULATED"
    assert conn.execute("SELECT COUNT(*) FROM race_results WHERE card_id=?", (card_id,)).fetchone()[0] == 7


def test_populate_refuses_without_a_card_or_on_a_mismatch(parsed):
    conn = _db()
    rid = _ingest_one(conn, parsed, D25.name)[2]
    assert populate_race_results(conn, rid)["status"] == "NO_CARD"
    _make_card(conn, _race(parsed, D25.name, 2), extra_active=("9", "Ghost Horse"))
    res = populate_race_results(conn, rid)
    assert res["status"] == "MISMATCH" and res["populated"] == 0 and "CARD_RUNNER_NOT_IN_CHART" in res["reason"]
    assert conn.execute("SELECT COUNT(*) FROM race_results").fetchone()[0] == 0


def test_a_disqualified_horse_keeps_its_official_place_and_the_flag(parsed):
    conn = _db()
    race = _race(parsed, D28.name, 8)
    card_id = _make_card(conn, race)
    rid = _ingest_one(conn, parsed, D28.name)[8]
    assert populate_race_results(conn, rid)["status"] == "POPULATED"
    rows = {r["program_number"]: r for r in conn.execute(
        "SELECT e.program_number, rr.* FROM race_results rr JOIN entries e ON e.entry_id=rr.entry_id WHERE rr.card_id=?", (card_id,))}
    assert [rows[p]["official_finish"] for p in ("7", "5", "4", "1")] == [1, 2, 3, 4]
    assert (rows["1"]["is_disqualified"], rows["4"]["is_disqualified"]) == (1, 0)
    assert rows["4"]["official_odds_decimal"] == pytest.approx(6.98)         # Furio 5.98-1, third officially


# ---- the command line gate -----------------------------------------------------------------------

def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "ingest_results_charts.py"), *args],
                          capture_output=True, text=True, timeout=300)


def test_cli_check_passes_on_the_retained_cards():
    done = _cli("--root", str(ROOT / "data" / "raw" / "historical_results"), "--check")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "10 races, 10 valid" in done.stdout and "OK (0 problem(s))" in done.stdout


def test_cli_check_fails_on_an_unreadable_chart(tmp_path):
    (tmp_path / "eqb_ZZ_2026-01-01_fullcard.pdf").write_bytes(b"this is not a pdf")
    done = _cli("--root", str(tmp_path), "--check")
    assert done.returncode == 1 and "UNREADABLE" in done.stdout and "FAIL" in done.stdout


def test_cli_check_fails_when_the_folder_has_no_charts(tmp_path):
    assert _cli("--root", str(tmp_path), "--check").returncode == 1
