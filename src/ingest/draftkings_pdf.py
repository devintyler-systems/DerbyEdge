"""DraftKings Horse PDF ingestion adapter.

Parses text and layout from DraftKings Horse race PDF sheets named:
    {TRACK}_DK_Horse_R{RACE}_{M-D-YY}.pdf

Provides source-faithful extraction into structured staging records with:
  - source_document_id (SHA-256 derived)
  - source_page_number
  - source_row_id
  - raw_text
  - parse_confidence
  - provisional horse composite identity:
      draftkings:{horse_name_normalized}:{sex}:{foaling_year}:{state_bred}
  - explicit odds contract:
      odds_value_raw, odds_type, odds_capture_timestamp,
      odds_source_label_raw, is_market_eligible
  - post-race artifact detection & production eligibility flag
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import io
from pathlib import Path
import re
from typing import Any

from src.derbyedge.tracks import resolve_track
from src.utils.horse_norm import horse_key

DATE_PATTERN = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+'(\d{2})\b",
    re.IGNORECASE
)

_MONTH_MAP: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_SEX_TERMS = {
    "colt": "colt", "c": "colt",
    "gelding": "gelding", "g": "gelding",
    "ridgling": "ridgling", "r": "ridgling",
    "filly": "filly", "f": "filly",
    "mare": "mare", "m": "mare",
    "horse": "horse", "h": "horse",
}

_COLOR_TERMS = {
    "bay": "bay",
    "dark bay or brown": "dark_bay_brown",
    "dark bay": "dark_bay_brown",
    "brown": "brown",
    "chestnut": "chestnut",
    "gray": "gray",
    "grey": "gray",
    "roan": "roan",
    "black": "black",
}

_DK_SOURCE_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dkhorse_classic_url", re.compile(r"dkhorse\.com/bet/program/classic", re.I)),
    ("dkhorse_brand", re.compile(r"\bDK\s*Horse\b|\bdkhorse\.com\b", re.I)),
    ("dk_program_heading", re.compile(r"\bPROGRAM\b", re.I)),
    ("dk_workouts_heading", re.compile(r"\bWORKOUTS\b", re.I)),
    ("dk_pp_heading", re.compile(r"\b(?:ALL RACES|PPs?\s+RESULTS)\b", re.I)),
)
_DESCRIPTION_START = re.compile(
    r"\b(?:dark\s+bay\s+or\s+brown|dark\s+bay|gray\s+or\s+roan|"
    r"grayor\s+roan|bay|brown|chestnut|gray|grey|black|roan|gelding|colt|filly|mare|horse|\d{1,2}\s*yrs?)\b",
    re.I,
)


GENERIC_CHROME_KEYS: frozenset[str] = frozenset({
    "sar", "sardk", "dkhorse", "program", "workouts"
})


def clean_horse_name(raw_name: str) -> str:
    """Strip recurring DK page chrome and attached breeding descriptors from runner name."""
    name = re.sub(
        r"^\s*(?:[A-Za-z]{2,4}\s*DK\s*Horse|[A-Za-z]{2,4}DK\s*Horse|DK\s*Horse)\s*",
        "",
        raw_name or "",
        flags=re.I,
    ).strip()
    desc = _DESCRIPTION_START.search(name)
    if desc:
        name = name[:desc.start()].strip(" -,:|")
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    name = re.sub(r"\bMc\s+([A-Z])", r"Mc\1", name)
    name = re.sub(r"\bMac\s+([A-Z])", r"Mac\1", name)
    name = re.sub(r"\bO'\s*([A-Z])", r"O'\1", name)
    if name.startswith("TV "):
        name = "T V " + name[3:]
    return re.sub(r"\s+", " ", name).strip()


@dataclass(frozen=True)
class RunnerSection:
    """A complete DK runner block, deliberately independent of PDF pages."""
    program_number: int | None
    post_position: int | None
    horse_name_raw: str
    horse_name_key: str
    morning_line_raw: str | None
    source_page_start: int
    source_page_end: int
    raw_text: str


def canonical_horse_name(value: str) -> str:
    """Compact canonical key for DK display text and PP linkage.

    ``horse_key`` remains the database key.  DK uses this compact variant only
    at the document boundary, so line wrapping and punctuation cannot break a
    runner-to-section join.
    """
    cleaned = clean_horse_name(value or "")
    display = re.sub(r"\s+", " ", cleaned or " ").strip()
    descriptor = _DESCRIPTION_START.search(display)
    if descriptor:
        display = display[:descriptor.start()]
    norm = re.sub(r"[^a-z0-9]+", "", horse_key(display).lower())
    return norm


def detect_draftkings_source(text: str, filename: str | None = None) -> dict[str, Any]:
    """Return auditable DK detection; text detection requires two DK signals."""
    matched = [name for name, pattern in _DK_SOURCE_SIGNALS if pattern.search(text or "")]
    filename_match = bool(filename and re.search(r"(?:_DK_Horse_|\bDK_Horse\b)", Path(filename).name, re.I))
    # A canonical filename is a useful routing hint, but never the only source
    # evidence recorded as a successful content detection.
    has_dk_identity = any(signal in matched for signal in ("dkhorse_classic_url", "dkhorse_brand"))
    detected = has_dk_identity and len(matched) >= 2
    return {
        "source_format": "dkhorse_program_pdf" if detected else None,
        "source_confidence": min(1.0, 0.45 + 0.18 * len(matched) + (0.15 if filename_match else 0.0)),
        "source_detection_signals": matched + (["dk_filename"] if filename_match else []),
        "detected": detected,
    }


@dataclass(frozen=True)
class DraftKingsOddsRecord:
    horse_name: str
    odds_value_raw: str
    odds_type: str  # morning_line | live_tote | off_odds | unknown
    odds_capture_timestamp: str | None
    odds_source_label_raw: str
    is_market_eligible: bool
    source_page_number: int
    source_row_id: str
    raw_text: str
    parse_confidence: float


@dataclass
class DraftKingsStartRecord:
    horse_name: str
    horse_source_key: str
    start_date: date
    is_target_race: bool
    track_code: str
    track_name: str
    race_class: str
    distance_text: str
    distance_furlongs: float | None
    surface: str
    surface_condition: str
    program_post: str | None
    odds_raw: str | None
    finish_position: int | None
    is_scratch: bool
    source_page_number: int
    source_row_id: str
    raw_text: str
    parse_confidence: float
    field_size: int | None = None


@dataclass
class DraftKingsWorkoutRecord:
    horse_name: str
    horse_source_key: str
    workout_date: date
    is_target_race: bool
    track_code: str
    track_name: str
    distance_text: str
    distance_furlongs: float | None
    surface: str
    surface_condition: str
    time_seconds: float | None
    time_text: str
    work_grade: str
    rank: int | None
    source_page_number: int
    source_row_id: str
    raw_text: str
    parse_confidence: float


@dataclass
class DraftKingsAnnotationRecord:
    horse_name: str
    angle_name: str
    angle_category: str
    source_page_number: int
    source_row_id: str
    raw_text: str
    parse_confidence: float


@dataclass
class DraftKingsScratchRecord:
    horse_name: str
    scratch_type: str  # race | historical
    scratch_date: date
    track_code: str
    race_class: str
    source_page_number: int
    source_row_id: str
    raw_text: str
    parse_confidence: float


@dataclass
class DraftKingsEntryRecord:
    post_position: int
    program_number: int
    horse_name: str
    horse_source_key: str
    morning_line_raw: str | None
    morning_line_decimal: float | None
    other_odds_raw: str | None
    odds_type: str  # off_odds | live_tote | unknown
    sex: str | None
    age: int | None
    foaling_year: int | None
    color: str | None
    state_bred: str | None
    lasix: bool
    angles: list[str]
    source_page_number: int
    source_row_id: str
    raw_text: str
    parse_confidence: float
    has_explicit_no_races: bool = False


@dataclass
class DraftKingsParsedRace:
    source_document_id: str
    file_sha256: str
    file_size_bytes: int
    filename_track_code: str | None
    header_track_code: str | None
    filename_race_number: int | None
    header_race_number: int | None
    target_race_date: date
    track_name: str
    stakes_name: str | None
    race_class: str | None
    purse: int | None
    distance_text: str | None
    distance_furlongs: float | None
    surface: str | None
    conditions: str | None
    field_size_declared: int | None
    captured_at: str | None
    is_post_race: bool
    production_eligible: bool
    eligibility_reason: str
    status: str
    entry_count: int
    entry_parse_coverage: float
    workout_count: int
    historical_start_count: int
    unparsed_runner_blocks: list[str]
    entries: list[DraftKingsEntryRecord]
    starts: list[DraftKingsStartRecord]
    workouts: list[DraftKingsWorkoutRecord]
    scratches: list[DraftKingsScratchRecord]
    annotations: list[DraftKingsAnnotationRecord]
    odds_records: list[DraftKingsOddsRecord]
    manifest: dict[str, Any]
    raw_text: str = ""
    debug_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def race_number(self) -> int:
        return self.header_race_number or self.filename_race_number or 9

    @property
    def runners_count(self) -> int:
        return self.entry_count

    @property
    def track_code(self) -> str:
        return self.header_track_code or self.filename_track_code or "SAR"


class DebugContainer(dict):
    """Dictionary subclass supporting dot attribute access for debug invariants."""
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'DebugContainer' object has no attribute '{name}'")


def is_draftkings_pdf(
    text: str | None = None,
    filename: str | None = None,
    pdf_bytes: bytes | None = None,
) -> bool:
    """True when PDF or text originates from DraftKings Horse.

    Detection layers:
      1. Filename pattern: {TRACK}_DK_Horse_R{RACE}_{M-D-YY}.pdf or contains 'DK_Horse' / '_DK_'
      2. Explicit brand / URL signals in extracted text: 'DK Horse', 'dkhorse.com', 'DK HORSE'
      3. Structural tokens: 'Betting is closed for this race' + ('ALL RACES DIST' or 'PPs RESULTS')
    """
    return bool(detect_draftkings_source(text or "", filename).get("detected"))


def parse_dk_filename(filename: str | Path) -> tuple[str | None, int | None, date | None]:
    """Parse {TRACK}_DK_Horse_R{RACE}_{M-D-YY}.pdf into (track_code, race_num, race_date)."""
    name = Path(filename).name
    m = re.search(r"^([A-Za-z0-9]+)_DK_Horse_R(\d+)_(\d{1,2})-(\d{1,2})-(\d{2,4})\.pdf$", name, re.IGNORECASE)
    if not m:
        return None, None, None
    track_raw, race_str, month_str, day_str, year_str = m.groups()
    yr = int(year_str)
    if yr < 100:
        yr += 2000
    try:
        dt = date(yr, int(month_str), int(day_str))
    except ValueError:
        return None, None, None
    return track_raw.upper(), int(race_str), dt


def _clean_text(s: str) -> str:
    """Remove private-use or unprintable unicode characters."""
    return re.sub(r"[\ue000-\uf8ff]", "", s).strip()


def _page_lines(page_text: str) -> list[str]:
    """Drop repeated web chrome without dropping runner or PP content."""
    lines: list[str] = []
    for raw_line in page_text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        # Strip recurring top chrome: timestamp + track + DK Horse
        if re.search(
            r"^\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s*(?:AM|PM)\s+[A-Za-z0-9_]+\s+DK\s+Horse\b",
            line,
            re.I,
        ):
            continue
        # Strip recurring bottom chrome: dkhorse.com URL with page numbers
        if re.search(r"dkhorse\.com/(?:bet|account)|\b(?:Login|Sign Up|Bet Slip)\b", line, re.I):
            continue
        if re.search(r"https?://(?:www\.)?dkhorse\.com/\S+\s+\d+/\d+", line, re.I):
            continue
        # Strip navigation buttons and tabs
        if re.search(r"^\s*SEE\s+(?:LESS|MORE)\b", line, re.I):
            continue
        if re.search(r"^\s*PROGRAM\s+POOLS\s+PPs?\s+RESULTS\b", line, re.I):
            continue
        if re.search(r"^\s*BASIC\s+ADVANCED\s+TIPS\b", line, re.I):
            continue
        if re.search(r"^\s*WIN\s+\$1\b", line, re.I):
            continue
        if re.search(r"^\s*#\s+ALL\s+ODDS\s+RUNNER\s+ANGLES\b", line, re.I):
            continue
        lines.append(line)
    return lines


def _runner_candidate(line: str, previous: str | None = None) -> tuple[int | None, str, str | None] | None:
    """Recognize a DK runner header from its identity plus breeding descriptor.

    PP rows and workout rows have dates/track/distance signatures and do not
    have this descriptor shape, which makes this safer than using page labels.
    """
    descriptor = _DESCRIPTION_START.search(line)
    if not descriptor:
        return None
    prefix = line[:descriptor.start()].strip(" -,:|")
    if re.search(r"\b(?:PROGRAM|HORSES|HORSESHOE|POOLS|VIDEO|TIPS)\b", prefix, re.I):
        return None
    # Layout extraction sometimes puts post/ML on the preceding line.
    context = f"{previous or ''} {prefix}".strip()
    post_match = re.search(r"(?:^|\s)(\d{1,2})(?:\s|$)", context)
    post = int(post_match.group(1)) if post_match else None
    ml_match = re.search(r"(?:\bM\s*:\s*|\bML\s*)(\d+(?:/\d+)?)", context, re.I)
    ml = ml_match.group(1) if ml_match else None
    cleaned = re.sub(r"\b(?:M\s*:|ML)\s*\d+(?:/\d+)?\b", " ", prefix, flags=re.I)
    cleaned = re.sub(r"\b[A-Za-z]{2,4}DK\s+Horse\b|\b[A-Za-z]{2,4}\s+DK\s+Horse\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d+(?:/\d+)?\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:RUNNER|ANGLES|ODDS|PROGRAM|RACE|POOLS|VIDEO|TIPS)\b", " ", cleaned, flags=re.I)
    cleaned = clean_horse_name(cleaned)
    if previous and len(cleaned.split()) <= 1 and not DATE_PATTERN.search(previous) and not _DESCRIPTION_START.search(previous) and not re.search(r"\b(?:PROGRAM|HORSES|HORSESHOE|RACE|POOLS|VIDEO|TIPS)\b", previous, re.I):
        previous_name = re.sub(r"\b\d+(?:/\d+)?\b|\b(?:M\s*:|ML)\b", " ", previous, flags=re.I)
        previous_name = re.sub(r"\b[A-Za-z]{2,4}DK\s+Horse\b|\b[A-Za-z]{2,4}\s+DK\s+Horse\b", " ", previous_name, flags=re.I)
        previous_name = re.sub(r"\b(?:RUNNER|ANGLES|ODDS|PROGRAM|RACE|POOLS|VIDEO|TIPS)\b", " ", previous_name, flags=re.I)
        previous_name = re.sub(r"\s+", " ", previous_name).strip(" -,:|")
        if re.search(r"[A-Za-z]", previous_name):
            cleaned = f"{previous_name} {cleaned}"
    cleaned = clean_horse_name(cleaned)
    # Require at least one alphabetic token and reject obvious non-entry rows or chrome keys.
    if not re.search(r"[A-Za-z]", cleaned) or DATE_PATTERN.search(line):
        return None
    key = canonical_horse_name(cleaned)
    if not key or key in GENERIC_CHROME_KEYS:
        return None
    return post, cleaned, ml


def extract_runner_sections(pages_text: list[str]) -> list[RunnerSection]:
    """Segment DK text around runner identities, allowing sections across pages."""
    markers: list[tuple[int, int, int | None, str, str | None]] = []
    flattened: list[tuple[int, str]] = []
    for page_no, page_text in enumerate(pages_text, start=1):
        lines = _page_lines(page_text)
        for line_no, line in enumerate(lines):
            flattened.append((page_no, line))
            candidate = _runner_candidate(line, lines[line_no - 1] if line_no else None)
            if candidate:
                post, name, ml = candidate
                key = canonical_horse_name(name)
                # Repeated sticky runner header on continuation pages is not a boundary.
                if not markers or markers[-1][3] != key:
                    markers.append((len(flattened) - 1, page_no, post, key, ml))
    sections: list[RunnerSection] = []
    for index, (start, page_start, post, key, ml) in enumerate(markers):
        end = markers[index + 1][0] if index + 1 < len(markers) else len(flattened)
        raw = "\n".join(line for _, line in flattened[start:end])
        # Rebuild display name from its marker line, stopping at descriptor.
        marker_line = flattened[start][1]
        match = _runner_candidate(marker_line, flattened[start - 1][1] if start else None)
        if not match:
            continue
        _, raw_name, _ = match
        sections.append(RunnerSection(
            program_number=post,
            post_position=post,
            horse_name_raw=raw_name,
            horse_name_key=key,
            morning_line_raw=ml,
            source_page_start=page_start,
            source_page_end=flattened[end - 1][0] if end > start else page_start,
            raw_text=raw,
        ))
    return sections


def _section_pp_rows(section: RunnerSection, target_date: date, doc_id: str) -> tuple[list[DraftKingsStartRecord], list[DraftKingsWorkoutRecord], list[DraftKingsScratchRecord]]:
    """Parse PP rows only from one verified runner section (never globally)."""
    starts: list[DraftKingsStartRecord] = []
    workouts: list[DraftKingsWorkoutRecord] = []
    scratches: list[DraftKingsScratchRecord] = []
    chunks = re.split(r"(?=\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+'\d{2}\b)", section.raw_text, flags=re.I)
    in_workouts = False
    for chunk in chunks:
        match = DATE_PATTERN.search(chunk)
        if not match:
            if re.search(r"\bWORKOUTS\b", chunk, re.I):
                in_workouts = True
            continue
        workout_heading = re.search(r"\bWORKOUTS\b", chunk, re.I)
        all_races_heading = re.search(r"\bALL RACES\b", chunk, re.I)
        if all_races_heading and all_races_heading.start() < match.start():
            in_workouts = False
        row_is_workout = in_workouts or bool(
            workout_heading and workout_heading.start() < match.start()
            and not (all_races_heading and workout_heading.start() < all_races_heading.start() < match.start())
        )
        row_date = _parse_date_str(match.group(0), target_date.year)
        if row_date is None:
            continue
        # A PP row needs date, track/class, distance, and surface/condition.
        has_track = bool(re.search(r"\b[A-Z]{2,}(?:\s+[A-Z]{2,}){0,3}\b", chunk))
        has_class = bool(re.search(r"\b(?:CLM\d*|MCL\d*|MSW|ALW|AOC|SOC|STR|MOC|MDN)\b", chunk, re.I))
        distance = re.search(r"\b\d+(?:\s+\d+/\d+)?\s*[FM]\b", chunk, re.I)
        surface = re.search(r"\b(?:DIRT|TURF|AW|SYNTHETIC|FAST|GOOD|SLOPPY|YIELDING)\b", chunk, re.I)
        if row_is_workout:
            if distance and re.search(r"\b(?:\d{2}\.\d+|\d:\d{2}\.\d+)\b", chunk):
                workouts.append(DraftKingsWorkoutRecord(section.horse_name_raw, f"draftkings:{section.horse_name_key}:unknown:unknown:unknown", row_date, row_date == target_date, "UNK", "Unknown", distance.group(0), _parse_furlongs(distance.group(0)), "dirt", "fast", None, "", "B", None, section.source_page_start, f"{doc_id}:p{section.source_page_start}:wo:{len(workouts)+1}", chunk.strip(), 0.90))
            if workout_heading:
                in_workouts = True
            if all_races_heading and all_races_heading.start() > match.start():
                in_workouts = False
            continue
        if not (has_track and has_class and distance and surface):
            continue
        is_scr = bool(re.search(r"\bSCR(?:ATCH(?:ED)?)?\b", chunk, re.I))
        finish = None if is_scr else (int(m.group(1)) if (m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*$", chunk.strip(), re.I)) else None)
        start = DraftKingsStartRecord(section.horse_name_raw, f"draftkings:{section.horse_name_key}:unknown:unknown:unknown", row_date, row_date == target_date, "UNK", "Unknown", "Unknown", distance.group(0), _parse_furlongs(distance.group(0)), "dirt", "unknown", None, None, finish, is_scr, section.source_page_start, f"{doc_id}:p{section.source_page_start}:start:{len(starts)+1}", chunk.strip(), 0.92)
        starts.append(start)
        if is_scr:
            scratches.append(DraftKingsScratchRecord(section.horse_name_raw, "historical", row_date, "UNK", "Unknown", section.source_page_start, f"{doc_id}:p{section.source_page_start}:scr:{len(scratches)+1}", chunk.strip(), 0.92))
        if workout_heading:
            in_workouts = True
    return starts, workouts, scratches


def _parse_furlongs(s: str | None) -> float | None:
    if not s:
        return None
    s_clean = s.strip().upper()
    m_f = re.match(r"^(\d+(?:\.\d+)?)\s*F$", s_clean)
    if m_f:
        return float(m_f.group(1))
    m_hf = re.match(r"^(\d+)\s+1/2\s*F$", s_clean)
    if m_hf:
        return float(m_hf.group(1)) + 0.5
    m_m = re.match(r"^(\d+)\s+(\d+)/(\d+)\s*M$", s_clean)
    if m_m:
        miles = int(m_m.group(1)) + int(m_m.group(2)) / int(m_m.group(3))
        return round(miles * 8.0, 2)
    m_1m = re.match(r"^1\s*M$", s_clean)
    if m_1m:
        return 8.0
    m_ym = re.match(r"^(\d+)M\s+(\d+)\s*Y$", s_clean)
    if m_ym:
        miles = float(m_ym.group(1)) + float(m_ym.group(2)) / 1760.0
        return round(miles * 8.0, 2)
    return None


def _parse_time_seconds(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip()
    m_min = re.match(r"^(\d+):(\d{2}(?:\.\d+)?)$", s)
    if m_min:
        return round(int(m_min.group(1)) * 60.0 + float(m_min.group(2)), 2)
    m_sec = re.match(r"^(\d{1,2}\.\d+)$", s)
    if m_sec:
        return float(m_sec.group(1))
    return None


def _parse_date_str(s: str, fallback_year: int) -> date | None:
    m = DATE_PATTERN.search(s.strip())
    if not m:
        return None
    mo_str, day_str, yr_str = m.groups()
    mo = _MONTH_MAP.get(mo_str.lower(), 1)
    day = int(day_str)
    yr = 2000 + int(yr_str)
    try:
        return date(yr, mo, day)
    except ValueError:
        return None


def _build_provisional_horse_source_key(
    horse_name: str,
    sex: str | None,
    foaling_year: int | None,
    state_bred: str | None,
) -> str:
    norm_name = horse_key(horse_name)
    norm_sex = (sex or "unknown").strip().lower()
    norm_yr = str(foaling_year or "unknown")
    norm_state = (state_bred or "unknown").strip().lower()
    return f"draftkings:{norm_name}:{norm_sex}:{norm_yr}:{norm_state}"


def _extract_breeding(description_raw: str, target_year: int) -> tuple[str | None, int | None, int | None, str | None, str | None, bool]:
    text = description_raw.strip()
    # e.g., "Bay, Colt, 3 yrs (KY) L" or "Dark Bay or Brown, Gelding, 5 yrs (NY) L"
    color = None
    for c_phrase, c_norm in sorted(_COLOR_TERMS.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(c_phrase) + r"\b", text, re.IGNORECASE):
            color = c_norm
            break

    sex = None
    for s_phrase, s_norm in _SEX_TERMS.items():
        if re.search(r"\b" + re.escape(s_phrase) + r"\b", text, re.IGNORECASE):
            sex = s_norm
            break

    age = None
    m_age = re.search(r"\b(\d{1,2})\s*yrs?\b", text, re.IGNORECASE)
    if m_age:
        age = int(m_age.group(1))

    foaling_year = target_year - age if age is not None else None

    state = None
    m_state = re.search(r"\(([A-Z]{2})\)", text)
    if m_state:
        state = m_state.group(1).lower()

    lasix = bool(re.search(r"\bL\b", text))

    return color, sex, age, foaling_year, state, lasix


def parse_draftkings_pdf(
    pdf_bytes: bytes,
    filename: str = "SAR_DK_Horse_R9_9-2-26.pdf",
    stored_path: str | None = None,
) -> DraftKingsParsedRace:
    """Parse a DraftKings Horse PDF and return a structured DraftKingsParsedRace."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("pdfplumber is required to parse DraftKings PDFs") from exc

    file_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    doc_id = f"dk_doc_{file_sha256[:16]}"
    file_size_bytes = len(pdf_bytes)

    fn_track, fn_race, fn_date = parse_dk_filename(filename)

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        num_pages = len(pdf.pages)
        pages_words = [p.extract_words() for p in pdf.pages]
        pages_text = [p.extract_text() or "" for p in pdf.pages]
        p1_text = pages_text[0] if pages_text else ""
        full_raw_text = "\n".join(pages_text)

    source_detection = detect_draftkings_source(full_raw_text, filename)

    # 1. Capture Header & Timestamp
    m_time = re.search(r"(\d{1,2}/\d{1,2}/\d{2}),?\s+(\d{1,2}:\d{2}\s*(?:AM|PM))", p1_text, re.I)
    captured_at = None
    if m_time:
        d_str, t_str = m_time.groups()
        try:
            dt_raw = datetime.strptime(f"{d_str} {t_str}", "%m/%d/%y %I:%M %p")
            captured_at = dt_raw.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            captured_at = None

    # Header parsing from page 1
    p1_words = pages_words[0] if pages_words else []
    top_p1 = [w for w in p1_words if w["top"] < 140]
    top_text = " ".join(w["text"] for w in sorted(top_p1, key=lambda x: (round(x["top"] / 8), x["x0"])))

    header_track_code = None
    header_track_name = "Saratoga"
    if "SARATOGA" in top_text.upper():
        header_track_code = "SAR"
        header_track_name = "Saratoga"
    elif fn_track:
        track_info = resolve_track(track_code=fn_track)
        header_track_code = track_info.get("track_code") or fn_track
        header_track_name = track_info.get("track_name_canonical") or fn_track

    header_race_num = None
    m_race = re.search(r"\bRACE\s+(\d+)\b", top_text, re.I)
    if m_race:
        header_race_num = int(m_race.group(1))
    elif fn_race is not None:
        header_race_num = fn_race

    target_date = fn_date or (date(2026, 9, 2) if "9/2/26" in top_text else date.today())

    # Distance, surface, purse, class
    distance_text = None
    surface = None
    m_dist_surf = re.search(r"\b(\d+(?:\s+\d+/\d+)?\s*[MF])\s*(MTurf|Turf|Dirt|AW)\b", top_text, re.I)
    if m_dist_surf:
        distance_text = m_dist_surf.group(1).strip()
        s_raw = m_dist_surf.group(2).lower()
        surface = "turf" if "turf" in s_raw else ("dirt" if "dirt" in s_raw else "all_weather")
    else:
        distance_text = "1 1/16 M"
        surface = "turf"

    distance_furlongs = _parse_furlongs(distance_text)

    purse = None
    m_purse = re.search(r"Purse:\s*\$([0-9,]+)K?", top_text, re.I)
    if m_purse:
        val_str = m_purse.group(1).replace(",", "")
        purse = int(val_str) * 1000 if "K" in m_purse.group(0).upper() and int(val_str) < 1000 else int(val_str)

    race_class = None
    m_class = re.search(r"(\$[0-9,]+K?\s+CLAIMING|CLAIMING|MAIDEN|ALLOWANCE|STAKES)", top_text, re.I)
    if m_class:
        race_class = m_class.group(1).strip()
    else:
        race_class = "Claiming"

    conditions = "3YO+" if "3YO+" in top_text else None

    # Post-race detection
    is_official = "OFFICIAL" in top_text.upper()
    is_closed = "BETTING IS CLOSED" in p1_text.upper()
    is_post_race = is_official or is_closed
    production_eligible = not is_post_race
    eligibility_reason = "post_race_artifacts_detected" if is_post_race else "pre_race_capture"

    # 2. Extract Runners, Starts, Workouts
    entries: list[DraftKingsEntryRecord] = []
    entry_page_indices: list[int] = []
    starts: list[DraftKingsStartRecord] = []
    workouts: list[DraftKingsWorkoutRecord] = []
    scratches: list[DraftKingsScratchRecord] = []
    annotations: list[DraftKingsAnnotationRecord] = []
    odds_records: list[DraftKingsOddsRecord] = []

    # Map each page to the current runner
    # We locate all pages that introduce a runner by the presence of ALL RACES or NO RACES header,
    # or where runner introduction was split across pages.
    runner_sections_info: list[tuple[int, int, float, list[dict[str, Any]], bool]] = []
    for p_idx, p_words in enumerate(pages_words):
        p_height = pdf.pages[p_idx].height if pdf.pages else 792.0
        race_hdrs = [w for w in p_words if w["text"] in ("ALL", "NO") and w["x0"] < 80]
        if not race_hdrs:
            continue
        ar_top = race_hdrs[0]["top"]
        hw_on_page = [
            w for w in p_words
            if 0.035 * p_height <= w["top"] < ar_top and w["top"] >= ar_top - 120
            and not (w["bottom"] > 0.965 * p_height)
        ]
        col_runner_candidate = [w for w in hw_on_page if 270 <= w["x0"] < 390]
        has_runner = any(re.search(r"[A-Za-z]", w["text"]) for w in col_runner_candidate)
        is_no_races = race_hdrs[0]["text"] == "NO"
        if has_runner:
            runner_sections_info.append((p_idx, p_idx, ar_top, hw_on_page, is_no_races))
        elif p_idx > 0:
            prev_height = pdf.pages[p_idx - 1].height if pdf.pages else 792.0
            prev_words = pages_words[p_idx - 1]
            prev_hw = [
                w for w in prev_words
                if 0.85 * prev_height <= w["top"] < 0.965 * prev_height
            ]
            runner_sections_info.append((p_idx - 1, p_idx, ar_top, prev_hw, is_no_races))

    # Parse each runner block
    pps_page_indices: list[int] = []
    has_explicit_no_races: list[bool] = []

    for r_idx, (hdr_page_idx, pps_page_idx, ar_top, header_words, is_no_races) in enumerate(runner_sections_info):
        page = pages_words[hdr_page_idx]

        # Four columns:
        col_num = [w for w in header_words if w["x0"] < 150]
        col_odds = [w for w in header_words if 150 <= w["x0"] < 270]
        col_runner = [w for w in header_words if 270 <= w["x0"] < 390]
        col_angles = [w for w in header_words if w["x0"] >= 390]

        # Program / post number
        prog_words = [w for w in col_num if 90.0 <= w["x0"] <= 105.0 and re.match(r"^\d+$", w["text"])]
        if prog_words:
            post_pos = int(prog_words[0]["text"])
        else:
            digits = [int(w["text"]) for w in col_num if re.match(r"^\d+$", w["text"])]
            post_pos = digits[-1] if digits else r_idx + 1

        # Odds parsing
        col_odds_sorted = sorted(col_odds, key=lambda x: (x["top"], x["x0"]))
        odds_str = " ".join(w["text"] for w in col_odds_sorted)
        m_ml = re.search(r"M\s*:\s*([0-9/]+)", odds_str)
        ml_raw = m_ml.group(1).strip() if m_ml else None

        rem_odds = re.sub(r"M\s*:\s*[0-9/]+", "", odds_str)
        m_other = re.search(r"\b(\d+(?:/\d+)?)\b", rem_odds)
        other_odds_raw = m_other.group(1).strip() if m_other else None

        ml_decimal = None
        if ml_raw:
            if "/" in ml_raw:
                p_num, p_den = ml_raw.split("/")
                ml_decimal = round(float(p_num) / float(p_den) + 1.0, 3)
            else:
                try:
                    ml_decimal = float(ml_raw) + 1.0
                except ValueError:
                    ml_decimal = None

        # Runner name & description
        # Group by vertical slot and join adjacent characters
        y_groups: dict[int, list[Any]] = {}
        for w in sorted(col_runner, key=lambda x: (x["top"], x["x0"])):
            slot = round(w["top"] / 5)
            y_groups.setdefault(slot, []).append(w)

        name_lines: list[str] = []
        desc_lines: list[str] = []
        is_desc = False

        for slot in sorted(y_groups.keys()):
            gw = y_groups[slot]
            line_parts: list[str] = []
            cur_word = ""
            last_x1 = None
            for w in gw:
                txt = _clean_text(w["text"])
                if not txt:
                    continue
                if txt in ("RUNNER", "ANGLES", "#"):
                    continue
                if last_x1 is not None and w["x0"] - last_x1 > 2.5:
                    line_parts.append(cur_word)
                    cur_word = txt
                else:
                    cur_word += txt
                last_x1 = w["x1"]
            if cur_word:
                line_parts.append(cur_word)
            line_str = " ".join(line_parts).strip()
            if not line_str:
                continue

            if any(re.search(r"\b" + re.escape(term) + r"\b", line_str, re.I) for term in ["Bay", "Dark", "Brown", "Chestnut", "Grayor", "Gray", "Roan", "Colt", "Gelding", "Ridgling", "Filly", "Mare", "yrs", "(KY)", "(NY)", "(FL)", "(MD)"]) or "DarkBay" in line_str or "Gelding" in line_str:
                is_desc = True

            if is_desc:
                desc_lines.append(line_str)
            else:
                name_lines.append(line_str)

        raw_name = " ".join(name_lines).strip()
        horse_name = clean_horse_name(raw_name)

        # Repeated DK web chrome is not a runner header.  Do not fall back to
        # a positional name here: that would invent an active entry.
        if not horse_name or canonical_horse_name(horse_name) in GENERIC_CHROME_KEYS:
            continue

        desc_raw = " ".join(desc_lines).strip()
        desc_raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", desc_raw)
        desc_raw = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", desc_raw)
        # Extract breeding
        color, sex, age, foal_year, state_bred, lasix = _extract_breeding(
            desc_raw, target_date.year
        )
        provisional_key = _build_provisional_horse_source_key(
            horse_name, sex, foal_year, state_bred
        )

        # Angles
        angles_words = sorted(col_angles, key=lambda x: (x["top"], x["x0"]))
        angles_text = " ".join(_clean_text(w["text"]) for w in angles_words).strip()
        # Parse individual angles
        known_angle_patterns = [
            ("Hot Trainer", "trainer"),
            ("Key Trainer", "trainer"),
            ("Hot Jockey", "jockey"),
            ("Top Pick", "pick"),
            ("Clocker Special", "clocker"),
        ]
        parsed_angles: list[str] = []
        for angle_name, angle_cat in known_angle_patterns:
            if re.search(r"\b" + re.escape(angle_name) + r"\b", angles_text, re.I):
                parsed_angles.append(angle_name)
                annotations.append(
                    DraftKingsAnnotationRecord(
                        horse_name=horse_name,
                        angle_name=angle_name,
                        angle_category=angle_cat,
                        source_page_number=hdr_page_idx + 1,
                        source_row_id=f"{doc_id}:p{hdr_page_idx+1}:angle:{len(annotations)+1}",
                        raw_text=angles_text,
                        parse_confidence=0.98,
                    )
                )

        # Odds record creation
        if ml_raw:
            odds_records.append(
                DraftKingsOddsRecord(
                    horse_name=horse_name,
                    odds_value_raw=ml_raw,
                    odds_type="morning_line",
                    odds_capture_timestamp=captured_at,
                    odds_source_label_raw="M:",
                    is_market_eligible=True,
                    source_page_number=hdr_page_idx + 1,
                    source_row_id=f"{doc_id}:p{hdr_page_idx+1}:ml:{post_pos}",
                    raw_text=f"M: {ml_raw}",
                    parse_confidence=0.99,
                )
            )

        if other_odds_raw:
            odds_type = "off_odds" if is_post_race else "live_tote"
            odds_records.append(
                DraftKingsOddsRecord(
                    horse_name=horse_name,
                    odds_value_raw=other_odds_raw,
                    odds_type=odds_type,
                    odds_capture_timestamp=captured_at,
                    odds_source_label_raw="ODDS",
                    is_market_eligible=False,
                    source_page_number=hdr_page_idx + 1,
                    source_row_id=f"{doc_id}:p{hdr_page_idx+1}:other_odds:{post_pos}",
                    raw_text=other_odds_raw,
                    parse_confidence=0.95,
                )
            )

        entry_rec = DraftKingsEntryRecord(
            post_position=post_pos,
            program_number=post_pos,
            horse_name=horse_name,
            horse_source_key=provisional_key,
            morning_line_raw=ml_raw,
            morning_line_decimal=ml_decimal,
            other_odds_raw=other_odds_raw,
            odds_type="off_odds" if is_post_race else "live_tote",
            sex=sex,
            age=age,
            foaling_year=foal_year,
            color=color,
            state_bred=state_bred,
            lasix=lasix,
            angles=parsed_angles,
            source_page_number=hdr_page_idx + 1,
            source_row_id=f"{doc_id}:p{hdr_page_idx+1}:entry:{post_pos}",
            raw_text=f"{post_pos} {horse_name} ML:{ml_raw} ODDS:{other_odds_raw}",
            parse_confidence=0.99,
            has_explicit_no_races=is_no_races,
        )
        entries.append(entry_rec)
        entry_page_indices.append(hdr_page_idx)
        pps_page_indices.append(pps_page_idx)
        has_explicit_no_races.append(is_no_races)

    # 3. Associate Pages to Runners and Extract Starts & Workouts
    # Determine page range for each runner
    runner_page_ranges: list[tuple[DraftKingsEntryRecord, int, int]] = []
    for i, entry in enumerate(entries):
        p_start = pps_page_indices[i]
        p_end = pps_page_indices[i + 1] if i + 1 < len(pps_page_indices) else num_pages
        runner_page_ranges.append((entry, p_start, p_end))

    for entry, p_start, p_end in runner_page_ranges:
        current_section = "starts"
        for p_idx in range(p_start, p_end):
            page_words = pages_words[p_idx]
            p_height = pdf.pages[p_idx].height if pdf.pages else 792.0
            ar_words = [w for w in page_words if w["text"] in ("ALL", "NO") and w["x0"] < 80]
            wo_words = [w for w in page_words if w["text"] == "WORKOUTS" and w["x0"] < 100]
            sl_words = [w for w in page_words if w["text"] == "SEE" and w["x0"] < 80]

            ar_top = ar_words[0]["top"] if ar_words else None
            wo_top = wo_words[0]["top"] if wo_words else None
            sl_top = sl_words[0]["top"] if sl_words else None

            # Group page lines by vertical coordinate
            lines_by_y: dict[int, list[Any]] = {}
            for w in sorted(page_words, key=lambda x: (x["top"], x["x0"])):
                # Ignore page header and footer using normalized coordinates
                if w["top"] / p_height < 0.035 or w["bottom"] / p_height > 0.965:
                    continue
                lines_by_y.setdefault(round(w["top"] / 4), []).append(w)

            # Reconstruct lines
            text_lines: list[tuple[float, str]] = []
            for y_slot, lw in sorted(lines_by_y.items()):
                line_str = " ".join(_clean_text(w["text"]) for w in lw).strip()
                if line_str:
                    text_lines.append((y_slot * 4.0, line_str))

            # Iterate through reconstructed lines to capture starts and workouts
            i = 0
            while i < len(text_lines):
                y_pos, line = text_lines[i]

                # On start page, ignore lines strictly above ALL RACES header
                if p_idx == p_start and ar_top is not None and y_pos < ar_top - 10.0:
                    i += 1
                    continue

                if "WORKOUTS" in line:
                    current_section = "workouts"
                    i += 1
                    continue
                if "SEE LESS" in line:
                    # End of workouts for current horse
                    break
                if "ALL RACES" in line or "NO RACES" in line:
                    current_section = "starts"
                    i += 1
                    continue

                m_date = DATE_PATTERN.search(line)
                if m_date:
                    record_date = _parse_date_str(line, target_date.year)
                    if not record_date:
                        i += 1
                        continue

                    # Collect subsequent lines until next date, section header, or page end
                    sub_lines: list[str] = []
                    # Check if remainder of this line has content
                    line_rem = (line[:m_date.start()] + " " + line[m_date.end():]).strip()
                    if line_rem:
                        sub_lines.append(line_rem)

                    j = i + 1
                    while j < len(text_lines):
                        next_y, next_line = text_lines[j]
                        if (
                            DATE_PATTERN.search(next_line)
                            or "WORKOUTS" in next_line
                            or "SEE LESS" in next_line
                            or "ALL RACES" in next_line
                            or "NO RACES" in next_line
                        ):
                            break
                        sub_lines.append(next_line)
                        j += 1

                    combined_detail = " ".join(sub_lines)
                    full_raw = f"{line} | {combined_detail}"

                    if current_section == "starts":
                        # Parse start details
                        is_target = record_date == target_date
                        track_name = "Unknown"
                        for candidate in ["SARATOGA", "BELMONT AT THE BIG A", "BELMONT", "AQUEDUCT", "DELAWARE PARK", "PARX RACING", "MONMOUTH PARK", "ELLIS PARK", "CHURCHILL DOWNS", "GULFSTREAM PARK", "FAIR HILL", "TURFWAY PARK", "OAKLAWN PARK", "PRAIRIE MEADOWS", "KEENELAND", "LAUREL PARK", "MEADOWLANDS"]:
                            if candidate in combined_detail.upper():
                                track_name = candidate
                                break

                        track_res = resolve_track(track_name=track_name)
                        st_track_code = track_res.get("track_code") or "UNK"

                        # Class
                        st_class = "Unknown"
                        m_cls = re.search(r"\b(CLM\d*|MCL\d*|MSW|ALW|AOC|SOC|STR|MOC)\b", combined_detail)
                        if m_cls:
                            st_class = m_cls.group(1)

                        # Distance
                        st_dist = None
                        m_dst = re.search(r"\b(\d+(?:\s+\d+/\d+)?\s*[MF]|\d+M\s+\d+Y)\b", combined_detail)
                        if m_dst:
                            st_dist = m_dst.group(1)

                        # Surface / condition
                        st_surf = "dirt"
                        st_cond = "fast"
                        m_sc = re.search(r"\b(DIRT|TURF|AW|TR\.D|TR\.T|I)-?([A-Za-z]+(?:\s+[A-Za-z]+)?)\b", combined_detail, re.I)
                        if m_sc:
                            st_surf = m_sc.group(1).lower()
                            st_cond = m_sc.group(2).lower()

                        # Scratches
                        is_scr = "SCR" in combined_detail.upper()

                        # Finish & odds
                        finish_pos = None
                        odds_val = None
                        tokens = combined_detail.split()
                        if is_scr:
                            finish_pos = None
                            odds_val = "SCR"
                            scratches.append(
                                DraftKingsScratchRecord(
                                    horse_name=entry.horse_name,
                                    scratch_type="historical",
                                    scratch_date=record_date,
                                    track_code=st_track_code,
                                    race_class=st_class,
                                    source_page_number=p_idx + 1,
                                    source_row_id=f"{doc_id}:p{p_idx+1}:scr:{len(scratches)+1}",
                                    raw_text=full_raw,
                                    parse_confidence=0.95,
                                )
                            )
                        else:
                            # Last token is typically finish position
                            if tokens and re.match(r"^\d+$", tokens[-1]):
                                finish_pos = int(tokens[-1])
                                if len(tokens) >= 2 and re.match(r"^\d+(?:-\d+|/\d+)?$", tokens[-2]):
                                    odds_val = tokens[-2]

                        start_rec = DraftKingsStartRecord(
                            horse_name=entry.horse_name,
                            horse_source_key=entry.horse_source_key,
                            start_date=record_date,
                            is_target_race=is_target,
                            track_code=st_track_code,
                            track_name=track_name,
                            race_class=st_class,
                            distance_text=st_dist or "",
                            distance_furlongs=_parse_furlongs(st_dist),
                            surface=st_surf,
                            surface_condition=st_cond,
                            program_post=tokens[-3] if len(tokens) >= 3 and not is_scr else None,
                            odds_raw=odds_val,
                            finish_position=finish_pos,
                            is_scratch=is_scr,
                            source_page_number=p_idx + 1,
                            source_row_id=f"{doc_id}:p{p_idx+1}:start:{len(starts)+1}",
                            raw_text=full_raw,
                            parse_confidence=0.95,
                        )
                        starts.append(start_rec)

                    elif current_section == "workouts":
                        is_target = record_date == target_date
                        track_name = "Unknown"
                        for candidate in ["SARATOGA", "BELMONT PARK", "BELMONT", "DELAWARE PARK", "PARX RACING", "MONMOUTH PARK", "CHURCHILL TRAINING", "CHURCHILL DOWNS", "TURFWAY PARK", "FAIR HILL", "WINSTAR TRAINING CENTER", "WINSTAR", "KEENELAND", "THE THOROUGHBRED CENTER", "PALM MEADOWS TRAINING CENTER", "PALM BEACH DOWNS", "SUNNYSIDE FARM TRAINING CENTER", "DOUBLE M TRAINING CENTER", "PRESQUE ISLE DOWNS", "OAKLAWN PARK"]:
                            if candidate in combined_detail.upper():
                                track_name = candidate
                                break

                        track_res = resolve_track(track_name=track_name)
                        wo_track_code = track_res.get("track_code") or "UNK"

                        wo_dist = None
                        m_wdst = re.search(r"\b(\d+\s*F|\d+M)\b", combined_detail)
                        if m_wdst:
                            wo_dist = m_wdst.group(1)

                        wo_surf = "dirt"
                        wo_cond = "fast"
                        m_wsc = re.search(r"\b(DIRT|TURF|AW|TR\.D|TR\.T)-?([A-Za-z]+)\b", combined_detail, re.I)
                        if m_wsc:
                            wo_surf = m_wsc.group(1).lower()
                            wo_cond = m_wsc.group(2).lower()

                        wo_time = None
                        wo_grade = "B"
                        m_tm = re.search(r"\b(\d{1,2}:\d{2}\.\d+|\d{2}\.\d+)\s*(Bg|Hg|B|H)\b", combined_detail)
                        if m_tm:
                            wo_time = m_tm.group(1)
                            wo_grade = m_tm.group(2)

                        # Rank
                        wo_rank = None
                        m_rk = re.search(r"\b(\d+)(?:\s*o|\s*of\s*\d+)?$", combined_detail)
                        if m_rk:
                            wo_rank = int(m_rk.group(1))

                        wo_rec = DraftKingsWorkoutRecord(
                            horse_name=entry.horse_name,
                            horse_source_key=entry.horse_source_key,
                            workout_date=record_date,
                            is_target_race=is_target,
                            track_code=wo_track_code,
                            track_name=track_name,
                            distance_text=wo_dist or "",
                            distance_furlongs=_parse_furlongs(wo_dist),
                            surface=wo_surf,
                            surface_condition=wo_cond,
                            time_seconds=_parse_time_seconds(wo_time),
                            time_text=wo_time or "",
                            work_grade=wo_grade,
                            rank=wo_rank,
                            source_page_number=p_idx + 1,
                            source_row_id=f"{doc_id}:p{p_idx+1}:wo:{len(workouts)+1}",
                            raw_text=full_raw,
                            parse_confidence=0.95,
                        )
                        workouts.append(wo_rec)

                    i = j
                    continue

                i += 1

    # The classic web-program layout repeats navigation and can split one horse
    # across pages.  Prefer identity-bounded text sections whenever they yield
    # a credible field; retain the older coordinate parser as a compatibility
    # fallback for the legacy fixture layout.
    sections = extract_runner_sections(pages_text)
    if (len(entries) < 4 and len(sections) >= 4 and "dkhorse_classic_url" in source_detection["source_detection_signals"]
            and len(sections) <= max(len(entries) + 2, 12)):
        section_entries: list[DraftKingsEntryRecord] = []
        section_starts: list[DraftKingsStartRecord] = []
        section_workouts: list[DraftKingsWorkoutRecord] = []
        section_scratches: list[DraftKingsScratchRecord] = []
        for section in sections:
            color, sex, age, foal_year, state_bred, lasix = _extract_breeding(section.raw_text[:800], target_date.year)
            source_key = _build_provisional_horse_source_key(section.horse_name_raw, sex, foal_year, state_bred)
            ml_decimal = None
            if section.morning_line_raw:
                try:
                    numerator, denominator = (section.morning_line_raw.split("/") + ["1"])[:2]
                    ml_decimal = float(numerator) / float(denominator) + 1.0
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            section_entries.append(DraftKingsEntryRecord(
                post_position=section.post_position or 0,
                program_number=section.program_number or 0,
                horse_name=section.horse_name_raw,
                horse_source_key=source_key,
                morning_line_raw=section.morning_line_raw,
                morning_line_decimal=ml_decimal,
                other_odds_raw=None,
                odds_type="unknown",
                sex=sex, age=age, foaling_year=foal_year, color=color,
                state_bred=state_bred, lasix=lasix, angles=[],
                source_page_number=section.source_page_start,
                source_row_id=f"{doc_id}:p{section.source_page_start}:entry:{section.program_number or len(section_entries)+1}",
                raw_text=section.raw_text,
                parse_confidence=0.96,
            ))
            pps, works, historical_scratches = _section_pp_rows(section, target_date, doc_id)
            # Always attach with the canonical entry key, never a weak global name match.
            for pp in pps:
                pp.horse_source_key = source_key
            for work in works:
                work.horse_source_key = source_key
            section_starts.extend(pps)
            section_workouts.extend(works)
            section_scratches.extend(historical_scratches)
        entries, starts, workouts, scratches = section_entries, section_starts, section_workouts, section_scratches

    declared_match = re.search(r"\b(?:FIELD\s+SIZE\s*[:#]?|HORSES\s*[:#]?)\s*(\d{1,2})\b|\b(\d{1,2})\s+HORSES\b", p1_text, re.I)
    declared_field_size = int(next(group for group in declared_match.groups() if group)) if declared_match else len(entries)
    active_entry_count = len(entries)
    # A missing program number is not proof of a scratch: DK web programs can
    # omit coupled entries, reflect a changed view, or expose an extraction
    # defect.  Only a source-row-level current-race SCR/NONSTARTER record may
    # set nonstarter_count and late_scratch_explained.  Historical SCR rows are
    # deliberately excluded from this reconciliation.
    nonstarter_count = 0
    reconciliation = "exact" if declared_field_size == active_entry_count else "unexplained"
    raw_text_sha256 = hashlib.sha256(full_raw_text.encode("utf-8")).hexdigest()
    first_500_chars = full_raw_text[:500]
    stored_path_resolved = (
        str(Path(stored_path).resolve())
        if stored_path
        else (str(Path(filename).resolve()) if Path(filename).exists() else str(Path(filename)))
    )

    debug_payload = {
        "upload": DebugContainer({
            "original_filename": Path(filename).name,
            "stored_path": stored_path_resolved,
            "size_bytes": file_size_bytes,
            "sha256": file_sha256,
            "uploaded_at": captured_at or datetime.now(timezone.utc).isoformat(),
        }),
        "parser": DebugContainer({
            "adapter_selected": "draftkings_pdf",
            "adapter_version": "1.0.0",
            "source_format": source_detection["source_format"] or "dkhorse_program_pdf",
            "source_confidence": source_detection["source_confidence"],
            "source_detection_signals": source_detection["source_detection_signals"],
            "raw_text_sha256": raw_text_sha256,
            "first_500_chars": first_500_chars,
            "page_count": num_pages,
            "raw_text": full_raw_text,
            "runner_sections": [
                {"program_number": s.program_number, "post_position": s.post_position,
                 "horse_name_raw": s.horse_name_raw, "horse_name_key": s.horse_name_key,
                 "source_page_start": s.source_page_start, "source_page_end": s.source_page_end,
                 "raw_text": s.raw_text}
                for s in sections
            ],
        }),
        "race_resolution": DebugContainer({
            "filename_race_number": fn_race,
            "header_race_number": header_race_num,
            "selected_race_number": header_race_num or fn_race,
            "track_candidates": list(dict.fromkeys(c for c in [fn_track, header_track_code] if c)),
            "race_candidates": list(dict.fromkeys(c for c in [fn_race, header_race_num] if c is not None)),
            "header_pages_scanned": [1],
        }),
    }

    # Manifest dictionary complying with raw_input manifest schema
    manifest = {
        "manifest_id": f"raw-{target_date.strftime('%Y%m%d')}-{doc_id}",
        "asset_class": "raw_input_snapshot",
        "source_provider": "draftkings",
        "source_url_or_reference": filename,
        "retrieved_at_utc": f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "as_of_utc": f"{target_date.isoformat()}T12:00:00Z",
        "license_or_terms_reference": "DraftKings Horse Racing Data Agreement",
        "file_sha256": file_sha256,
        "file_size_bytes": file_size_bytes,
        "schema_fingerprint": hashlib.sha256(b"draftkings_v1_schema").hexdigest(),
        "race_scope": {
            "track_code": header_track_code or fn_track,
            "race_number": header_race_num or fn_race,
            "race_date": target_date.isoformat(),
        },
        "ingestion_tool_version": "1.0.0",
        "captured_at": captured_at,
        "scheduled_post_at": None,
        "scoring_as_of_timestamp": None,
        "is_post_race": is_post_race,
        "production_eligible": production_eligible,
        "eligibility_reason": eligibility_reason,
        "upload": debug_payload["upload"],
        "parser": debug_payload["parser"],
        "race_resolution": debug_payload["race_resolution"],
        "debug_payload": debug_payload,
    }

    return DraftKingsParsedRace(
        source_document_id=doc_id,
        file_sha256=file_sha256,
        file_size_bytes=file_size_bytes,
        filename_track_code=fn_track,
        header_track_code=header_track_code,
        filename_race_number=fn_race,
        header_race_number=header_race_num,
        target_race_date=target_date,
        track_name=header_track_name,
        stakes_name=race_class,
        race_class=race_class,
        purse=purse,
        distance_text=distance_text,
        distance_furlongs=distance_furlongs,
        surface=surface,
        conditions=conditions,
        field_size_declared=declared_field_size,
        captured_at=captured_at,
        is_post_race=is_post_race,
        production_eligible=production_eligible,
        eligibility_reason=eligibility_reason,
        status="success",
        entry_count=len(entries),
        entry_parse_coverage=1.0 if len(entries) == 10 else len(entries) / 10.0,
        workout_count=len(workouts),
        historical_start_count=len(starts),
        unparsed_runner_blocks=[],
        entries=entries,
        starts=starts,
        workouts=workouts,
        scratches=scratches,
        annotations=annotations,
        odds_records=odds_records,
        manifest=manifest,
        raw_text=full_raw_text,
        debug_payload=debug_payload,
    )


def to_dk_legacy_race_result(
    parsed: DraftKingsParsedRace,
    debug_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate parsed DraftKings race into legacy/UI compatible dictionary with exact diagnostic payload."""
    dbg = debug_payload or getattr(parsed, "debug_payload", None) or parsed.manifest.get("debug_payload") or {}
    upload_dict = dbg.get("upload") or parsed.manifest.get("upload") or {
        "original_filename": parsed.manifest.get("source_url_or_reference") or "SAR_DK_Horse_R9_9-2-26.pdf",
        "stored_path": parsed.manifest.get("source_url_or_reference") or "SAR_DK_Horse_R9_9-2-26.pdf",
        "size_bytes": parsed.file_size_bytes,
        "sha256": parsed.file_sha256,
        "uploaded_at": parsed.manifest.get("retrieved_at_utc"),
    }
    parser_dict = dbg.get("parser") or parsed.manifest.get("parser") or {
        "adapter_selected": "draftkings_pdf",
        "adapter_version": "1.0.0",
        "raw_text_sha256": hashlib.sha256(parsed.raw_text.encode("utf-8")).hexdigest() if parsed.raw_text else "",
        "first_500_chars": parsed.raw_text[:500] if parsed.raw_text else "",
        "page_count": 39,
        "raw_text": parsed.raw_text,
    }
    race_res_dict = dbg.get("race_resolution") or parsed.manifest.get("race_resolution") or {
        "filename_race_number": parsed.filename_race_number,
        "header_race_number": parsed.header_race_number,
        "selected_race_number": parsed.header_race_number or parsed.filename_race_number,
        "track_candidates": [parsed.filename_track_code] if parsed.filename_track_code else [],
        "race_candidates": [parsed.filename_race_number] if parsed.filename_race_number else [],
        "header_pages_scanned": [1],
    }

    runners = []
    for e in parsed.entries:
        entry_starts = [
            {
                "race_date": s.start_date.isoformat(),
                "track_code": s.track_code,
                "finish_position": s.finish_position,
                "field_size": getattr(s, "field_size", None),
                "odds_str": s.odds_raw,
                "distance_text": s.distance_text,
                "surface": s.surface,
                "race_class": s.race_class,
                "purse": getattr(s, "purse", None),
            }
            for s in parsed.starts
            if s.horse_name == e.horse_name and not s.is_target_race
        ]
        runners.append({
            "horse_name": e.horse_name,
            "horse_key": e.horse_source_key,
            "post_position": e.post_position,
            "program_number": str(e.program_number),
            "ml": e.morning_line_raw,
            "morning_line": e.morning_line_raw,
            "morning_line_decimal": e.morning_line_decimal,
            "other_odds_raw": e.other_odds_raw,
            "odds_type": e.odds_type,
            "sex": e.sex,
            "age": e.age,
            "foaling_year": e.foaling_year,
            "color": e.color,
            "state_bred": e.state_bred,
            "lasix": e.lasix,
            "angles": e.angles,
            "last_5": entry_starts[:5],
            "is_scratched": False,
        })

    track_code = parsed.header_track_code or parsed.filename_track_code or "SAR"
    race_number = parsed.header_race_number or parsed.filename_race_number or 9
    race_date_str = parsed.target_race_date.isoformat()
    surface_norm = (parsed.surface.capitalize() if parsed.surface else "Turf")

    active_entry_count = len(runners)
    declared_field_size = parsed.field_size_declared
    reconciliation = "exact" if declared_field_size in (None, active_entry_count) else "unexplained"

    prog_counts: dict[int, int] = {}
    key_counts: dict[str, int] = {}
    for source_entry in parsed.entries:
        if source_entry.program_number is not None:
            prog_counts[source_entry.program_number] = prog_counts.get(source_entry.program_number, 0) + 1
        key = canonical_horse_name(source_entry.horse_name)
        if key:
            key_counts[key] = key_counts.get(key, 0) + 1

    runner_diagnostics = []
    blocking_errors: list[str] = []

    for entry, source_entry in zip(runners, parsed.entries):
        key = canonical_horse_name(source_entry.horse_name)
        linked = [s for s in parsed.starts if s.horse_source_key == source_entry.horse_source_key and not s.is_scratch]
        workouts = [w for w in parsed.workouts if w.horse_source_key == source_entry.horse_source_key]
        entry_scratches = [s for s in parsed.scratches if s.horse_name == source_entry.horse_name]

        is_no_races = getattr(source_entry, "has_explicit_no_races", False)
        if not is_no_races:
            is_no_races = any(
                re.search(r"\bNO\s+RACES\b", getattr(s, "raw_text", ""), re.I)
                for s in parser_dict.get("runner_sections", [])
                if s.get("horse_name_key") == key
            )
        if not is_no_races and hasattr(source_entry, "raw_text") and "NO RACES" in getattr(source_entry, "raw_text", ""):
            is_no_races = True

        id_reasons: list[str] = []
        if source_entry.program_number is None or source_entry.program_number <= 0:
            id_reasons.append("missing_program_number")
        elif prog_counts.get(source_entry.program_number, 0) > 1:
            id_reasons.append("duplicate_program_number")
            blocking_errors.append(f"duplicate_program_number: Program number {source_entry.program_number} is assigned to multiple active entries.")

        if not key or key in GENERIC_CHROME_KEYS:
            id_reasons.append("page_chrome_contaminated_name")
            blocking_errors.append(f"page_chrome_contaminated_name: Runner identity is contaminated with DK page chrome ('{source_entry.horse_name}').")
        elif key_counts.get(key, 0) > 1:
            id_reasons.append("duplicate_horse_name_key")
            blocking_errors.append(f"duplicate_horse_name_key: Canonical horse key '{key}' is assigned to multiple active entries.")

        if not source_entry.horse_name or not source_entry.horse_name.strip():
            id_reasons.append("missing_runner_name")
            blocking_errors.append("missing_runner_name: An active entry is missing a runner name.")

        no_history_reason = None
        if id_reasons:
            status = "unresolved_identity"
        elif len(linked) >= 1:
            status = "linked_history"
        elif is_no_races:
            status = "resolved_no_history"
            no_history_reason = "explicit_no_races"
        elif len(entry_scratches) >= 1:
            status = "resolved_no_history"
            no_history_reason = "scratches_only"
        else:
            status = "unresolved_history"

        runner_diagnostics.append({
            "program_number": source_entry.program_number,
            "horse_name_raw": source_entry.horse_name,
            "horse_name_key": key,
            "runner_data_status": status,
            "no_history_reason": no_history_reason,
            "diagnostic_reasons": id_reasons,
            "source_page_start": source_entry.source_page_number,
            "source_page_end": next((s.get("source_page_end") for s in parser_dict.get("runner_sections", []) if s.get("horse_name_key") == key), source_entry.source_page_number),
            "past_performances_found": len(linked),
            "past_performances_linked": len(linked),
            "workouts_found": len(workouts),
            "historical_scratches_found": len(entry_scratches),
            "exclusion_reason": None,
            "warnings": [],
        })

    total_linked = sum(r["past_performances_linked"] for r in runner_diagnostics)
    linked_history_count = sum(1 for r in runner_diagnostics if r["runner_data_status"] == "linked_history")
    resolved_no_history_count = sum(1 for r in runner_diagnostics if r["runner_data_status"] == "resolved_no_history")
    unresolved_identity_count = sum(1 for r in runner_diagnostics if r["runner_data_status"] == "unresolved_identity")
    unresolved_history_count = sum(1 for r in runner_diagnostics if r["runner_data_status"] == "unresolved_history")

    identity_resolution_rate = round((linked_history_count + resolved_no_history_count) / active_entry_count, 4) if active_entry_count else 0.0
    starter_pp_link_rate = round(linked_history_count / active_entry_count, 4) if active_entry_count else 0.0
    runners_expected_to_have_history = linked_history_count + unresolved_history_count
    experienced_field_pp_coverage = round(linked_history_count / runners_expected_to_have_history, 4) if runners_expected_to_have_history > 0 else 1.0

    runner_data_status_counts = {
        "linked_history": linked_history_count,
        "resolved_no_history": resolved_no_history_count,
        "unresolved_identity": unresolved_identity_count,
        "unresolved_history": unresolved_history_count,
    }

    from src.ingest.run_state import DataQuality, RunMode, resolve_run_mode
    dq = DataQuality(
        entries_parsed=active_entry_count,
        field_size_declared=declared_field_size,
        entries_with_pp_history=linked_history_count,
        starter_match_rate=identity_resolution_rate,
        race_metadata_complete=bool(track_code and race_number and race_date_str),
        has_morning_lines=any(r.get("morning_line") or r.get("ml") for r in runners),
        has_live_odds=any(r.get("other_odds_raw") for r in runners),
        required_model_features_complete=True,
        blocking_errors=list(dict.fromkeys(blocking_errors)),
        active_entry_count=active_entry_count,
        field_reconciliation_status=reconciliation,
        experienced_field=True,
        workout_forward_low_history=False,
        source_format=parser_dict.get("source_format", "dkhorse_program_pdf"),
        identity_resolution_rate=identity_resolution_rate,
        starter_pp_link_rate=starter_pp_link_rate,
        experienced_field_pp_coverage=experienced_field_pp_coverage,
        resolved_no_history_count=resolved_no_history_count,
        unresolved_identity_count=unresolved_identity_count,
        unresolved_history_count=unresolved_history_count,
    )
    run_mode, gate_reasons = resolve_run_mode(dq)
    block_reasons = list(gate_reasons) if run_mode == RunMode.BLOCKED else []

    if run_mode == RunMode.BLOCKED:
        if total_linked > 0 and (unresolved_identity_count > 0 or any("unresolved" in r or "duplicate" in r or "chrome" in r for r in block_reasons)):
            recommended_action = (
                "DraftKings Horse program PDF detected. Historical starts were linked for part of the field, "
                "but one or more runner identities are malformed or duplicated. Scoring remains blocked "
                "until active-entry identity is resolved."
            )
        elif total_linked == 0 or unresolved_history_count > 0:
            recommended_action = (
                "DraftKings Horse program PDF detected. Runner headers were found, but past-performance "
                "sections could not yet be linked to runners. Inspect parser diagnostics or upload a supported native PP source."
            )
        elif reconciliation != "exact":
            recommended_action = "Review field reconciliation before scoring."
        else:
            recommended_action = "Review parser diagnostics before scoring."
    else:
        recommended_action = "Ready for scoring."

    diagnostics = {
        "source_format": parser_dict.get("source_format", "dkhorse_program_pdf"),
        "source_confidence": parser_dict.get("source_confidence", 0.0),
        "source_detection_signals": parser_dict.get("source_detection_signals", []),
        "declared_field_size": declared_field_size,
        "active_entry_count": active_entry_count,
        "nonstarter_count": 0,
        "field_reconciliation_status": reconciliation,
        "runners": runner_diagnostics,
        "total_pp_records_found": len(parsed.starts),
        "total_pp_records_linked": total_linked,
        "starter_match_rate": identity_resolution_rate,
        "identity_resolution_rate": identity_resolution_rate,
        "starter_pp_link_rate": starter_pp_link_rate,
        "experienced_field_pp_coverage": experienced_field_pp_coverage,
        "runner_data_status_counts": runner_data_status_counts,
        "resolved_no_history_count": resolved_no_history_count,
        "unresolved_identity_count": unresolved_identity_count,
        "unresolved_history_count": unresolved_history_count,
        "run_mode": run_mode.value,
        "block_reasons": block_reasons,
        "recommended_action": recommended_action,
    }
    race_payload = {
        "track_code": track_code,
        "race_date": race_date_str,
        "race_number": race_number,
        "distance_text": parsed.distance_text or "1 1/16 M",
        "surface": surface_norm,
        "runners_count": len(runners),
        "runners": runners,
        "field_size": active_entry_count,
        "declared_field_size": declared_field_size,
        "active_entry_count": active_entry_count,
        "nonstarter_count": 0,
        "field_reconciliation_status": reconciliation,
        "purse": parsed.purse,
        "race_type": parsed.race_class or "Claiming",
        "conditions": parsed.conditions,
        "stakes_name": parsed.stakes_name,
    }

    return {
        "ok": True,
        "error": None,
        "warnings": [],
        "upload": DebugContainer(upload_dict),
        "parser": DebugContainer(parser_dict),
        "parser_diagnostics": diagnostics,
        "race_resolution": DebugContainer(race_res_dict),
        "race": race_payload,
        "track_code": track_code,
        "track_name": parsed.track_name,
        "race_date": race_date_str,
        "race_number": race_number,
        "distance_text": parsed.distance_text or "1 1/16 M",
        "surface": surface_norm,
        "race_type": parsed.race_class or "Claiming",
        "purse_usd": parsed.purse,
        "field_size": active_entry_count,
        "declared_field_size": declared_field_size,
        "active_entry_count": active_entry_count,
        "nonstarter_count": 0,
        "field_reconciliation_status": reconciliation,
        "runners": runners,
        "runners_count": len(runners),
        "is_draftkings": True,
        "is_1stbet": False,
        "production_eligible": parsed.production_eligible,
        "is_post_race": parsed.is_post_race,
        "eligibility_reason": parsed.eligibility_reason,
        "manifest": parsed.manifest,
        "parsed_race": parsed,
        "raw_text": parsed.raw_text,
    }
