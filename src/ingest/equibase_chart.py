"""Equibase full-card result chart parser (one PDF, many races).

Turns an ``eqb_<TRK>_<date>_fullcard.pdf`` chart into one structured record per race:
conditions, finish order with final odds, scratches (with reasons, including
also-eligibles), win/place/show payoffs and the exotic-pool rows.

Nothing here touches a database, a model or a card.  Every race carries a list of
``problems`` produced by independent self-checks; a race with any problem is
``valid == False`` and must not be ingested.  The checks are:

* the first finish row is the chart's own ``Winner:`` horse;
* the winner's final odds equal its $2 win payoff / 2 - 1 (to a cent);
* the win/place/show payoff table lists the first three finishers, in order;
* program numbers are unique and every starter has a trainer;
* the distance, surface, final time and fractional times parse.

Units: ``odds_to_one`` is Equibase's printed final odds (4.79 means 4.79-1);
``decimal_odds`` is the return per $1 including the stake (payoff / 2), so
``decimal_odds == odds_to_one + 1``.

Text is extracted with a tight ``x_tolerance`` so horse names keep their spaces.
Mid-race position calls are glued to their lengths in the source ("131/4"), so they
are kept as raw text only and are not interpreted.
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.derbyedge.tracks import resolve_track

PARSER_VERSION = "equibase_chart/1.0.0"
X_TOLERANCE = 1.5

_HEADER = re.compile(r"^(?P<track>[A-Z][A-Z .'&-]+?)\s+-\s+(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})\s+-\s+Race\s+(?P<race>\d+)\s*$")
_ROW = re.compile(
    r"^(?P<last>---|\d{1,2}[A-Za-z]{3}\d{2}\s+(?:\d{1,2})?[A-Z]{1,4}\d{1,2})\s+"
    r"(?P<pgm>\d{1,2}[A-Z]?)\s+(?P<name>.+?)\s*\((?P<jockey>[^)]*)\)\s+(?P<wgt>\d{2,3})\s+(?P<rest>.*)$"
)
_ODDS = re.compile(r"(?:(?<=\s)|^)(?P<odds>\d{1,3}\.\d\d)(?P<fav>\*?)(?=\s|$)")
_ME_TOKEN = re.compile(r"^(?:-|[A-Za-z]{1,4})$")
_MONEY = r"\d{1,3}(?:,\d{3})*\.\d\d|\d+\.\d\d"
_PAYOFF_LEFT = re.compile(
    rf"^(?P<pgm>\d{{1,2}}[A-Z]?)\s+(?P<name>.+?)\s+(?P<m1>{_MONEY})(?:\s+(?P<m2>{_MONEY}))?(?:\s+(?P<m3>{_MONEY}))?(?=\s+\$|\s*$)"
)
_WAGER = re.compile(
    rf"(?P<base>\$\d+(?:\.\d\d)?)\s*(?P<type>[A-Za-z][A-Za-z0-9 ]*?)\s+"
    rf"(?P<nums>\d{{1,2}}[A-Z]?(?:[/-]\d{{1,2}}[A-Z]?)*|EVEN|ODD)(?:\s+\((?P<note>[^)]*)\))?\s+"
    rf"(?P<payoff>{_MONEY})\s+(?P<pool>\d{{1,3}}(?:,\d{{3}})*)(?:\s+(?P<carry>\d{{1,3}}(?:,\d{{3}})*))?\s*$"
)

# ---- distance in words -> furlongs ------------------------------------------------------------
_CARD = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_FRAC = {"half": 2, "halves": 2, "quarter": 4, "quarters": 4, "fourth": 4, "fourths": 4, "eighth": 8, "eighths": 8,
         "sixteenth": 16, "sixteenths": 16, "third": 3, "thirds": 3}
_UNIT = {"furlong": 1.0, "furlongs": 1.0, "mile": 8.0, "miles": 8.0, "yard": 1 / 220, "yards": 1 / 220}


def distance_to_furlongs(words: str) -> float | None:
    """``"One And One Sixteenth Miles"`` -> 9.0.  None if any word is not understood."""
    total = 0.0
    whole = 0.0
    pending: float | None = None
    frac = 0.0
    seen_unit = False
    last_was_tens = False
    for raw in re.findall(r"[A-Za-z]+", words.lower()):
        if raw in ("and", "about"):
            continue
        if raw in _TENS:
            if pending is not None:
                whole += pending
            pending, last_was_tens = float(_TENS[raw]), True
        elif raw in _CARD:
            if pending is not None and last_was_tens and _CARD[raw] < 10:
                pending += _CARD[raw]
            else:
                if pending is not None:
                    whole += pending
                pending = float(_CARD[raw])
            last_was_tens = False
        elif raw in _FRAC:
            if pending is None:
                return None
            frac += pending / _FRAC[raw]
            pending, last_was_tens = None, False
        elif raw in _UNIT:
            value = whole + (pending or 0.0) + frac
            if value == 0:
                return None
            total += value * _UNIT[raw]
            whole, pending, frac, last_was_tens, seen_unit = 0.0, None, 0.0, False, True
        else:
            return None
    if pending is not None or whole or frac:
        return None
    return round(total, 4) if seen_unit else None


def _surface(text: str) -> str | None:
    t = text.lower()
    if "turf" in t:
        return "turf"
    if any(w in t for w in ("dirt", "mud")):
        return "dirt"
    if any(w in t for w in ("tapeta", "polytrack", "synthetic", "all weather", "all-weather")):
        return "all_weather"
    return None


def _seconds(text: str | None) -> float | None:
    if not text:
        return None
    m = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", text.strip())
    if not m:
        return None
    return round((int(m.group(1)) * 60 if m.group(1) else 0) + float(m.group(2)), 2)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _money(text: str | None) -> float | None:
    return float(text.replace(",", "")) if text else None


# ---- records -----------------------------------------------------------------------------------
@dataclasses.dataclass
class ChartStarter:
    finish_order: int
    program: str
    horse_name: str
    jockey: str
    weight: int
    medication_equipment: str | None
    post_position: int | None
    last_raced: str | None
    calls_raw: str
    odds_to_one: float
    favorite: bool
    comment: str
    run_order: int = 0                    # order they crossed the line; finish_order is the OFFICIAL order
    disqualified: bool = False
    placed_from: int | None = None        # as-run position before a stewards' change
    trainer: str | None = None
    owner: str | None = None
    win_payoff: float | None = None
    place_payoff: float | None = None
    show_payoff: float | None = None

    @property
    def decimal_odds(self) -> float:
        return round(self.odds_to_one + 1.0, 4)


@dataclasses.dataclass
class ChartScratch:
    horse_name: str
    reason: str
    also_eligible: bool


@dataclasses.dataclass
class ChartPayoff:
    wager_base: str
    wager_type: str | None
    winning_numbers: str | None
    payoff: float | None
    pool: int | None
    carryover: int | None
    raw: str
    parsed: bool


@dataclasses.dataclass
class ChartRace:
    track_name: str
    track_code: str | None
    race_date: date | None
    race_number: int
    race_type: str | None = None
    conditions: str | None = None
    distance_text: str | None = None
    distance_furlongs: float | None = None
    surface: str | None = None
    purse: int | None = None
    value_of_race: int | None = None
    purse_by_place: dict[int, int] = dataclasses.field(default_factory=dict)      # official place -> earnings
    weather: str | None = None
    track_condition: str | None = None
    off_time: str | None = None
    start_comment: str | None = None
    timing_method: str | None = None
    fractional_times: list[str] = dataclasses.field(default_factory=list)
    final_time: str | None = None
    final_time_seconds: float | None = None
    winner_name: str | None = None
    win_pool_total: int | None = None
    starters: list[ChartStarter] = dataclasses.field(default_factory=list)
    scratches: list[ChartScratch] = dataclasses.field(default_factory=list)
    payoffs: list[ChartPayoff] = dataclasses.field(default_factory=list)
    flags: list[str] = dataclasses.field(default_factory=list)       # DEAD_HEAT, ORDER_CHANGED_BY_STEWARDS ...
    problems: list[str] = dataclasses.field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems

    @property
    def race_key(self) -> str:
        return f"{self.track_code or self.track_name}|{self.race_date.isoformat() if self.race_date else '?'}|R{self.race_number}"


@dataclasses.dataclass
class ChartCard:
    source_sha256: str
    parser_version: str
    races: list[ChartRace]
    errors: list[str]

    @property
    def valid(self) -> bool:
        return not self.errors and all(r.valid for r in self.races)


# ---- text extraction ----------------------------------------------------------------------------
def extract_chart_text(pdf_bytes: bytes) -> str:
    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text(x_tolerance=X_TOLERANCE) or "")
    return "\n".join(pages)


# ---- one race block -----------------------------------------------------------------------------
def _section(lines: list[str], start: int, stop_prefixes: tuple[str, ...]) -> str:
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith(stop_prefixes):
            break
        out.append(line)
    return " ".join(out)


def _find(lines: list[str], prefix: str) -> int | None:
    return next((i for i, line in enumerate(lines) if line.startswith(prefix)), None)


def _parse_row(line: str, order: int) -> ChartStarter | None:
    m = _ROW.match(line.strip())
    if not m:
        return None
    rest = m.group("rest")
    odds = _ODDS.search(rest)
    if not odds:
        return None
    mid = rest[:odds.start()].split()
    me: list[str] = []
    while mid and _ME_TOKEN.match(mid[0]):
        me.append(mid.pop(0))
    post = int(mid.pop(0)) if mid and mid[0].isdigit() else None
    name = m.group("name").strip()
    dq = name.startswith("DQ-")
    if dq:
        name = name[3:].strip()
    return ChartStarter(
        finish_order=order, run_order=order, disqualified=dq, program=m.group("pgm"), horse_name=name,
        jockey=m.group("jockey").strip(), weight=int(m.group("wgt")),
        medication_equipment="".join(t for t in me if t != "-") or None, post_position=post,
        last_raced=None if m.group("last") == "---" else m.group("last"),
        calls_raw=" ".join(mid), odds_to_one=float(odds.group("odds")), favorite=bool(odds.group("fav")),
        comment=rest[odds.end():].strip(),
    )


def _parse_scratches(lines: list[str]) -> list[ChartScratch]:
    i = _find(lines, "Scratched Horse(s):")
    if i is None:
        return []
    text = _section(lines, i, ("Total WPS Pool", "Pgm Horse"))[len("Scratched Horse(s):"):].strip()
    out = []
    for m in re.finditer(r"(?P<name>[^,()]+?)\s*\((?P<reason>[^)]*)\)", text):
        reason = m.group("reason").strip()
        out.append(ChartScratch(m.group("name").strip(" ,"), reason, "also-eligible" in reason.lower()))
    return out


def _apply_disqualifications(race: ChartRace, lines: list[str]) -> None:
    """Rows are in as-run order; ``Disqualification(s): # 1 This Is Uscar from 3 to 4`` moves
    horses to their official place.  The official order is what the payoff table follows."""
    i = _find(lines, "Disqualification(s):")
    if i is None:
        if any(s.disqualified for s in race.starters):
            race.problems.append("a DQ- row has no Disqualification(s) line")
        return
    text = _section(lines, i, ("Scratched Horse", "Total WPS Pool", "Pgm Horse", "Claiming Prices"))
    moves = re.findall(r"#\s*(\d{1,2}[A-Z]?)\s+(.+?)\s+from\s+(\d+)\s+to\s+(\d+)", text)
    if not moves:
        race.problems.append(f"Disqualification line not understood: {text[:80]!r}")
        return
    race.flags.append("ORDER_CHANGED_BY_STEWARDS")
    order = list(race.starters)
    for program, _name, frm, to in moves:
        horse = next((s for s in order if s.program == program), None)
        if horse is None:
            race.problems.append(f"Disqualification names unknown program {program}")
            continue
        if horse.run_order != int(frm):
            race.problems.append(f"program {program} was {horse.run_order} as run, DQ line says {frm}")
        order.remove(horse)
        order.insert(int(to) - 1, horse)
        horse.disqualified, horse.placed_from = True, int(frm)
    for n, horse in enumerate(order, start=1):
        horse.finish_order = n
    race.starters = order


def _parse_people(lines: list[str], label: str, stop: tuple[str, ...]) -> dict[str, str]:
    i = _find(lines, label)
    if i is None:
        return {}
    text = _section(lines, i, stop)[len(label):].strip()
    parts = re.split(r";\s*(?=\d{1,2}[A-Z]?\s*-)", text.strip().rstrip(";"))
    out: dict[str, str] = {}
    for part in parts:
        m = re.match(r"^(\d{1,2}[A-Z]?)\s*-\s*(.+)$", part.strip().rstrip(";"), re.S)
        if m:
            out[m.group(1)] = re.sub(r"\s+", " ", m.group(2)).strip()
    return out


def _parse_payoffs(lines: list[str], race: ChartRace) -> dict[int, ChartPayoff | None]:
    start = next((i for i, line in enumerate(lines) if line.startswith("Pgm Horse Win Place Show")), None)
    if start is None:
        race.problems.append("payoff table not found")
        return {}
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("Past Performance Running Line")), len(lines))
    left_rows: list[tuple[str, str, list[float]]] = []
    wager_lines: list[str] = []
    for line in lines[start + 1:end]:
        line = line.strip()
        if not line:
            continue
        lm = _PAYOFF_LEFT.match(line)
        right = line
        if lm:
            amounts = [_money(lm.group(k)) for k in ("m1", "m2", "m3") if lm.group(k)]
            left_rows.append((lm.group("pgm"), lm.group("name").strip(), amounts))
            right = line[lm.end():].strip()
        if right.startswith("$"):
            wager_lines.append(right)
        elif right and wager_lines:
            wager_lines[-1] += " " + right          # a wrapped wager row ("... (1\ncorrect)")
    for raw in wager_lines:
        wm = _WAGER.match(raw)
        race.payoffs.append(ChartPayoff(
            wager_base=(wm.group("base") if wm else raw.split()[0]),
            wager_type=wm.group("type").strip() if wm else None,
            winning_numbers=wm.group("nums") if wm else None,
            payoff=_money(wm.group("payoff")) if wm else None,
            pool=int(wm.group("pool").replace(",", "")) if wm else None,
            carryover=int(wm.group("carry").replace(",", "")) if wm and wm.group("carry") else None,
            raw=raw, parsed=bool(wm),
        ))
    # the left block lists finishers in order, each with the pools they paid: win/place/show, place/show, show
    by_order: dict[int, tuple[str, list[float]]] = {}
    for n, (pgm, _name, amounts) in enumerate(left_rows, start=1):
        by_order[n] = (pgm, amounts)
    return by_order  # type: ignore[return-value]


def _parse_race(header: re.Match, block: list[str]) -> ChartRace:
    track_name = header.group("track").strip()
    try:
        race_date = datetime.strptime(header.group("date"), "%B %d, %Y").date()
    except ValueError:
        race_date = None
    resolved = resolve_track(track_name=track_name)
    race = ChartRace(track_name=track_name, track_code=resolved["track_code"], race_date=race_date,
                     race_number=int(header.group("race")))
    if race.track_code is None:
        race.problems.append(f"track {track_name!r} is not in the track registry")
    if race.race_date is None:
        race.problems.append(f"unreadable race date {header.group('date')!r}")
    lines = [ln.rstrip() for ln in block if ln.strip()]

    # conditions block (everything before the finish table)
    table_at = next((i for i, ln in enumerate(lines) if ln.startswith("Last Raced Pgm")), None)
    head = lines[:table_at] if table_at is not None else lines
    glyph = re.compile(r"^[?<>\[\]*/\\|!]$")
    head = [ln for ln in head if not glyph.match(ln.strip())]
    type_line = next((ln for ln in head if ln.endswith(" - Thoroughbred") or " - Thoroughbred" in ln), None)
    race.race_type = type_line.strip() if type_line else None
    if type_line is not None:
        k = head.index(type_line) + 1
        cond = []
        while k < len(head) and not head[k].startswith(("Distance:", "Purse:")):
            cond.append(head[k])
            k += 1
        race.conditions = " ".join(cond) or None
    vi = next((i for i, ln in enumerate(head) if ln.startswith("Value of Race:")), None)
    if vi is not None:                       # the line wraps when many places are paid
        vtext = _section(head, vi, ("Weather:", "Off at:"))
        m = re.match(r"^Value of Race:\s*\$([\d,]+)", vtext)
        race.value_of_race = int(m.group(1).replace(",", "")) if m else None
        race.purse_by_place = {int(n): int(v.replace(",", ""))
                               for n, v in re.findall(r"(\d{1,2})(?:st|nd|rd|th)\s+\$([\d,]+)", vtext)}
    for ln in head:
        if ln.startswith("Distance:"):
            text = re.sub(r"\s*Current Track Record.*$", "", ln[len("Distance:"):]).strip()
            race.distance_text = text
            m = re.match(r"^(?P<d>.+?)\s+On The\s+(?P<s>.+)$", text)
            if m:
                race.distance_furlongs = distance_to_furlongs(m.group("d"))
                race.surface = _surface(m.group("s"))
        elif ln.startswith("Purse:"):
            m = re.search(r"\$([\d,]+)", ln)
            race.purse = int(m.group(1).replace(",", "")) if m else None
        elif ln.startswith("Weather:"):
            m = re.match(r"^Weather:\s*(?P<w>.*?)\s+Track:\s*(?P<t>.+)$", ln)
            if m:
                race.weather, race.track_condition = m.group("w").strip(), m.group("t").strip()
        elif ln.startswith("Off at:"):
            m = re.match(r"^Off at:\s*(?P<o>\S+)\s+Start:\s*(?P<s>.*?)\s+Timing Method:\s*(?P<t>.+)$", ln)
            if m:
                race.off_time, race.start_comment, race.timing_method = m.group("o"), m.group("s"), m.group("t").strip()

    # finish table
    if table_at is not None:
        order = 0
        for ln in lines[table_at + 1:]:
            if ln.startswith(("Fractional Times", "Winner:")):
                break
            row = _parse_row(ln, order + 1)
            if row is not None:
                order += 1
                race.starters.append(row)
            elif race.starters and not re.match(r"^(---|\d{1,2}[A-Za-z]{3}\d{2})", ln):
                race.starters[-1].comment += " " + ln.strip()      # wrapped comment
            elif ln.strip():
                race.problems.append(f"unparsed finish-table line: {ln.strip()[:80]!r}")
    else:
        race.problems.append("finish table not found")

    i = _find(lines, "Fractional Times:")
    if i is not None:
        text = lines[i]
        m = re.match(r"^Fractional Times:\s*(?P<f>.*?)\s*Final Time:\s*(?P<t>\S+)", text)
        if m:
            race.fractional_times = m.group("f").split()
            race.final_time = m.group("t")
            race.final_time_seconds = _seconds(race.final_time)
    i = _find(lines, "Winner:")
    if i is not None:
        race.winner_name = lines[i][len("Winner:"):].split(",")[0].strip()
    i = _find(lines, "Total WPS Pool:")
    if i is not None:
        m = re.search(r"\$([\d,]+)", lines[i])
        race.win_pool_total = int(m.group(1).replace(",", "")) if m else None

    race.scratches = _parse_scratches(lines)
    _apply_disqualifications(race, lines)
    trainers = _parse_people(lines, "Trainers:", ("Owners:", "Footnotes"))
    owners = _parse_people(lines, "Owners:", ("Footnotes",))
    for s in race.starters:
        s.trainer, s.owner = trainers.get(s.program), owners.get(s.program)

    by_order = _parse_payoffs(lines, race)
    for n, s in enumerate(race.starters, start=1):
        pgm_amounts = by_order.get(n)
        if pgm_amounts is None:
            continue
        pgm, amounts = pgm_amounts
        if pgm != s.program:
            race.problems.append(f"payoff table row {n} is program {pgm}, finish order says {s.program}")
            continue
        tail = {3: ("win_payoff", "place_payoff", "show_payoff"), 2: ("place_payoff", "show_payoff"),
                1: ("show_payoff",)}.get(len(amounts))
        if tail is None:
            race.problems.append(f"payoff row for program {pgm} has {len(amounts)} amounts")
            continue
        for key, value in zip(tail, amounts):
            setattr(s, key, value)

    whole = " ".join(lines)
    if re.search(r"dead heat", whole, re.I):
        race.flags.append("DEAD_HEAT")
    if "ORDER_CHANGED_BY_STEWARDS" not in race.flags and re.search(
            r"placed \w+ through disqualification|order of finish was changed", whole, re.I):
        race.flags.append("ORDER_CHANGED_BY_STEWARDS")
    _self_check(race)
    return race


def _self_check(race: ChartRace) -> None:
    p = race.problems
    if len(race.starters) < 2:
        p.append(f"only {len(race.starters)} starter row(s) parsed")
        return
    programs = [s.program for s in race.starters]
    if len(set(programs)) != len(programs):
        p.append("duplicate program number among starters")
    w = race.starters[0]
    if not race.winner_name:
        p.append("no Winner: line")
    elif _squash(race.winner_name) != _squash(w.horse_name):
        p.append(f"first finish row {w.horse_name!r} is not the chart winner {race.winner_name!r}")
    if "DEAD_HEAT" not in race.flags:
        if w.win_payoff is None:
            p.append("winner has no win payoff")
        elif abs(w.win_payoff / 2.0 - 1.0 - w.odds_to_one) > 0.011:
            p.append(f"winner odds {w.odds_to_one} disagree with the ${w.win_payoff} win payoff")
    missing = [s.program for s in race.starters if not s.trainer]
    if missing:
        p.append(f"no trainer for program(s) {', '.join(missing)}")
    if race.distance_furlongs is None:
        p.append(f"distance not understood: {race.distance_text!r}")
    if race.surface is None:
        p.append(f"surface not understood: {race.distance_text!r}")
    if race.final_time_seconds is None:
        p.append(f"final time not understood: {race.final_time!r}")
    if not race.fractional_times:
        p.append("no fractional times")
    if race.track_condition is None:
        p.append("no track condition")
    if race.value_of_race is not None and sum(race.purse_by_place.values()) != race.value_of_race:
        p.append(f"purse by place sums to {sum(race.purse_by_place.values())}, race value is {race.value_of_race}")
    race.problems = list(dict.fromkeys(p))


# ---- whole card ---------------------------------------------------------------------------------
def parse_chart_text(text: str, *, source_sha256: str = "") -> ChartCard:
    lines = text.replace("\r", "").split("\n")
    heads = [(i, _HEADER.match(ln.strip())) for i, ln in enumerate(lines)]
    heads = [(i, m) for i, m in heads if m]
    errors: list[str] = []
    if not heads:
        errors.append("no race headers found (expected 'TRACK - Month D, YYYY - Race N')")
    races: list[ChartRace] = []
    for n, (i, m) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        races.append(_parse_race(m, lines[i + 1:end]))
    keys = [r.race_key for r in races]
    for dup in sorted({k for k in keys if keys.count(k) > 1}):
        errors.append(f"race {dup} appears more than once")
    numbers = sorted(r.race_number for r in races)
    if numbers and numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        errors.append(f"race numbers are not consecutive: {numbers}")
    return ChartCard(source_sha256=source_sha256, parser_version=PARSER_VERSION, races=races, errors=errors)


def parse_chart_pdf(source: str | Path | bytes) -> ChartCard:
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    return parse_chart_text(extract_chart_text(raw), source_sha256=hashlib.sha256(raw).hexdigest())
