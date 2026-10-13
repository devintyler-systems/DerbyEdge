"""TwinSpires SPEED, CLASS and PACE tabs (BRIS figures with ranks), pasted into a race bundle.

Each tab is one cell per line.  The parser is strict by design: the column header row must match the sample literally
(that row is also how the three tabs are told apart) and every runner must carry exactly the expected cells, so a layout
change fails naming the program and the offending text instead of shifting values into the wrong column.

Nothing here feeds scoring.  The figures are captured now because they cannot be reconstructed as-of later, and they are
stored under their exact printed column names with no interpretation: TwinSpires' own help text defines "Average Speed"
and "Average Class" inconsistently between screens, so which reading predicts is for outcome data to decide.
The live ODDS cell is not parsed (the tabs are captured at different moments); only the morning line is kept.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from typing import Any

from src.utils.distance_parser import parse_furlongs

PARSER_VERSION = "twinspires_tabs_v1"

# kind -> (label shown to the user, printed column header row, value columns in printed order)
TAB_SPECS: dict[str, dict[str, Any]] = {
    "ts_speed": {
        "title": "Speed",
        "header": ["#", "all", "odds", "runner", "run style", "average speed", "average distance", "best speed"],
        "style": True,
        "columns": ["average_speed", "average_distance", "best_speed"],
    },
    "ts_class": {
        "title": "Class",
        "header": ["#", "all", "odds", "runner", "days off", "prime power", "last class", "average class"],
        "style": False,
        "columns": ["prime_power", "last_class", "average_class"],
    },
    "ts_pace": {
        "title": "Pace",
        "header": ["#", "all", "odds", "runner", "run style", "early pace 1", "early pace 2", "late pace"],
        "style": True,
        "columns": ["early_pace_1", "early_pace_2", "late_pace"],
    },
}
TAB_KINDS = tuple(TAB_SPECS)

_PROGRAM = re.compile(r"^(?:AE|MTO)?\d{1,2}[A-Za-z]?$")
_PP = re.compile(r"^PP\s+(\d+)$", re.I)
_ML = re.compile(r"^M:\s*(.+)$", re.I)
_IDENTITY = re.compile(r"\b\d+\s*yrs?\b", re.I)          # "Dark Bay or Brown, Mare, 6 yrs (KY) L"
_STYLE = re.compile(r"^(E/P|E|P|S|NA)\s*(\d+)$", re.I)
_VALUE = re.compile(r"^(?:-?\d+(?:\.\d+)?|-)$")
_RANK = re.compile(r"^(?:(\d+)(?:st|nd|rd|th)|-)$", re.I)
_SCRATCH = frozenset({"SCR", "SCRATCH", "SCRATCHED"})


class TwinSpiresTabError(ValueError):
    """A tab is present but malformed; the message names the tab, the program and what was found."""


@dataclasses.dataclass
class TabRunner:
    program_number: str
    post_position: int
    horse_name: str
    morning_line: str | None
    scratched: bool
    run_style_raw: str | None           # as printed, e.g. "NA 0", "E/P 2"; None on the Class tab
    run_style_code: str | None          # E, E/P, P, S or NA
    run_style_points: int | None        # the 0-8 early-speed points printed beside the style
    values: dict[str, float | None]     # printed column name -> value; None for a printed "-" (never 0)
    ranks: dict[str, int | None]        # printed rank cell ("8th" -> 8); None for "-"
    days_off: int | None = None         # Class tab only


@dataclasses.dataclass
class TwinSpiresTab:
    kind: str
    runners: list[TabRunner]
    raw_text: str
    raw_sha256: str
    parser_version: str = PARSER_VERSION

    @property
    def title(self) -> str:
        return TAB_SPECS[self.kind]["title"]

    def to_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out.pop("raw_text")             # stored beside the parsed form, not inside it
        return out


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip()).casefold()


def header_kind(window: list[str]) -> str | None:
    """Which tab a run of normalised header lines belongs to (by its distinctive columns), else None."""
    have = set(window)
    if {"average distance", "best speed"} <= have:
        return "ts_speed"
    if {"prime power", "last class"} <= have:
        return "ts_class"
    if "early pace 1" in have:
        return "ts_pace"
    return None


def distance_category(distance: str | None) -> str | None:
    """'sprint' (under a mile) or 'route' (a mile or more): pace figures are measured to different calls in each."""
    furlongs = parse_furlongs(distance)
    if furlongs is None:
        return None
    return "route" if furlongs >= 8.0 else "sprint"


def _cell(token: str, pattern: re.Pattern[str], what: str) -> re.Match[str]:
    m = pattern.match(token)
    if m is None:
        raise TwinSpiresTabError(f"{what}: unexpected cell {token!r}")
    return m


def parse_twinspires_tab(text: str, kind: str) -> TwinSpiresTab:
    """Parse one tab; raise ``TwinSpiresTabError`` naming the problem if it is not exactly the known layout."""
    spec = TAB_SPECS[kind]
    title = spec["title"]
    tokens = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    starts = [i for i in range(len(tokens) - 1) if _PROGRAM.match(tokens[i]) and _PP.match(tokens[i + 1])]
    if not starts:
        raise TwinSpiresTabError(f"{title} tab: no runner rows found (a row starts with the program number, then 'PP n')")
    header = [_norm(t) for t in tokens[:starts[0]]]
    expected = spec["header"]
    if header not in (expected, [h for h in expected if h != "all"]):
        raise TwinSpiresTabError(
            f"{title} tab: expected the column header row {[h for h in expected if h != 'all']} "
            f"(copy the table from its '#' header down) but found {header}")
    n_cols = len(spec["columns"])
    runners: list[TabRunner] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(tokens)
        chunk = tokens[start:end]
        program = chunk[0]
        what = f"{title} tab, program {program}"
        post_position = int(_PP.match(chunk[1]).group(1))                    # type: ignore[union-attr]
        if len(chunk) < 4:
            raise TwinSpiresTabError(f"{what}: row is too short: {chunk}")
        odds = chunk[2]
        ml = _ML.match(chunk[3])
        if ml is None:
            raise TwinSpiresTabError(f"{what}: expected 'M: <morning line>' after the odds cell, found {chunk[3]!r}")
        ident_at = next((i for i in range(4, len(chunk)) if _IDENTITY.search(chunk[i])), None)
        if ident_at is None or ident_at < 5:
            raise TwinSpiresTabError(f"{what}: no horse name + identity line ('... 6 yrs (KY) L') found: {chunk[3:8]}")
        name = chunk[ident_at - 1]
        cells = chunk[ident_at + 1:]
        lead = 1                                                            # the style cell, or the days-off cell
        if len(cells) != lead + 2 * n_cols:
            raise TwinSpiresTabError(
                f"{what} ({name}): expected {lead + 2 * n_cols} cells after the identity line "
                f"(1 + {n_cols} value/rank pairs) but found {len(cells)}: {cells}")
        style_raw = style_code = None
        points = days_off = None
        if spec["style"]:
            style_raw = cells[0]
            sm = _cell(cells[0], _STYLE, f"{what} ({name}) run style")
            style_code, points = sm.group(1).upper(), int(sm.group(2))
        else:
            _cell(cells[0], _VALUE, f"{what} ({name}) days off")
            days_off = None if cells[0] == "-" else int(float(cells[0]))
        values: dict[str, float | None] = {}
        ranks: dict[str, int | None] = {}
        for k, col in enumerate(spec["columns"]):
            v, r = cells[lead + 2 * k], cells[lead + 2 * k + 1]
            _cell(v, _VALUE, f"{what} ({name}) {col}")
            rm = _cell(r, _RANK, f"{what} ({name}) {col} rank")
            values[col] = None if v == "-" else float(v)
            ranks[col] = int(rm.group(1)) if rm.group(1) else None
        runners.append(TabRunner(
            program_number=program, post_position=post_position, horse_name=name,
            morning_line=ml.group(1).strip() or None, scratched=odds.upper() in _SCRATCH,
            run_style_raw=style_raw, run_style_code=style_code, run_style_points=points,
            values=values, ranks=ranks, days_off=days_off))
    programs = [r.program_number.upper() for r in runners]
    if len(programs) != len(set(programs)):
        raise TwinSpiresTabError(f"{title} tab: a program number appears twice")
    raw = "\n".join(text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")) + "\n"
    return TwinSpiresTab(kind=kind, runners=runners, raw_text=raw, raw_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest())
