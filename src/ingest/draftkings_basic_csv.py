"""Parser for DraftKings Basic-tab wagering-grid CSV exports.

The Basic tab is a same-race, pre-race DraftKings view that supplies the
assigned-weight column omitted by some Advanced/PP Markdown renders.  This
module intentionally retains only the small, source-owned overlay needed for
that reconciliation: program number and assigned weight.
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
from io import StringIO
from pathlib import Path
import re
from typing import Iterable


BASIC_TAB_COLUMNS: tuple[str, ...] = (
    "#", "ODDS", "ML", "RUNNER", "WEIGHT", "JOCKEY", "TRAINER", "SIRE",
    "DAM", "RUN STYLE", "DAYS OFF",
)
"""Required columns in the DraftKings Basic-tab CSV export."""

_PROGRAM_RE = re.compile(r"^\d{1,2}[A-Za-z]?$")


class DraftKingsBasicCSVError(ValueError):
    """Raised when a purported Basic-tab CSV cannot safely be used as an overlay."""


def normalize_program_number(value: object | None) -> str | None:
    """Return the case-insensitive matching key for a DK program number."""
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    return normalized or None


@dataclasses.dataclass(frozen=True)
class DraftKingsBasicCSVRow:
    """One Basic-tab runner row, keyed by its source program number (``#``)."""
    program_number: str
    weight: int | None
    current_odds: str | None = None
    horse_name: str | None = None


@dataclasses.dataclass(frozen=True)
class DraftKingsBasicCSV:
    """Validated Basic-tab CSV weight source suitable for a same-race overlay."""
    source_path: str
    source_sha256: str
    rows: tuple[DraftKingsBasicCSVRow, ...]

    @property
    def weights_by_program_number(self) -> dict[str, int]:
        """Return populated weights keyed by normalized DraftKings program number."""
        return {
            normalize_program_number(row.program_number) or row.program_number: row.weight
            for row in self.rows
            if row.weight is not None
        }


def _read_source(source: str | bytes | Path, source_path: str | None) -> tuple[str, str, bytes]:
    if isinstance(source, Path):
        raw_bytes = source.read_bytes()
        return raw_bytes.decode("utf-8-sig"), str(source), raw_bytes
    if isinstance(source, bytes):
        return source.decode("utf-8-sig"), source_path or "<in-memory-dk-basic-csv>", source
    if source_path is None and "\n" not in source:
        candidate = Path(source)
        if candidate.is_file():
            raw_bytes = candidate.read_bytes()
            return raw_bytes.decode("utf-8-sig"), str(candidate), raw_bytes
    return source, source_path or "<in-memory-dk-basic-csv>", source.encode("utf-8")


def _required_headers(fieldnames: Iterable[str | None]) -> set[str]:
    return {header.strip() for header in fieldnames if header is not None and header.strip()}


def parse_draftkings_basic_csv(
    source: str | bytes | Path, *, source_path: str | None = None,
) -> DraftKingsBasicCSV:
    """Parse the documented DK Basic-tab CSV schema into a weight overlay.

    Program numbers must be unique, since ambiguity would make a weight overlay
    unsafe.  Blank WEIGHT cells remain absent; non-blank malformed values reject
    the source rather than being guessed or silently discarded.
    """
    text, resolved_path, raw_bytes = _read_source(source, source_path)
    reader = csv.DictReader(StringIO(text))
    headers = _required_headers(reader.fieldnames or ())
    missing_headers = [column for column in BASIC_TAB_COLUMNS if column not in headers]
    if missing_headers:
        raise DraftKingsBasicCSVError(
            "DK Basic-tab CSV missing required column(s): " + ", ".join(missing_headers)
        )

    rows: list[DraftKingsBasicCSVRow] = []
    seen_programs: set[str] = set()
    for row_number, raw_row in enumerate(reader, start=2):
        # Ignore a physically blank trailing row, but reject malformed runner rows.
        if not any((value or "").strip() for value in raw_row.values()):
            continue
        raw_program = (raw_row.get("#") or "").strip()
        program_key = normalize_program_number(raw_program)
        if program_key is None or not _PROGRAM_RE.fullmatch(raw_program):
            raise DraftKingsBasicCSVError(
                f"DK Basic-tab CSV row {row_number} has invalid program number: {raw_program!r}"
            )
        if program_key in seen_programs:
            raise DraftKingsBasicCSVError(
                f"DK Basic-tab CSV contains duplicate program number: {raw_program!r}"
            )
        seen_programs.add(program_key)

        raw_weight = (raw_row.get("WEIGHT") or "").strip()
        weight: int | None = None
        if raw_weight:
            if not re.fullmatch(r"\d{1,3}", raw_weight):
                raise DraftKingsBasicCSVError(
                    f"DK Basic-tab CSV row {row_number} has invalid WEIGHT: {raw_weight!r}"
                )
            weight = int(raw_weight)
            if weight <= 0:
                raise DraftKingsBasicCSVError(
                    f"DK Basic-tab CSV row {row_number} has invalid WEIGHT: {raw_weight!r}"
                )
        rows.append(DraftKingsBasicCSVRow(
            program_number=raw_program, weight=weight,
            current_odds=(raw_row.get("ODDS") or "").strip() or None,
            horse_name=(raw_row.get("RUNNER") or "").strip() or None,
        ))

    return DraftKingsBasicCSV(
        source_path=resolved_path,
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        rows=tuple(rows),
    )
