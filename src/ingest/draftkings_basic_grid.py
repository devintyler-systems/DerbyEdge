"""Parser for the DraftKings *Basic* tab as rendered text (one cell per line).

Header (one cell per line): ``# All ODDS ML PL Runner MED/WT/EQP Jockey Trainer``.
Each runner is then exactly one cell per header column plus the leading program
number.  The grid carries the assigned weight (``L122`` = Lasix + 122 lb), which
the Advanced tab omits.  A row that does not line up with the header fails the
whole grid rather than guessing which cell is missing.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re

_PROGRAM = re.compile(r"^(?:AE|MTO)?\d{1,2}[A-Za-z]?$")
_MEDWT = re.compile(r"^(?P<med>[A-Za-z]*)(?P<wt>\d{2,3})$")
_REQUIRED = ("ml", "runner", "med/wt/eqp", "jockey", "trainer")


class BasicGridError(ValueError):
    """The Basic grid cannot be read as aligned runner rows."""


@dataclasses.dataclass(frozen=True)
class BasicGridRow:
    program_number: str
    odds: str | None
    morning_line: str | None
    horse_name: str
    medication: str | None
    weight: int | None
    jockey: str | None
    trainer: str | None
    scratched: bool


@dataclasses.dataclass(frozen=True)
class BasicGrid:
    source_sha256: str
    rows: tuple[BasicGridRow, ...]


def _clean_person(value: str | None) -> str | None:
    text = (value or "").strip().lstrip("*").strip()
    return text or None


def parse_basic_grid(text: str) -> BasicGrid:
    cells = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    try:
        end = next(i for i, c in enumerate(cells) if c.lower() == "trainer")
    except StopIteration:
        raise BasicGridError("Basic grid header has no Trainer column") from None
    header = [c.lower() for c in cells[:end + 1] if c not in ("#",) and c.lower() != "all"]
    missing = [c for c in _REQUIRED if c not in header]
    if missing:
        raise BasicGridError("Basic grid header missing column(s): " + ", ".join(missing))
    index = {name: i + 1 for i, name in enumerate(header)}  # +1: leading program cell
    width = len(header) + 1
    body = cells[end + 1:]
    if not body:
        raise BasicGridError("Basic grid has no runner rows")
    if len(body) % width:
        raise BasicGridError(
            f"Basic grid has {len(body)} cells, not a multiple of the {width}-cell row width; "
            "a cell is missing or extra"
        )
    rows: list[BasicGridRow] = []
    seen: set[str] = set()
    for n in range(len(body) // width):
        row = body[n * width:(n + 1) * width]
        program = row[0]
        if not _PROGRAM.match(program):
            raise BasicGridError(f"Basic grid row {n + 1} does not start with a program number: {program!r}")
        if program.lower() in seen:
            raise BasicGridError(f"Basic grid repeats program number {program!r}")
        seen.add(program.lower())
        medwt = _MEDWT.match(row[index["med/wt/eqp"]])
        if not medwt:
            raise BasicGridError(
                f"Basic grid row {n + 1} (program {program}) misaligned: "
                f"MED/WT/EQP cell is {row[index['med/wt/eqp']]!r}"
            )
        odds = row[index["odds"]] if "odds" in index else None
        rows.append(BasicGridRow(
            program_number=program, odds=odds,
            morning_line=row[index["ml"]], horse_name=row[index["runner"]],
            medication=medwt.group("med") or None, weight=int(medwt.group("wt")),
            jockey=_clean_person(row[index["jockey"]]), trainer=_clean_person(row[index["trainer"]]),
            scratched=(odds or "").upper() == "SCR",
        ))
    return BasicGrid(source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), rows=tuple(rows))
