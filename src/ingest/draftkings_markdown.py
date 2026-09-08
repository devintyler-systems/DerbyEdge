"""Fail-closed staging parser for DraftKings race-card Markdown exports.

This module deliberately does not parse PDFs and does not call model or scoring
code.  A caller must pass :func:`require_scoring_ready` before converting this
raw capture into any feature-generation input.
"""
from __future__ import annotations

import dataclasses
import hashlib
from io import BytesIO
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.distance_parser import parse_furlongs


PARSER_VERSION = "1.1.0"
DATE_RE = re.compile(r"(?P<value>[A-Z][a-z]{2}\s+\d{1,2},\s*'\d{2})")
ENTRY_RE = re.compile(
    r"(?m)^(?P<program>\d{1,2}[A-Za-z]?)\s*\n"
    r"(?P<odds>[^\n]+)\n(?P<morning_line>[^\n]+)\n"
    r"(?P<horse>[^\n]+)\n(?P<medication>L?\d{2,3}[^\n]*)\n"
    r"(?P<jockey>[^\n]+)\n(?P<trainer>[^\n]+)\s*$"
)
FILENAME_DATE_RE = re.compile(r"_R\d+_(\d{1,2}-\d{1,2}-(?:\d{2}|\d{4}))", re.I)

# Del-Mar-style layout: every runner is anchored by a bare ``PP {n}`` line, with
# the program number on its own line just above.  This layout does not use the
# compact ``ENTRY_RE`` header at all.
PP_ANCHOR_RE = re.compile(r"(?m)^PP\s+(?P<pp>\d+)\s*$")
_PROGRAM_LINE_RE = re.compile(r"^(?P<program>\d{1,2}[A-Za-z]?)$")
_DMR_IDENTITY_RE = re.compile(
    r"^(?P<color>.+?),\s*"
    r"(?P<sex>Colt|Filly|Gelding|Horse|Mare|Ridgling|Rig|Rigling|Colt/Gelding)\.?,?\s*"
    r"(?P<age>\d{1,2})\s*yrs?\b\s*"
    r"(?:\(\s*(?P<state>[A-Za-z]{2,3})\s*\))?\s*"
    r"(?P<med>[A-Za-z0-9*/ ]+?)?\s*$",
    re.I,
)
_PCT_RECORD_RE = re.compile(r"^\d{1,3}\s*%")
_MTP_RE = re.compile(r"(?mi)^\s*(?:(\d{1,3})\s*)?MTP\s*$")
_SCRATCH_TOKENS = frozenset({"SCR", "SCRATCH", "SCRATCHED"})
_SEX_WORDS = frozenset({
    "colt", "filly", "gelding", "horse", "mare", "ridgling", "rig", "rigling",
})


@dataclasses.dataclass
class Race:
    track: str | None = None
    race_date: date | None = None
    race_number: int | None = None
    scheduled_post_time: str | None = None
    class_code: str | None = None
    purse: int | None = None
    age_restriction: str | None = None
    sex_restriction: str | None = None
    distance: str | None = None
    normalized_distance_furlongs: float | None = None
    surface: str | None = None
    surface_condition: str | None = None
    raw_distance: str | None = None
    raw_class_code: str | None = None
    raw_surface_condition: str | None = None
    # Non-breaking additions: the post is sometimes rendered as a countdown
    # ("9 MTP") rather than a clock time, and cards can carry a status token.
    post_time_display: str | None = None
    card_status: str | None = None


@dataclasses.dataclass
class HorseProfile:
    age: int | None = None
    sex: str | None = None
    color: str | None = None
    sire: str | None = None
    dam: str | None = None
    dam_sire: str | None = None
    breeder: str | None = None
    raw_profile: str | None = None


@dataclasses.dataclass
class RecordSplit:
    """A source-provided pre-race W/P/S/E aggregate, retained numerically."""
    starts: int | None = None
    wins: int | None = None
    places: int | None = None
    shows: int | None = None
    earnings: int | None = None
    raw_label: str | None = None
    raw_text: str | None = None


@dataclasses.dataclass
class Entry:
    program_number: str | None
    horse_name: str | None
    post_position: int | None
    weight: int | None
    jockey: str | None
    trainer: str | None
    morning_line: str | None
    medication_weight_equipment: str | None
    horse_profile: HorseProfile = dataclasses.field(default_factory=HorseProfile)
    owner: str | None = None
    breeder: str | None = None
    record_splits: dict[str, RecordSplit] = dataclasses.field(default_factory=dict)
    raw_program_number: str | None = None
    raw_morning_line: str | None = None
    raw_block: str | None = None
    # Non-breaking additions for runner-retention hardening.  A runner block is
    # retained on identity evidence alone; ALL RACES / WORKOUTS are optional
    # child sections whose absence is reported, not fatal.
    is_scratched: bool = False
    runner_retained_without_history: bool = False
    has_all_races_section: bool = False
    has_workouts_section: bool = False
    runner_parse_warnings: list[str] = dataclasses.field(default_factory=list)
    block_source_span: tuple[int, int] | None = None


@dataclasses.dataclass
class PastPerformance:
    horse_name: str
    start_date: date | None
    track: str | None
    race_class: str | None
    distance: str | None
    surface_condition: str | None
    program_or_post: str | None
    odds: str | None
    finish_position: int | str | None
    beaten_lengths: str | None
    jockey: str | None
    comment: str | None
    raw_distance: str | None = None
    raw_race_class: str | None = None
    raw_surface_condition: str | None = None
    raw_odds: str | None = None
    raw_beaten_lengths: str | None = None
    raw_text: str | None = None


@dataclasses.dataclass
class Workout:
    horse_name: str
    work_date: date | None
    track: str | None
    distance: str | None
    surface_condition: str | None
    time: str | None
    rank: str | None
    designation: str | None = None
    rank_numerator: int | None = None
    rank_denominator: int | None = None
    raw_distance: str | None = None
    raw_surface_condition: str | None = None
    raw_time: str | None = None
    raw_text: str | None = None


@dataclasses.dataclass
class DraftKingsMarkdownCard:
    source_path: str
    source_sha256: str
    source_format: str
    parser_version: str
    as_of: datetime
    race: Race
    entries: list[Entry]
    past_performances: list[PastPerformance]
    workouts: list[Workout]
    parser_errors: list[str] = dataclasses.field(default_factory=list)
    parser_warnings: list[str] = dataclasses.field(default_factory=list)
    expected_runner_count: int | None = None
    declared_pp_header: bool = False
    declared_workout_header: bool = False
    open_sections: list[str] = dataclasses.field(default_factory=list)
    # Immutable source payload retained solely for append-only provenance.
    raw_bytes: bytes = b""


@dataclasses.dataclass
class ValidationResult:
    source_path: str
    source_format: str
    source_sha256: str
    parser_version: str
    validation_timestamp: str
    race_identifier: str
    expected_runner_count: int | None
    parsed_unique_runner_count: int
    past_performance_row_count: int
    workout_row_count: int
    errors: list[str]
    warnings: list[str]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class DraftKingsMarkdownValidationError(ValueError):
    """Raised when raw card records attempt to enter features without passing QC."""


@dataclasses.dataclass
class ExcelReconciliationResult:
    """Non-production, best-effort workbook observations for operator QC."""
    source_path: str
    source_sha256: str
    worksheets_scanned: int
    recognizable_runner_records: int
    recognizable_past_performance_records: int
    recognizable_workout_records: int
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.replace("  ", " "), "%b %d, '%y").date()
    except ValueError:
        return None


def _parse_filename_date(source_path: str) -> date | None:
    match = FILENAME_DATE_RE.search(Path(source_path).name)
    if not match:
        return None
    value = match.group(1)
    for fmt in ("%m-%d-%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _read_source(source: str | Path, source_path: str | None) -> tuple[str, str, bytes]:
    if isinstance(source, Path):
        raw_bytes = source.read_bytes()
        return raw_bytes.decode("utf-8"), str(source), raw_bytes
    if source_path is None and "\n" not in source:
        candidate = Path(source)
        if candidate.is_file():
            raw_bytes = candidate.read_bytes()
            return raw_bytes.decode("utf-8"), str(candidate), raw_bytes
    return source, source_path or "<in-memory-markdown>", source.encode("utf-8")


def _normal_surface(value: str | None) -> str | None:
    if not value:
        return None
    value = value.lower()
    if value.startswith(("dirt", "tr.d")):
        return "dirt"
    if value.startswith(("turf", "i-", "tr.t")):
        return "turf"
    if value.startswith("aw"):
        return "all_weather"
    return value


def _distance_furlongs(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = parse_furlongs(value)
        return float(parsed) if parsed is not None else None
    except (TypeError, ValueError):
        return None


def _metadata(raw: str, source_path: str) -> Race:
    header = raw.split("PROGRAM", 1)[0]
    lines = [_clean(line) for line in header.splitlines()]
    lines = [line for line in lines if line]
    race_match = re.search(r"\bRACE\s+(\d+)\b", header, re.I)
    purse_match = re.search(r"Purse:\s*\$([\d,.]+)K?", header, re.I)
    distance_match = re.search(r"(?m)^\s*(\d+(?:\s+\d+/\d+)?\s*[MF])\s*$", header, re.I)
    surface_match = re.search(r"(?m)^\s*([A-Za-z.]+):\s*([A-Za-z ]+)\s*$", header)
    class_match = re.search(r"(?m)^\s*(\$[^\n]+(?:CLAIMING|MAIDEN|ALLOWANCE|STAKES)[^\n]*)\s*$", header, re.I)
    age_match = re.search(r"(?m)^\s*(\dYO\+?|\d\s*YO\+?)\s*$", header, re.I)
    post_match = re.search(r"(?m)^(\d{1,2}:\d{2})\s*$\s*^([AP]M)\s*$", header)
    track = lines[0] if lines else None
    distance = distance_match.group(1) if distance_match else None
    raw_surface = "-".join(surface_match.groups()) if surface_match else None

    # Post display + card status.  The header renders either a clock time
    # ("1:03" / "PM") or a minutes-to-post countdown ("9" / "MTP"); capture the
    # visible string and any status token without disturbing scheduled_post_time.
    post_display: str | None = None
    card_status: str | None = None
    mtp_match = _MTP_RE.search(header)
    if mtp_match:
        card_status = "MTP"
    if race_match:
        tail = header[race_match.end():]
        stop = re.search(r"(?m)^\s*(?:\$|Purse:|More\b|PROGRAM\b)", tail)
        window_lines = [
            _clean(line) for line in tail[: stop.start() if stop else len(tail)].splitlines()
        ]
        window_lines = [line for line in window_lines if line]
        if window_lines:
            post_display = " ".join(window_lines[:3])
    if post_display is None and post_match:
        post_display = " ".join(post_match.groups())
    purse = None
    if purse_match:
        raw_purse = purse_match.group(1).replace(",", "")
        purse = int(float(raw_purse) * (1000 if "K" in purse_match.group(0).upper() else 1))
    return Race(
        track=track,
        race_date=_parse_filename_date(source_path),
        race_number=int(race_match.group(1)) if race_match else None,
        scheduled_post_time=(" ".join(post_match.groups()) if post_match else None),
        class_code=_clean(class_match.group(1)) if class_match else None,
        purse=purse,
        age_restriction=_clean(age_match.group(1)) if age_match else None,
        distance=distance,
        normalized_distance_furlongs=_distance_furlongs(distance),
        surface=_normal_surface(surface_match.group(1) if surface_match else None),
        surface_condition=_clean(surface_match.group(2)) if surface_match else None,
        raw_distance=distance,
        raw_class_code=_clean(class_match.group(1)) if class_match else None,
        raw_surface_condition=raw_surface,
        post_time_display=post_display,
        card_status=card_status,
    )


def _dated_blocks(section: str) -> list[tuple[date | None, list[str]]]:
    matches = list(DATE_RE.finditer(section))
    rows: list[tuple[date | None, list[str]]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        lines = [_clean(line) for line in section[match.end():end].splitlines()]
        rows.append((_parse_date(match.group("value")), [line for line in lines if line]))
    return rows


def _profile(horse: str, block: str) -> HorseProfile:
    match = re.search(
        re.escape(horse) + r"\s*\n([^\n]+)\s+-\s+([^\n]+)\s*\n([^,\n]+),\s*([^,\n]+),\s*(\d+)",
        block,
    )
    breeder_match = re.search(r"(?mi)^BREEDER\s*$\s*^([^\n]+)$", block)
    if not match:
        return HorseProfile(breeder=_clean(breeder_match.group(1)) if breeder_match else None)
    pedigree = _clean(match.group(2))
    # DK normally presents sire - dam; a parenthetical dam-sire is retained
    # when the rendered export supplies it rather than guessed from pedigree.
    dam_sire_match = re.search(r"\s+\((?:by\s+)?([^()]+)\)\s*$", pedigree or "", re.I)
    dam = re.sub(r"\s+\((?:by\s+)?[^()]+\)\s*$", "", pedigree or "").strip() or None
    return HorseProfile(
        sire=_clean(match.group(1)), dam=dam, color=_clean(match.group(3)),
        sex=_clean(match.group(4)), age=int(match.group(5)),
        dam_sire=_clean(dam_sire_match.group(1)) if dam_sire_match else None,
        breeder=_clean(breeder_match.group(1)) if breeder_match else None,
        raw_profile=match.group(0),
    )


def _record_splits(
    block: str, race_year: int | None, track_code: str | None = None
) -> dict[str, RecordSplit]:
    """Parse the rendered DK Record tables without treating them as results."""
    labels = {"life": "life", "dirt": "dirt", "turf": "turf", "aw": "all_weather",
              "off": "off_track", "dist": "distance", "sar": "target_track"}
    target = (track_code or "").strip().upper()
    if target:
        labels[target.lower()] = "target_track"
    splits: dict[str, RecordSplit] = {}
    pattern = re.compile(
        r"(?mi)^\s*(Life|Dirt|Turf|AW|OFF|Dist|SAR|[A-Z]{2,4}|20\d{2})\s+"
        r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+\$?([\d,]+)\s*$"
    )
    for match in pattern.finditer(block.split("ALL RACES", 1)[0]):
        raw_label = match.group(1)
        normalized = labels.get(raw_label.lower())
        if normalized is None and raw_label.isdigit():
            normalized = "current_year" if int(raw_label) == race_year else "prior_year"
        if normalized is None:
            continue
        splits[normalized] = RecordSplit(
            starts=int(match.group(2)), wins=int(match.group(3)), places=int(match.group(4)),
            shows=int(match.group(5)), earnings=int(match.group(6).replace(",", "")),
            raw_label=raw_label, raw_text=match.group(0).strip(),
        )
    return splits


def _parse_entry_block(match: re.Match[str], block: str, race_year: int | None = None) -> Entry:
    program = _clean(match.group("program"))
    medication = _clean(match.group("medication"))
    weight_match = re.search(r"(\d{2,3})", medication or "")
    return Entry(
        program_number=program,
        horse_name=_clean(match.group("horse")),
        post_position=int(re.match(r"\d+", program).group()) if program and re.match(r"\d+", program) else None,
        weight=int(weight_match.group(1)) if weight_match else None,
        jockey=_clean(match.group("jockey")), trainer=_clean(match.group("trainer")),
        morning_line=_clean(match.group("morning_line")), medication_weight_equipment=medication,
        horse_profile=_profile(_clean(match.group("horse")) or "", block),
        owner=(lambda owner_match: _clean(owner_match.group(1)) if owner_match else None)(
            re.search(r"(?mi)^OWNER\s*$\s*^([^\n]+)$", block)
        ),
        breeder=(lambda breeder_match: _clean(breeder_match.group(1)) if breeder_match else None)(
            re.search(r"(?mi)^BREEDER\s*$\s*^([^\n]+)$", block)
        ),
        record_splits=_record_splits(block, race_year),
        raw_program_number=program, raw_morning_line=_clean(match.group("morning_line")), raw_block=block,
    )


def _parse_history(entry: Entry, block: str) -> tuple[list[PastPerformance], list[Workout], bool, bool]:
    pp_header = "ALL RACES" in block
    workout_header = "WORKOUTS" in block
    pp_section = block.split("ALL RACES", 1)[1].split("WORKOUTS", 1)[0] if pp_header else ""
    workout_section = block.split("WORKOUTS", 1)[1] if workout_header else ""
    pps: list[PastPerformance] = []
    workouts: list[Workout] = []
    for start_date, lines in _dated_blocks(pp_section):
        joined = " ".join(lines)
        dist_match = re.search(
            r"((?:\d+(?:\s+\d+/\d+)?\s*[MF](?:\s+\d+\s*Y)?)|(?:\d{2,4}\s*Y))\s+"
            r"([A-Za-z.]+-[A-Za-z]+(?:\s+[A-Za-z]+)?)\s+(.+)$", joined, re.I,
        )
        before = joined[:dist_match.start()].strip() if dist_match else joined
        after = dist_match.group(3).split() if dist_match else []
        before_lines = [_clean(line) for line in before.split(" ") if line]
        track = lines[0] if lines else None
        race_class = lines[1] if len(lines) > 1 else None
        surface_condition = dist_match.group(2) if dist_match else None
        detail_match = re.match(
            r"(?P<program>\d+(?:\s+\(\d+\))?)\s+(?P<odds>\S+)\s+"
            r"(?P<finish>\d+|SCR)\s+(?P<bl>\S+)\s+(?P<jockey>\S+)\s*(?P<comment>.*)$",
            dist_match.group(3) if dist_match else "",
        )
        finish: int | str | None = None
        if detail_match and detail_match.group("finish").isdigit():
            finish = int(detail_match.group("finish"))
        elif detail_match and detail_match.group("finish") == "SCR":
            finish = "SCR"
        if finish is None and "SCR" in after:
            # An explicitly marked historical scratch is not an absent finish
            # field; retain the source state rather than treating it as a run.
            finish = "SCR"
        pps.append(PastPerformance(
            horse_name=entry.horse_name or "", start_date=start_date, track=track, race_class=race_class,
            distance=dist_match.group(1) if dist_match else None, surface_condition=surface_condition,
            program_or_post=detail_match.group("program") if detail_match else None,
            odds=detail_match.group("odds") if detail_match else None,
            finish_position=finish, beaten_lengths=detail_match.group("bl") if detail_match else None,
            jockey=detail_match.group("jockey") if detail_match else None,
            comment=_clean(detail_match.group("comment")) if detail_match else None,
            raw_distance=dist_match.group(1) if dist_match else None, raw_race_class=race_class,
            raw_surface_condition=surface_condition,
            raw_odds=detail_match.group("odds") if detail_match else None,
            raw_beaten_lengths=detail_match.group("bl") if detail_match else None, raw_text=joined,
        ))
    for work_date, lines in _dated_blocks(workout_section):
        joined = " ".join(lines)
        match = re.search(
            r"(\d+\s*[FM])\s+([A-Za-z.]+-[A-Za-z]+(?:\s+[A-Za-z]+)?)\s+"
            r"(\d{1,2}:\d{2}\.\d+|\d{2}\.\d+)\s*(?:[A-Za-z]+)?\s*(.*)$", joined, re.I,
        )
        time_text = _clean(match.group(3)) if match else None
        designation_match = re.search(r"\b([A-Za-z]{1,2})\s*$", time_text or "")
        rank_text = _clean(match.group(4)) if match else None
        rank_match = re.search(r"(\d+)\s+of\s+(\d+)", rank_text or "", re.I)
        workouts.append(Workout(
            horse_name=entry.horse_name or "", work_date=work_date, track=lines[0] if lines else None,
            distance=match.group(1) if match else None, surface_condition=match.group(2) if match else None,
            time=time_text, rank=rank_text,
            designation=designation_match.group(1) if designation_match else None,
            rank_numerator=int(rank_match.group(1)) if rank_match else None,
            rank_denominator=int(rank_match.group(2)) if rank_match else None,
            raw_distance=match.group(1) if match else None, raw_surface_condition=match.group(2) if match else None,
            raw_time=match.group(3) if match else None, raw_text=joined,
        ))
    return pps, workouts, pp_header, workout_header


def _compact_block_has_identity(match: re.Match[str], raw: str, next_boundary: int) -> bool:
    """Stage A retention test for the compact (``ENTRY_RE``) layout.

    The compact header is deliberately loose and also matches number-led history
    table fragments, so a real runner is still recognised by an ALL RACES
    history section before the next runner boundary -- with an explicit scratch
    header accepted as identity evidence when its top-matter (BREEDER / OWNER /
    Record) is present.  This keeps the long-standing compact-card semantics.
    """
    see_less = raw.find("SEE LESS", match.end())
    span_end = min(x for x in (see_less if see_less >= 0 else len(raw), next_boundary) if x >= 0)
    window = raw[match.end():span_end]
    if "ALL RACES" in window:
        return True
    scratched = (_clean(match.group("odds")) or "").upper() in _SCRATCH_TOKENS
    if scratched and re.search(r"(?mi)^\s*(BREEDER|OWNER|Record)\b", window):
        return True
    return False


def _split_pp_runner_blocks(raw: str) -> list[tuple[str | None, int, int, int]]:
    """Pure text segmentation for the ``PP {n}`` layout.

    Returns ``(program, pp_number, block_start, block_end)`` per runner, in
    document order.  ``block_start`` includes the bare program-number line that
    precedes the ``PP {n}`` anchor when one is present.
    """
    anchors = list(PP_ANCHOR_RE.finditer(raw))
    blocks: list[tuple[str | None, int, int, int]] = []
    for index, anchor in enumerate(anchors):
        line_start = raw.rfind("\n", 0, anchor.start()) + 1
        program: str | None = None
        block_start = line_start
        # Walk back over blank lines to find the program-number line.
        cursor = line_start
        for _ in range(4):
            prev_end = cursor - 1
            if prev_end <= 0:
                break
            prev_start = raw.rfind("\n", 0, prev_end) + 1
            prev_line = raw[prev_start:prev_end].strip()
            cursor = prev_start
            if not prev_line:
                continue
            program_match = _PROGRAM_LINE_RE.match(prev_line)
            if program_match:
                program = program_match.group("program")
                block_start = prev_start
            break
        next_start = (
            raw.rfind("\n", 0, anchors[index + 1].start()) + 1
            if index + 1 < len(anchors)
            else len(raw)
        )
        # Re-extend the following block's program line back to this block's end.
        if index + 1 < len(anchors):
            follow_start = next_start
            cursor = next_start
            for _ in range(4):
                prev_end = cursor - 1
                if prev_end <= 0:
                    break
                prev_start = raw.rfind("\n", 0, prev_end) + 1
                prev_line = raw[prev_start:prev_end].strip()
                cursor = prev_start
                if not prev_line:
                    continue
                if _PROGRAM_LINE_RE.match(prev_line):
                    follow_start = prev_start
                break
            next_start = follow_start
        blocks.append((program, int(anchor.group("pp")), block_start, next_start))
    return blocks


def _parse_pp_runner_block(
    block: str, program: str | None, race_year: int | None, track_code: str | None,
) -> tuple[Entry, list[str]]:
    """Parse the Del-Mar-style runner top-matter (identity only, tolerant)."""
    warnings: list[str] = []
    lines = [ln.rstrip() for ln in block.splitlines()]

    def _nonblank_after(start: int) -> list[tuple[int, str]]:
        return [(i, ln.strip()) for i, ln in enumerate(lines[start:], start) if ln.strip()]

    pp_idx = next((i for i, ln in enumerate(lines) if PP_ANCHOR_RE.match(ln.strip())), 0)
    seq = _nonblank_after(pp_idx + 1)
    values = [text for _, text in seq]

    odds = values[0] if values else None
    is_scratched = (odds or "").upper() in _SCRATCH_TOKENS

    morning_line = None
    cursor = 1
    if cursor < len(values) and values[cursor].upper().startswith("M:"):
        morning_line = values[cursor].split(":", 1)[1].strip() or None
        cursor += 1
    elif len(values) > 1 and re.match(r"^M:\s*", values[1], re.I):
        morning_line = re.sub(r"^M:\s*", "", values[1]).strip() or None
        cursor = 2

    horse_name = values[cursor].strip() if cursor < len(values) else None
    cursor += 1

    color = sex = med = None
    age: int | None = None
    if cursor < len(values):
        identity_match = _DMR_IDENTITY_RE.match(values[cursor])
        if identity_match:
            color = _clean(identity_match.group("color"))
            sex = _clean(identity_match.group("sex"))
            age = int(identity_match.group("age"))
            med = _clean(identity_match.group("med"))
            cursor += 1
        else:
            warnings.append(f"unrecognized identity line for {horse_name!r}: {values[cursor]!r}")

    def _take_person(idx: int) -> tuple[str | None, int]:
        if idx >= len(values):
            return None, idx
        name = values[idx].lstrip("*").strip() or None
        idx += 1
        if idx < len(values) and _PCT_RECORD_RE.match(values[idx]):
            idx += 1  # skip the "NN% w-p-s" strike-rate line
        return name, idx

    jockey, cursor = _take_person(cursor)
    trainer, cursor = _take_person(cursor)

    sire = values[cursor].strip() if cursor < len(values) else None
    dam = values[cursor + 1].strip() if cursor + 1 < len(values) else None
    # Guard against consuming the repeated "Horse" / "Sire - Dam" identity echo.
    if sire and sire.casefold() == (horse_name or "").casefold():
        sire = dam = None
    if dam and " - " in dam:
        dam = None

    breeder_match = re.search(r"(?mi)^BREEDER\s*$\s*^([^\n]+(?:\n(?!OWNER$)[^\n]+)?)$", block)
    owner_match = re.search(
        r"(?mi)^OWNER\s*$\s*^([^\n]+(?:\n(?!Record\b|ALL RACES|str\b|WORKOUTS)[^\n]+)?)$", block
    )
    breeder = _clean(breeder_match.group(1)) if breeder_match else None
    owner = _clean(owner_match.group(1)) if owner_match else None

    if horse_name is None:
        warnings.append("runner block has no resolvable horse name")

    profile = HorseProfile(
        age=age, sex=sex, color=color, sire=_clean(sire), dam=_clean(dam),
        breeder=breeder, raw_profile="\n".join(values[:12]) or None,
    )
    post_position = None
    if program and re.match(r"\d+", program):
        post_position = int(re.match(r"\d+", program).group())

    entry = Entry(
        program_number=_clean(program),
        horse_name=_clean(horse_name),
        post_position=post_position,
        weight=None,
        jockey=_clean(jockey),
        trainer=_clean(trainer),
        morning_line=_clean(morning_line),
        medication_weight_equipment=med,
        horse_profile=profile,
        owner=owner,
        breeder=breeder,
        record_splits=_record_splits(block, race_year, track_code),
        raw_program_number=_clean(program),
        raw_morning_line=_clean(morning_line),
        raw_block=block,
        is_scratched=is_scratched,
    )
    return entry, warnings


def parse_draftkings_markdown(
    source: str | Path, *, source_path: str | None = None, as_of: datetime | None = None,
) -> DraftKingsMarkdownCard:
    """Parse Markdown into raw-preserving staging records; validation is separate.

    Two-stage parse: Stage A segments and retains every runner block that carries
    identity evidence (``PP {n}`` anchor or compact header + scratch/top-matter),
    regardless of whether ALL RACES / WORKOUTS / SEE LESS are present.  Stage B
    attaches ALL RACES / WORKOUTS as optional child sections; a subsection parse
    failure yields warnings and completeness flags, never a dropped runner.
    """
    raw, resolved_path, raw_bytes = _read_source(source, source_path)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    race = _metadata(raw, resolved_path)
    race_year = race.race_date.year if race.race_date else None
    track_code = (race.track or "").strip()[:4].upper() or None

    entries: list[Entry] = []
    pps: list[PastPerformance] = []
    workouts: list[Workout] = []
    errors: list[str] = []
    warnings: list[str] = []
    open_sections: list[str] = []
    pp_header = workout_header = False

    pp_blocks = _split_pp_runner_blocks(raw)

    def _attach_history(
        entry: Entry, block: str, *, see_less_found: bool, truncation_is_fatal: bool
    ) -> None:
        nonlocal pp_header, workout_header
        has_all_races = "ALL RACES" in block
        has_workouts = "WORKOUTS" in block
        entry.has_all_races_section = has_all_races
        entry.has_workouts_section = has_workouts
        entry.runner_retained_without_history = not has_all_races
        try:
            entry_pps, entry_workouts, has_pp, has_wo = _parse_history(entry, block)
        except Exception as exc:  # pragma: no cover - defensive, exercised by fuzz tests
            entry.runner_parse_warnings.append(f"history subsection parse failed: {exc!r}")
            warnings.append(f"runner {entry.horse_name!r}: history subsection parse failed ({exc!r})")
            return
        if has_all_races and not entry_pps:
            entry.runner_parse_warnings.append("ALL RACES header present but no history rows parsed")
        if has_workouts and not entry_workouts:
            entry.runner_parse_warnings.append("WORKOUTS header present but no workout rows parsed")
        malformed_pp = sum(
            1 for row in entry_pps
            if row.distance is None and row.finish_position is None and row.program_or_post is None
        )
        if malformed_pp:
            entry.runner_parse_warnings.append(
                f"{malformed_pp} ALL RACES row(s) could not be parsed into a running line"
            )
        malformed_wo = sum(1 for row in entry_workouts if row.distance is None and row.time is None)
        if malformed_wo:
            entry.runner_parse_warnings.append(
                f"{malformed_wo} WORKOUTS row(s) could not be parsed"
            )
        pps.extend(entry_pps)
        workouts.extend(entry_workouts)
        pp_header = pp_header or has_pp
        workout_header = workout_header or has_wo
        if has_pp and not has_workouts:
            if truncation_is_fatal:
                open_sections.append("past_performances")
            else:
                entry.runner_parse_warnings.append("history section ended without a WORKOUTS block")
        if has_workouts and not see_less_found:
            if truncation_is_fatal:
                open_sections.append("workouts")
            else:
                entry.runner_parse_warnings.append("WORKOUTS section not closed by SEE LESS")

    if len(pp_blocks) >= 2:
        # ---- Del-Mar-style PP {n} layout -------------------------------------
        for program, _pp_number, block_start, block_end in pp_blocks:
            block = raw[block_start:block_end]
            entry, block_warnings = _parse_pp_runner_block(block, program, race_year, track_code)
            entry.block_source_span = (block_start, block_end)
            entry.runner_parse_warnings.extend(block_warnings)
            _attach_history(
                entry, block,
                see_less_found="SEE LESS" in block, truncation_is_fatal=False,
            )
            warnings.extend(f"runner {entry.horse_name!r}: {w}" for w in block_warnings)
            entries.append(entry)
    else:
        # ---- Compact ENTRY_RE layout ---------------------------------------
        candidates = list(ENTRY_RE.finditer(raw))
        retained: list[re.Match[str]] = []
        for index, match in enumerate(candidates):
            next_boundary = candidates[index + 1].start() if index + 1 < len(candidates) else len(raw)
            if _compact_block_has_identity(match, raw, next_boundary):
                retained.append(match)
        for index, match in enumerate(retained):
            next_start = retained[index + 1].start() if index + 1 < len(retained) else len(raw)
            boundary = raw.find("SEE LESS", match.end())
            scratched = (_clean(match.group("odds")) or "").upper() in _SCRATCH_TOKENS
            if boundary < 0 or boundary > next_start:
                see_less_found = False
                block_end = next_start
                message = (
                    f"runner {match.group('horse')!r} has no valid SEE LESS boundary "
                    f"before next runner/card end"
                )
                if scratched:
                    # A scratched runner is often truncated with no history / no
                    # SEE LESS; retain it and warn rather than fail the card.
                    warnings.append(message)
                else:
                    errors.append(message)
                    open_sections.append("runner")
            else:
                see_less_found = True
                block_end = boundary
            block = raw[match.start():block_end]
            entry = _parse_entry_block(match, block, race_year)
            entry.block_source_span = (match.start(), block_end)
            entry.is_scratched = scratched
            _attach_history(
                entry, block,
                see_less_found=see_less_found, truncation_is_fatal=not scratched,
            )
            entries.append(entry)

    expected_match = re.search(r"\b(\d+)\s+(?:runners|starters)\b", raw, re.I)
    return DraftKingsMarkdownCard(
        source_path=resolved_path, source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        source_format="draftkings_markdown", parser_version=PARSER_VERSION, as_of=as_of,
        race=race, entries=entries, past_performances=pps, workouts=workouts,
        parser_errors=errors, parser_warnings=warnings,
        expected_runner_count=int(expected_match.group(1)) if expected_match else None,
        declared_pp_header=pp_header, declared_workout_header=workout_header, open_sections=open_sections,
        raw_bytes=raw_bytes,
    )


def validate_draftkings_markdown_card(card: DraftKingsMarkdownCard) -> ValidationResult:
    """Validate card completeness and leakage rules.  Any error blocks scoring."""
    errors = list(card.parser_errors)
    warnings = list(card.parser_warnings)
    race = card.race
    for label, value in (("track", race.track), ("race number", race.race_number), ("race date/as_of date", race.race_date), ("surface", race.surface), ("distance", race.distance)):
        if value is None or value == "":
            errors.append(f"race identity missing required {label}")
    if not card.entries:
        errors.append("no entries parsed")
    program_seen: set[str] = set()
    horse_seen: set[str] = set()
    for entry in card.entries:
        program = (entry.program_number or "").lower()
        horse = (entry.horse_name or "").lower()
        if program and program in program_seen:
            errors.append(f"duplicate program-number entry identity: {entry.program_number}")
        if horse and horse in horse_seen:
            errors.append(f"duplicate horse-name entry identity: {entry.horse_name}")
        program_seen.add(program)
        horse_seen.add(horse)
        for label, value in (("horse name", entry.horse_name), ("jockey", entry.jockey), ("trainer", entry.trainer), ("weight", entry.weight)):
            if value is None or value == "":
                errors.append(f"entry {entry.program_number or '?'} missing required {label}")
        if entry.horse_profile.sire is None or entry.horse_profile.dam is None:
            warnings.append(f"entry {entry.program_number or '?'} has optional pedigree fields unavailable")
    for row in card.past_performances:
        missing = [label for label, value in (("date", row.start_date), ("race track", row.track), ("distance", row.distance), ("surface-condition", row.surface_condition), ("finish position", row.finish_position), ("class", row.race_class)) if value is None or value == ""]
        if missing:
            errors.append(f"materially incomplete PP row for {row.horse_name}: missing {', '.join(missing)}")
        if row.start_date is not None and row.start_date >= card.as_of.date():
            errors.append(f"PP row for {row.horse_name} is on or after as_of ({row.start_date.isoformat()})")
    for row in card.workouts:
        missing = [label for label, value in (("date", row.work_date), ("distance", row.distance), ("time", row.time)) if value is None or value == ""]
        if missing:
            errors.append(f"materially incomplete workout row for {row.horse_name}: missing {', '.join(missing)}")
        if row.work_date is not None and row.work_date >= card.as_of.date():
            errors.append(f"workout row for {row.horse_name} is on or after as_of ({row.work_date.isoformat()})")
    if card.declared_pp_header and not card.past_performances:
        errors.append("ALL RACES table header present but zero valid PP rows parsed")
    if card.declared_workout_header and not card.workouts:
        errors.append("WORKOUTS table header present but zero valid workout rows parsed")
    if card.open_sections:
        errors.append(f"parser terminated in known open section(s): {', '.join(sorted(set(card.open_sections)))}")
    if card.expected_runner_count is not None and card.expected_runner_count != len(card.entries):
        errors.append(f"parsed runner count {len(card.entries)} conflicts with declared runner count {card.expected_runner_count}")
    identifier = f"{race.track or '?'}-{race.race_date.isoformat() if race.race_date else '?'}-R{race.race_number or '?'}"
    return ValidationResult(
        source_path=card.source_path, source_format=card.source_format, source_sha256=card.source_sha256,
        parser_version=card.parser_version, validation_timestamp=datetime.now(timezone.utc).isoformat(),
        race_identifier=identifier, expected_runner_count=card.expected_runner_count,
        parsed_unique_runner_count=len({(entry.program_number, entry.horse_name) for entry in card.entries}),
        past_performance_row_count=len(card.past_performances), workout_row_count=len(card.workouts),
        errors=list(dict.fromkeys(errors)), warnings=list(dict.fromkeys(warnings)), passed=not errors,
    )


def require_scoring_ready(card: DraftKingsMarkdownCard, result: ValidationResult | None = None) -> DraftKingsMarkdownCard:
    """The mandatory integrity boundary before feature generation or scoring."""
    result = result or validate_draftkings_markdown_card(card)
    if not result.passed:
        raise DraftKingsMarkdownValidationError("DraftKings Markdown card is not scoring-ready: " + "; ".join(result.errors))
    return card


def validated_feature_staging_records(card: DraftKingsMarkdownCard, result: ValidationResult | None = None) -> dict[str, Any]:
    """Return staging records only after the fail-closed scoring-readiness gate."""
    require_scoring_ready(card, result)
    return {"race": card.race, "entries": card.entries, "past_performances": card.past_performances, "workouts": card.workouts}


def reconcile_draftkings_excel(
    path: str | Path | bytes, *, source_filename: str | None = None,
) -> ExcelReconciliationResult:
    """Inspect a rendered DK workbook without making it an ingestion dependency.

    The workbook is intentionally treated as visual-layout evidence: this
    reports recognizable markers only and never emits feature-ready records.
    """
    if isinstance(path, bytes):
        raw = path
        source_path = source_filename or "<in-memory-excel>"
    else:
        workbook_path = Path(path)
        raw = workbook_path.read_bytes()
        source_path = str(workbook_path)
    warnings: list[str] = []
    try:
        from openpyxl import load_workbook
    except ImportError:
        return ExcelReconciliationResult(
            source_path=source_path, source_sha256=hashlib.sha256(raw).hexdigest(),
            worksheets_scanned=0, recognizable_runner_records=0,
            recognizable_past_performance_records=0, recognizable_workout_records=0,
            warnings=["openpyxl unavailable; Excel reconciliation not run"],
        )
    workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    runner_records = pp_records = workout_records = 0
    for sheet in workbook.worksheets:
        section: str | None = None
        for row in sheet.iter_rows(values_only=True):
            line = " ".join(str(value).strip() for value in row if value is not None).strip()
            if not line:
                continue
            upper = line.upper()
            if "ALL RACES" in upper:
                section = "past_performances"
                continue
            if "WORKOUTS" in upper:
                section = "workouts"
                continue
            if "SEE LESS" in upper:
                section = None
                continue
            if re.search(r"\bRUNNER\b", upper) and re.search(r"\bJOCKEY\b", upper):
                section = "entries"
                continue
            if section == "entries" and re.match(r"^\d{1,2}[A-Z]?\b", line):
                runner_records += 1
            elif section == "past_performances" and DATE_RE.search(line):
                pp_records += 1
            elif section == "workouts" and DATE_RE.search(line):
                workout_records += 1
    if not pp_records or not workout_records:
        warnings.append("rendered workbook lacks one or more recognizable PP/workout headers")
    return ExcelReconciliationResult(
        source_path=source_path, source_sha256=hashlib.sha256(raw).hexdigest(),
        worksheets_scanned=len(workbook.worksheets), recognizable_runner_records=runner_records,
        recognizable_past_performance_records=pp_records,
        recognizable_workout_records=workout_records, warnings=warnings,
    )
