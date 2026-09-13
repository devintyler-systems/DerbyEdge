"""Strict parser for TwinSpires Speed/Power/Style Markdown exports.

The rendered export is deliberately treated as a provider-specific observation
table.  It is not a DraftKings card and its proprietary speed columns are not
Beyer figures.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path


PARSER_VERSION = "twinspires_markdown_speed_power_style/1.1.0"
_STYLE = re.compile(r"^(E/P|E|P|S|NA)(\d+)$", re.I)
_PERCENT = re.compile(r"^(?:\d+(?:\.\d+)?%|-)$")
_HEADER = (
    "ODDS", "PL", "RUNNER", "DAYS", "OFF", "RUN", "STYLE", "AVG", "SPD",
    "BACK", "SPD", "SPD", "LR", "AVG", "CLS", "PRM", "PWR", "W%",
    "JKY", "W%", "TRN", "$", "WON",
)


@dataclasses.dataclass(frozen=True)
class TwinSpiresRecord:
    program_number: str
    horse_name: str
    run_style: str | None
    avg_speed: float | None
    back_speed: float | None
    last_speed: float | None
    class_rating: float | None
    power_rating: float | None
    jockey_win_pct: float | None
    trainer_win_pct: float | None
    raw_values: dict[str, str | None]
    raw_text: str
    scratched: bool = False
    run_style_code: str | None = None
    early_speed_points: int | None = None
    current_odds: str | None = None


@dataclasses.dataclass(frozen=True)
class TwinSpiresMarkdownCard:
    source_path: str
    source_filename: str
    source_sha256: str
    parser_version: str
    declared_as_of: str | None
    as_of_status: str
    records: tuple[TwinSpiresRecord, ...]
    parser_errors: tuple[str, ...]
    parser_warnings: tuple[str, ...]


def _number(value: str | None) -> float | None:
    if value in (None, "-", ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _percent(value: str | None) -> float | None:
    if value in (None, "-", ""):
        return None
    try:
        return float(value.rstrip("%")) / 100.0
    except ValueError:
        return None


def _as_of(raw: str, declared_as_of: str | None) -> tuple[str | None, str]:
    # Only an explicit artifact marker or documented caller parameter may
    # establish decision-time provenance.  Filename and filesystem metadata
    # are intentionally ignored.
    artifact = re.search(r"(?:source\s+)?as\s*of\s*[:=]\s*([^\n]+)", raw, re.I)
    value = declared_as_of or (artifact.group(1).strip() if artifact else None)
    if value is None:
        return None, "UNPROVEN"
    try:
        normalized = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value, "INVALID"
    if normalized.tzinfo is None:
        return value, "INVALID"
    return normalized.astimezone(timezone.utc).isoformat(), "PROVEN"


def _record_from_chunk(program: str, lines: list[str]) -> TwinSpiresRecord | None:
    if not lines:
        return None
    if any(line.upper() == "SCR" for line in lines[:2]):
        return TwinSpiresRecord(program, "", None, None, None, None, None, None, None, None, {}, "\n".join(lines), True)
    style_at = next((idx for idx, value in enumerate(lines) if _STYLE.match(value)), None)
    if style_at is None or style_at < 2 or len(lines) < style_at + 9:
        return None
    if not re.fullmatch(r"\d+|-", lines[style_at - 1]):
        return None
    # The preceding cell is DAYS; the horse-name cell is immediately before it.
    horse_name = lines[style_at - 2]
    # The eight cells after the style code have a fixed documented order.
    cells = lines[style_at + 1:style_at + 9]
    if any(cell not in ("-", "") and _number(cell) is None for cell in cells[:5]):
        return None
    if any(cell not in ("-", "") and not _PERCENT.fullmatch(cell) for cell in cells[5:7]):
        return None
    raw_values = dict(zip((
        "avg_speed", "back_speed", "last_speed", "class_rating", "power_rating",
        "jockey_win_pct", "trainer_win_pct", "dollars_won",
    ), cells, strict=True))
    return TwinSpiresRecord(
        program_number=program, horse_name=horse_name, run_style=lines[style_at],
        avg_speed=_number(cells[0]), back_speed=_number(cells[1]), last_speed=_number(cells[2]),
        class_rating=_number(cells[3]), power_rating=_number(cells[4]),
        jockey_win_pct=_percent(cells[5]), trainer_win_pct=_percent(cells[6]),
        raw_values=raw_values, raw_text="\n".join(lines), scratched=False,
        run_style_code=_STYLE.fullmatch(lines[style_at]).group(1).upper(),
        early_speed_points=int(_STYLE.fullmatch(lines[style_at]).group(2)),
        current_odds=lines[0],
    )


def parse_twinspires_markdown(
    path: str | Path | bytes, *, source_filename: str | None = None, declared_as_of: str | None = None,
) -> TwinSpiresMarkdownCard:
    """Parse an export, preserving missing cells as ``None`` without inference."""
    if isinstance(path, bytes):
        raw_bytes, source_path = path, source_filename or "<in-memory>"
    else:
        source = Path(path)
        raw_bytes, source_path = source.read_bytes(), str(source)
        source_filename = source_filename or source.name
    raw = raw_bytes.decode("utf-8", errors="replace")
    lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.startswith("#")]
    # A program number is the numeric line immediately followed by an odds
    # cell and then the rendered ``M:`` morning-line cell.  Numeric speed
    # values must not be mistaken for runner boundaries.
    starts = [idx for idx, line in enumerate(lines)
              if re.fullmatch(r"\d{1,2}", line)
              and idx + 2 < len(lines) and lines[idx + 2].upper().startswith("M:")]
    errors: list[str] = []
    header = tuple(value.upper() for value in lines[:starts[0]]) if starts else ()
    if header not in (_HEADER, ("ALL",) + _HEADER):
        errors.append("TwinSpires summary header does not match the expected column schema")
    records: list[TwinSpiresRecord] = []
    for offset, start in enumerate(starts):
        end = starts[offset + 1] if offset + 1 < len(starts) else len(lines)
        record = _record_from_chunk(lines[start], lines[start + 1:end])
        if record is None:
            errors.append(f"program {lines[start]} malformed: required style/value cells unavailable")
        elif not record.scratched:
            records.append(record)
    if not starts:
        errors.append("no TwinSpires runner rows found")
    programs = [record.program_number for record in records]
    if len(programs) != len(set(programs)):
        errors.append("duplicate active program number")
    as_of, as_of_status = _as_of(raw, declared_as_of)
    warnings = [] if as_of_status == "PROVEN" else ["source as-of timestamp is not explicitly proven"]
    if as_of_status == "INVALID":
        errors.append("declared source as-of timestamp is invalid or lacks timezone")
    return TwinSpiresMarkdownCard(
        source_path=source_path, source_filename=source_filename or Path(source_path).name,
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(), parser_version=PARSER_VERSION,
        declared_as_of=as_of, as_of_status=as_of_status, records=tuple(records),
        parser_errors=tuple(errors), parser_warnings=tuple(warnings),
    )
