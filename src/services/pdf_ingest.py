"""
PDF ingestion service for DerbyEdge.

parse_race_pdf    — pre-race: sportsbook pages, Equibase race cards, DRF printouts
parse_results_pdf — post-race: Equibase official chart PDFs

Supported formats
-----------------
* 1/ST BET race-detail page (web-print PDF)
    Detection: text[:300] contains "1/ST BET"
    Header layout:
        Line 0:  "M/D/YY, H:MM PM 1/ST BET - The Easy & Smart Way to Bet the Races"
        Line 1:  "TRACK NAME R N"
        Line 2:  "H:MM PM N Horses CLS $P,PPP DIS Surface / Condition"
    Runner blocks (one per horse, in order):
        "HORSE NAME LIVE_ODDS"  or  "HORSE NAME SCR"   ← ALL CAPS
        "N"                                              ← program number alone
        "J: Jockey Name ML ML_ODDS"
        "PP N"                                           ← optional
        "T: Trainer Name"
        [stats / past-performance lines to ignore]

* Generic (Equibase, DRF, other text-based PDFs)

Strategy: extract full text via pdfplumber, apply regex patterns, normalize.
Returns Unknown/None + warning rather than raising on any missing field.

Requires: pip install pdfplumber
"""
from __future__ import annotations

import io
import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

# ── Track code lookup ─────────────────────────────────────────────────────────
# Canonical registry lives in src/derbyedge/tracks.py.
# TRACK_CODES is a flat {normalized_alias: code} dict used by the
# substring-scan extractors below; resolve_track() handles full resolution.
from src.derbyedge.tracks import TRACK_CODES as _TRACK_CODES, resolve_track as _resolve_track

_MONTH_MAP: dict[str, int] = {
    "january":1,  "february":2,  "march":3,    "april":4,
    "may":5,      "june":6,      "july":7,     "august":8,
    "september":9,"october":10,  "november":11,"december":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,
    "aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

_ORDINAL_MAP: dict[str, int] = {
    "first":1,"second":2,"third":3,"fourth":4,"fifth":5,"sixth":6,
    "seventh":7,"eighth":8,"ninth":9,"tenth":10,"eleventh":11,"twelfth":12,
}

_SURFACE_NORM: dict[str, str] = {
    "dirt":"D","turf":"T","synthetic":"S","polytrack":"S","tapeta":"S",
    "all-weather":"AW","all weather":"AW","allweather":"AW",
}

# Abbreviations used in the 1/ST BET race-info header line
_1STBET_CLASS_MAP: dict[str, str] = {
    "CLM": "Claiming",
    "MCL": "Maiden Claiming",
    "MSW": "Maiden Special Weight",
    "MSP": "Maiden Special Weight",
    "ALW": "Allowance",
    "AOC": "Allowance Optional Claiming",
    "OC":  "Optional Claiming",
    "STK": "Stakes",
    "HCP": "Handicap",
    "WMC": "Waiver Maiden Claiming",
}


# ── Text extraction ───────────────────────────────────────────────────────────

def _extract_text(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber not installed — run: pip install pdfplumber"
        )
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text(x_tolerance=2, y_tolerance=2)
            if t:
                parts.append(t)
    return "\n".join(parts)


# ── Debug helper ──────────────────────────────────────────────────────────────

def debug_pdf_text(pdf_bytes: bytes, chars: int = 2000, lines: int = 50) -> None:
    """Print extracted PDF text for layout inspection.

    Usage (from Python REPL or a one-off script):
        from src.services.pdf_ingest import debug_pdf_text
        debug_pdf_text(open("my_race.pdf", "rb").read())
    """
    text = _extract_text(pdf_bytes)
    all_lines = text.splitlines()
    print(f"=== PDF TEXT ({len(text)} chars, {len(all_lines)} lines) ===")
    print(f"\n--- First {chars} chars ---")
    print(text[:chars])
    print(f"\n--- First {lines} lines (with index) ---")
    for i, line in enumerate(all_lines[:lines]):
        print(f"{i:3d}|{line}")


# ── Format detection ──────────────────────────────────────────────────────────

def _is_1stbet(text: str) -> bool:
    """True when text was extracted from a 1/ST BET race-detail page.

    Two detection layers:

    1. Brand / URL signals in the first 1000 chars — covers standard exports
       where pdfplumber picks up the page header "1/ST BET - ..." or the
       footer URL "legacy.1stbet.com".

    2. Structural heuristic — compact pdfplumber exports collapse the brand
       header into the race-info line, so "1/ST BET" never appears in plain
       text.  Instead we count compact runner tokens of the form "1PP1", "2PP2"
       etc. (digit(s) + PP + digit(s)).  Three or more distinct occurrences
       are unique to 1/ST BET compact exports and absent from Equibase/DRF.
    """
    # Layer 1: explicit brand / URL signals
    if re.search(
        r'1/ST\s+BET'        # standard:  "1/ST BET"
        r'|1ST\s*BET'        # compact:   "1ST BET" or "1STBET"
        r'|1stbet\.com'      # URL:       "1stbet.com", "www.1stbet.com"
        r'|legacy\.1stbet',  # subdomain: "legacy.1stbet.com"
        text[:1000], re.I,
    ):
        return True
    # Layer 2: structural — ≥3 compact "NNPPnn" runner tokens
    return len(re.findall(r'(?<!\d)\d{1,2}PP\d{1,2}(?!\d)', text)) >= 3


# ── Generic field extractors ──────────────────────────────────────────────────

def _extract_date(text: str) -> str | None:
    # ISO
    m = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', text)
    if m:
        return m.group(1)
    # "May 4, 2024" or "May 4 2024"
    m = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|'
        r'October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
        r'\.?\s+(\d{1,2}),?\s+(\d{4})\b', text, re.I
    )
    if m:
        mo = _MONTH_MAP.get(m.group(1).lower(), 0)
        return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}" if mo else None
    # MM/DD/YYYY  (4-digit year only — 2-digit year handled by 1/ST BET path)
    m = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', text)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _extract_race_number(text: str) -> int | None:
    m = re.search(r'\brace\s+#?\s*(\d{1,2})\b', text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(
        r'\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|'
        r'eleventh|twelfth)\s+race\b', text, re.I
    )
    if m:
        return _ORDINAL_MAP.get(m.group(1).lower())
    return None


def _extract_track(text: str) -> tuple[str | None, str | None]:
    """Returns (track_code, track_name). Searches the full text."""
    text_lower = text.lower()
    for name, code in _TRACK_CODES.items():
        if name in text_lower:
            return code, name.title()
    m = re.search(r'^([A-Z]{2,4})\s*[-–·|]\s*(?:Race|R\s*\d)', text, re.M)
    if m:
        return m.group(1), None
    m = re.search(
        r'([A-Z][A-Za-z\s]+(?:Race\s*Course|Raceway|Park|Downs|Racetrack))',
        text
    )
    if m:
        return None, m.group(1).strip()
    return None, None


def _extract_distance(text: str) -> str | None:
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:furlongs?|f\b)', text, re.I)
    if m:
        return f"{float(m.group(1)):g} Furlongs"
    m = re.search(r'(\d+)\s+(\d+)/(\d+)\s*miles?', text, re.I)
    if m:
        return f"{m.group(1)} {m.group(2)}/{m.group(3)} Miles"
    m = re.search(r'(\d+(?:\.\d+)?)\s*miles?', text, re.I)
    if m:
        return f"{m.group(1)} Miles"
    m = re.search(r'(\d{3,5})\s*yards?', text, re.I)
    if m:
        return f"{m.group(1)} Yards"
    return None


def _extract_surface(text: str) -> str | None:
    m = re.search(
        r'\b(dirt|turf|synthetic|all[- ]?weather|polytrack|tapeta)\b', text, re.I
    )
    if m:
        return _SURFACE_NORM.get(m.group(1).strip().lower(), m.group(1)[:1].upper())
    return None


def _extract_race_type(text: str) -> str | None:
    m = re.search(
        r'\b(maiden\s+special\s+weight|maiden\s+claiming|maiden|allowance\s+optional\s+claiming|'
        r'optional\s+claiming|starter\s+allowance|allowance|claiming|grade\s+[iii]+|g[1-3]|'
        r'graded\s+stakes?|stakes?|handicap|waiver\s+maiden)\b',
        text, re.I
    )
    return m.group(1).strip().title() if m else None


def _extract_purse(text: str) -> int | None:
    m = re.search(r'purse[:\s]*\$?([\d,]+(?:\.\d+)?)', text, re.I)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _norm_odds(s: str) -> float | None:
    """Convert "5-2", "5/2", "3.50" to decimal odds (win+stake per $1 wagered).
    Returns None on failure.
    """
    s = s.strip()
    for pat in (r'^(\d+)-(\d+)$', r'^(\d+)/(\d+)$'):
        m = re.match(pat, s)
        if m:
            num, den = int(m.group(1)), int(m.group(2))
            return round(num / den + 1.0, 3) if den else None
    try:
        v = float(s)
        return v if v >= 1.0 else None
    except ValueError:
        return None


# ── 1/ST BET-specific extractors ─────────────────────────────────────────────

def _parse_ml_1stbet(raw: str) -> tuple[str, float | None]:
    """Normalize a 1/ST BET morning-line token to (display_string, decimal_odds).

    In horse-racing shorthand, a bare integer N means N-to-1 odds:
        "2"  → "2/1"   decimal 3.0
        "20" → "20/1"  decimal 21.0
    Fractional strings pass through unchanged:
        "9/5" → "9/5"  decimal 2.8
        "7/2" → "7/2"  decimal 4.5
    """
    raw = raw.strip()
    if re.match(r'^\d+$', raw):
        n = int(raw)
        return f"{n}/1", _norm_odds(f"{n}-1")
    return raw, _norm_odds(raw)


def _extract_date_1stbet(text: str, warnings: list[str]) -> str | None:
    """Extract date from 1/ST BET page timestamp on line 0.

    Format: "M/D/YY, H:MM PM 1/ST BET ..."
    Two-digit year is assumed to be 2000+YY (safe through 2099).
    """
    # M/D/YY with 2-digit year (won't collide with fractional odds because
    # odds never have three slash-separated digit groups)
    m = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{2})\b', text)
    if m:
        mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = 2000 + yr
        return f"{year}-{mo:02d}-{day:02d}"
    # Fallback: "TRACK - Month D, YYYY - Race N" header (some 1/ST BET variants)
    m = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|'
        r'October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
        r'\.?\s+(\d{1,2}),?\s+(\d{4})\b',
        text, re.I
    )
    if m:
        mo = _MONTH_MAP.get(m.group(1).lower(), 0)
        return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}" if mo else None
    warnings.append("1/ST BET: could not extract date from page timestamp")
    return None


def _extract_race_number_1stbet(text: str) -> int | None:
    """Extract race number from '1/ST BET header line 1: "TRACK NAME R N"."""
    # Matches "PENN NATIONAL R 6", "AQU R 3", etc. at start of line
    m = re.search(r'^[A-Z][A-Z ]+\s+R\s+(\d{1,2})\s*$', text, re.M)
    if m:
        return int(m.group(1))
    return _extract_race_number(text)


def _extract_track_1stbet(text: str) -> tuple[str | None, str | None]:
    """Extract track from the 1/ST BET header area (first 4 lines only).

    Searching only the header prevents past-performance track names
    (Aqueduct, Parx, etc.) from overriding the actual race track.
    """
    header = "\n".join(text.splitlines()[:4])
    header_lower = header.lower()
    for name, code in _TRACK_CODES.items():
        if name in header_lower:
            return code, name.title()
    # "TRACK NAME R N" — extract raw name from line 1 and fuzzy-match
    m = re.search(r'^([A-Z][A-Z ]+?)\s+R\s+\d{1,2}\s*$', header, re.M)
    if m:
        raw = m.group(1).strip().lower()
        for name, code in _TRACK_CODES.items():
            if name in raw or raw in name:
                return code, name.title()
        return None, m.group(1).strip().title()
    return None, None


def _extract_1stbet_header(text: str) -> dict[str, Any]:
    """Parse the 1/ST BET race-info line (line 2).

    Handles both spaced and compact pdfplumber outputs:
      Spaced:  "5:10 PM 5 Horses CLM $14,000 1M Dirt / Sloppy"
      Compact: "510 PM5 HorsesCLM38,0001MDirt Fast"

    Returns a dict with keys: field_size, race_type, purse_usd,
    distance_text, surface  (all optional, absent if not found).
    """
    result: dict[str, Any] = {}
    # Header line detection: "X Horses" is the most reliable anchor.
    # Drop the \$ requirement — compact exports omit the dollar sign.
    header = None
    for line in text.splitlines()[:8]:
        if re.search(r'\d+\s*Horses', line, re.I):
            header = line
            break
    if not header:
        return result

    _log.debug("1stbet header line: %r", header)

    # Field size
    m = re.search(r'(\d+)\s*Horses', header, re.I)
    if m:
        result["field_size"] = int(m.group(1))

    # Race class abbreviation — try with \b first, fall back to substring
    for abbr, full in _1STBET_CLASS_MAP.items():
        if re.search(r'\b' + abbr + r'\b', header) or abbr in header:
            result["race_type"] = full
            break

    # Purse — try "$N,NNN" first; fall back to bare comma-number like "38,000"
    # Track position to anchor the distance search after the purse.
    purse_end = 0
    m = re.search(r'\$([\d,]+)', header)
    if m:
        try:
            result["purse_usd"] = int(m.group(1).replace(",", ""))
            purse_end = m.end()
        except ValueError:
            pass
    if not result.get("purse_usd"):
        # Compact: "38,0001M" — purse is the comma-number before the distance token
        m = re.search(r'(\d{1,3},\d{3})(?=\d*\s*[MF])', header)
        if not m:
            m = re.search(r'(\d{1,3},\d{3})', header)
        if m:
            try:
                result["purse_usd"] = int(m.group(1).replace(",", ""))
                purse_end = m.end()
            except ValueError:
                pass

    # Distance — search in text AFTER purse position so "38,0001M" correctly
    # yields distance "1M" not a false match inside the purse digits.
    dist_src = header[purse_end:] if purse_end else header

    m = re.search(
        r'(?<!\d)1\s*(1/16|1/8|3/16|1/4|5/16|3/8|1/2|5/8|3/4)\s*M', dist_src, re.I
    )
    if m:
        result["distance_text"] = f"1 {m.group(1)} Miles"
    else:
        # Simple: "1M", "8F", "8.5F" — no \b after unit; unit may touch next word
        m = re.search(r'(?<!\d)(\d+(?:\.\d+)?)\s*([FM])(?!\d)', dist_src)
        if m:
            val, unit = float(m.group(1)), m.group(2).upper()
            if unit == "M" and 0.25 <= val <= 3.0:
                result["distance_text"] = "1 Mile" if val == 1.0 else f"{val} Miles"
            elif unit == "F" and 2.0 <= val <= 20.0:
                result["distance_text"] = f"{val:g} Furlongs"

    # Surface — no \b required; unit may be immediately adjacent ("1MDirt")
    m = re.search(r'(Dirt|Turf|Synthetic|Tapeta)', dist_src, re.I)
    if m:
        result["surface"] = _SURFACE_NORM.get(m.group(1).lower(), m.group(1)[:1].upper())

    return result


# ── 1/ST BET runner parser ────────────────────────────────────────────────────

def _parse_race_runners_1stbet(lines: list[str], warnings: list[str]) -> list[dict]:
    """Parse 1/ST BET race-detail runner blocks.

    Each horse occupies a multi-line block:
        [ALL-CAPS HORSE NAME] [live_odds | SCR]   ← confirmed by next line being a bare integer
        [N]                                        ← program / post number
        J: [Jockey Name] ML [ml_odds]
        PP[N]                                      ← optional label, skipped
        T: [Trainer Name]
        [W x% P y% S z% ...]                       ← stats, skipped
        [RECENT 5 ...]                              ← skipped
        [past-performance lines ...]               ← skipped until next horse

    Detection heuristic: a line is a *candidate* horse-name line only when
    (a) it is entirely upper-case letters / spaces / apostrophes / dashes,
    (b) it ends with a fractional/integer odds token or "SCR", AND
    (c) the immediately-following non-empty line is a bare 1-2 digit integer.

    Condition (c) eliminates false positives such as "PENN NATIONAL R 6"
    (where the next line is the race-info string, not a bare integer).
    """
    runners: list[dict] = []
    seen: set[str] = set()
    n = len(lines)

    re_pp_label = re.compile(r'^PP\d{1,2}$', re.I)
    re_digit    = re.compile(r'^\d{1,2}$')
    re_jockey   = re.compile(r'^J:\s+(.+?)(?:\s+ML\s+(\S+))?\s*$', re.I)
    re_trainer  = re.compile(r'^T:\s+(.+?)\s*$', re.I)

    def _candidate_horse(line: str) -> tuple[str, str] | None:
        """Split 'HORSE NAME ODDS' into (name_part, odds_str), or None."""
        # rsplit on last whitespace to isolate the trailing token
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            return None
        name_part, trailing = parts
        trailing = trailing.strip()
        # Trailing must be fractional odds, integer odds, or "SCR"
        if trailing.upper() == "SCR":
            pass
        elif not re.match(r'^\d+[-/]\d+$', trailing) and not re.match(r'^\d+(?:\.\d+)?$', trailing):
            return None
        # Name part must be ALL CAPS (letters, spaces, apostrophes, hyphens, dots)
        if not re.match(r'^[A-Z][A-Z0-9\'\s\-\.]+$', name_part):
            return None
        return name_part.strip(), trailing

    i = 0
    while i < n:
        line = lines[i].strip()
        i += 1

        if not line:
            continue

        cand = _candidate_horse(line)
        if not cand:
            continue

        # Peek at the next non-empty line — must be a bare program number
        j = i
        while j < n and not lines[j].strip():
            j += 1
        if j >= n or not re_digit.match(lines[j].strip()):
            continue  # not a horse block (e.g. "PENN NATIONAL R 6")

        # --- confirmed horse block ---
        raw_name, raw_odds = cand
        is_scr    = raw_odds.upper() == "SCR"
        live_odds = raw_odds if not is_scr else None

        name = raw_name.title()
        if name in seen:
            i = j + 1
            continue

        pp = int(lines[j].strip())
        i  = j + 1

        jockey  = None
        ml_str  = None
        trainer = None

        # Scan up to 12 lines ahead for J: and T:
        limit = min(i + 12, n)
        while i < limit:
            l = lines[i].strip()
            i += 1

            if not l:
                continue

            if re_pp_label.match(l):
                continue

            mj = re_jockey.match(l)
            if mj:
                jockey = mj.group(1).strip()
                if mj.group(2):
                    ml_str = mj.group(2).strip()
                continue

            mt = re_trainer.match(l)
            if mt:
                trainer = mt.group(1).strip()
                break  # trainer is the last field we care about

            # If we've stumbled onto the next horse block, back up and stop
            nc = _candidate_horse(l)
            if nc:
                k = i
                while k < n and not lines[k].strip():
                    k += 1
                if k < n and re_digit.match(lines[k].strip()):
                    i -= 1  # push this line back so the outer loop sees it
                    break

            # Skip stats / RECENT / past-perf lines silently

        # Use ML from the J: line; fall back to the displayed live odds
        morning_line_raw = ml_str or live_odds
        if morning_line_raw:
            morning_line_str, morning_line_dec = _parse_ml_1stbet(morning_line_raw)
        else:
            morning_line_str = morning_line_dec = None

        seen.add(name)
        runners.append(
            _runner_dict(pp, name, jockey, trainer, morning_line_str, morning_line_dec, is_scr)
        )

    if not runners:
        warnings.append(
            "1/ST BET runner parse found no horse blocks — "
            "confirm this is a 1/ST BET race-detail page (not a card summary)."
        )
    return runners


def _parse_race_runners_1stbet_fallback(text: str, warnings: list[str]) -> list[dict]:
    """Fallback 1/ST BET parser for continuous-stream PDF exports.

    Handles spaced, semi-compact, and fully-compact pdfplumber outputs:
      Layout A: "1 PP1 YOTOWIN J: Evin A. Roman T: Trainer ML 9/2"
      Layout B: "1 PP1 YOTOWIN J: Evin A. Roman T: Trainer - ML 8"
      Layout C: "1PP1YOTOWINJ Evin A. Roman T Rogelio Labra-ML 8"

    Strategy:
      1. Strip noise lines.
      2. Join into one normalised string.
      3. PRIMARY anchors: finditer on "(?<!\\d)\\d{1,2}\\s*PP\\d{1,2}(?!\\d)" —
         handles both spaced ("1 PP1") and compact ("1PP1") without needing \\b.
      4. SECONDARY anchors: bare "\\bPP\\d{1,2}\\b" if primary finds nothing.
      5. Extract pp/horse/jockey/trainer/ml per segment.
         - Horse: STRICT lookahead (\\s+ before J:/T:) for Layout A/B;
           LENIENT lookahead ((?-i:...) proper-case check) for Layout C.
         - J/T patterns accept both colon and no-colon forms.
      6. Deduplicate by post_position — exactly one runner per PP number.
      7. Field-level warnings for missing ML; runner kept regardless.
    """

    def _is_noise(line: str) -> bool:
        s = line.strip()
        if not s:
            return True
        if re.search(r'1/ST\s+BET\s*[-–]', s, re.I):
            return True
        if re.search(r'https?://\S+', s, re.I):
            return True
        if re.search(r'\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s*[AaPp][Mm]', s):
            return True
        if re.match(r'^\d+/\d+$', s):
            return True
        return False

    clean = ' '.join(ln for ln in text.splitlines() if not _is_noise(ln))
    clean = re.sub(r'\s+', ' ', clean).strip()

    # ── Anchor finding ───────────────────────────────────────────────────────
    # Slice at "PP\d" positions so the preceding ML digit stays in the current
    # segment (not stolen by the next anchor).  "1PP1YOTOWIN...ML 82PP2" →
    # segment PP1 = "PP1YOTOWIN...ML 8", segment PP2 = "PP2INNISFREE...".
    # (?!\d) prevents matching "PP10" as "PP1".
    anchors = list(re.finditer(r'PP(\d{1,2})(?!\d)', clean))
    _log.debug("1stbet fallback: PP anchors found: %d", len(anchors))

    # Slice text between consecutive PP anchors
    pp_segments: list[str] = []
    for i, anc in enumerate(anchors):
        start = anc.start()
        end   = anchors[i + 1].start() if i + 1 < len(anchors) else len(clean)
        pp_segments.append(clean[start:end].strip())

    raw_runners: list[dict] = []
    seen_names:  set[str]   = set()

    for seg_idx, seg in enumerate(pp_segments):
        # Locate PP{N} within this segment (may be "PP1" inside "1PP1...")
        m_pp = re.search(r'PP\d+', seg)
        if not m_pp:
            continue
        seg_pp = seg[m_pp.start():]   # "PP1YOTOWIN..." or "PP1 YOTOWIN..."

        _log.debug("1stbet fallback seg[%d]: %r", seg_idx, seg_pp[:160])

        # ── Horse extraction: STRICT then LENIENT ────────────────────────────
        # STRICT (Layout A/B): requires \\s+ (space) before J:/T:/ML label.
        # This prevents the horse name from stopping at "T" inside "WHAT ABOUT NOW".
        m = re.search(
            r'\bPP(?P<pp>\d+)\s*(?P<horse>[A-Z0-9\'\s]+?)'
            r'(?=\s+(?:J:|T:|[-–]?\s*ML\b))',
            seg_pp, re.I,
        )
        if not m:
            # LENIENT (Layout C): J/T without colon, but require a proper-cased
            # name to follow — (?-i:...) disables re.I for the case check,
            # which distinguishes "T Rogelio" (trainer) from "T" in "WHAT ABOUT".
            m = re.search(
                r'\bPP(?P<pp>\d+)\s*(?P<horse>[A-Z0-9\'\s]+?)'
                r'(?=(?-i:\s*J[:\s]+[A-Z][a-z]|\s*T[:\s]+[A-Z][a-z])|\s*[-–]?\s*ML\b)',
                seg_pp, re.I,
            )
        if not m:
            _log.debug("1stbet fallback: no horse match in seg[%d]: %r", seg_idx, seg_pp[:80])
            continue

        pp        = int(m.group('pp'))
        horse_raw = m.group('horse').strip()
        rest      = seg_pp[m.end():].strip()

        is_scr = bool(re.search(r'\b(?:SCR|Scratch(?:ed)?)\b', seg, re.I))
        if is_scr:
            horse_raw = re.sub(r'\s*\b(?:SCR|Scratch(?:ed)?)\b\s*', ' ',
                               horse_raw, flags=re.I).strip()

        horse_name = horse_raw.title() if horse_raw == horse_raw.upper() else horse_raw
        horse_name = re.sub(r'\s+', ' ', horse_name).strip()
        if not horse_name or horse_name in seen_names:
            continue
        seen_names.add(horse_name)

        # ── Jockey: J[:\\s] … stop before trainer or ML ──────────────────────
        # (?-i:T[:\\s]+[A-Z][a-z]) ensures we stop at a proper-cased trainer
        # name rather than "T" embedded inside a word in the jockey's name.
        jockey = None
        m_j = re.search(
            r'J[:\s]\s*(.*?)(?=\s+(?-i:T[:\s]+[A-Z][a-z])|\s*[-–]?\s*ML\b|\Z)',
            rest, re.I,
        )
        if m_j:
            jockey = re.sub(r'\s+', ' ', m_j.group(1)).strip() or None

        # ── Trainer: T[:\\s] … stop at ML or end ─────────────────────────────
        trainer = None
        m_t = re.search(r'T[:\s]\s*(.*?)(?=\s*[-–]?\s*ML\b|\Z)', rest, re.I)
        if m_t:
            trainer = re.sub(r'\s+', ' ', m_t.group(1)).strip() or None
            if trainer:
                trainer = re.sub(r'\s*[-–—]+\s*$', '', trainer).strip() or None

        # ── Morning line: "-ML 8", "- ML 8", "ML 8", "-ML9/2" ───────────────
        # In compact format the segment ends "ML {value}{next_prog_num}PP..." so
        # next_prog_num bleeds into the ML capture.  Strip it when safe to do so.
        next_pp_num = (
            int(anchors[seg_idx + 1].group(1)) if seg_idx + 1 < len(anchors) else None
        )
        ml_str = ml_dec = None
        pp_recap = ""
        m_ml = re.search(r'[-–]?\s*ML\s*(\S+)', rest, re.I)
        if m_ml:
            raw_ml = m_ml.group(1).strip()
            if next_pp_num is not None and raw_ml.endswith(str(next_pp_num)):
                candidate = raw_ml[:-1]
                if candidate and re.match(r'^\d+(?:/\d+)?$', candidate):
                    raw_ml = candidate
            ml_str, ml_dec = _parse_ml_1stbet(raw_ml)
            pp_recap = rest[m_ml.end():].strip()
            # Remove isolated program-number digit left by compact concatenation
            if re.match(r'^\d{1,2}$', pp_recap):
                pp_recap = ""
        else:
            warnings.append(
                f"1/ST BET fallback: PP{pp} ({horse_name}) — no ML odds; runner kept"
            )

        runner = _runner_dict(pp, horse_name, jockey, trainer, ml_str, ml_dec, is_scr)
        runner['ml'] = ml_str
        if pp_recap:
            runner['pp_recap'] = pp_recap
        raw_runners.append(runner)

    # ── Deduplication: exactly one runner per PP number (1–30) ───────────────
    seen_pp: set[int] = set()
    runners: list[dict] = []
    for r in raw_runners:
        pp = r['post_position']
        if not (1 <= pp <= 30):
            _log.debug("1stbet fallback: dropping out-of-range pp=%d (%s)", pp, r['horse_name'])
            continue
        if pp in seen_pp:
            _log.debug("1stbet fallback: dropping duplicate pp=%d (%s)", pp, r['horse_name'])
            continue
        seen_pp.add(pp)
        runners.append(r)

    _log.debug("1stbet fallback: %d raw → %d deduped runners", len(raw_runners), len(runners))

    if runners:
        runners.sort(key=lambda r: r['post_position'])
        warnings.append("1/ST BET fallback PP-anchor parser used.")

    return runners


# ── 1/ST BET multiline block parser ──────────────────────────────────────────

def _parse_race_runners_1stbet_multiline(lines: list[str], warnings: list[str]) -> list[dict]:
    """Parse 1/ST BET line-preserving multi-line runner blocks.

    Verified block structure (pdfplumber output for Horseshoe Indianapolis):
        N           ← bare integer 1-30 (entry / program number)
        PP{N}       ← post-position label
        HORSE NAME  ← all-caps; NO trailing odds token required
        J: Jockey
        T: Trainer
        -           ← lone dash separator (skip)
        ML {odds}   ← morning-line odds on its own line

    Runner-block start: bare-integer line whose next non-empty sibling is PP{N}.
    Also handles a bare PP{N} label as the block start (entry number absent).
    Scans up to 20 lines per block for J:, T:, ML before advancing.
    """
    runners:  list[dict] = []
    seen_pp:  set[int]   = set()
    n = len(lines)

    _re_digit   = re.compile(r'^\d{1,2}$')
    _re_pp      = re.compile(r'^PP(\d{1,2})$', re.I)
    _re_horse   = re.compile(r'^[A-Z][A-Z0-9\'\s\-\.]+$')
    _re_jockey  = re.compile(r'^J:\s*(.+)', re.I)
    _re_trainer = re.compile(r'^T:\s*(.+)', re.I)
    _re_ml      = re.compile(r'\bML\s+(\S+)', re.I)
    _re_noise   = re.compile(r'1/ST\s+BET\s*[-–]|https?://', re.I)
    _re_pgcnt   = re.compile(r'^\d+/\d+$')

    def _next_nonempty(start: int) -> tuple[int, str]:
        j = start
        while j < n and not lines[j].strip():
            j += 1
        return (j, lines[j].strip()) if j < n else (n, "")

    i = 0
    while i < n:
        raw = lines[i].strip()
        i += 1

        if not raw or _re_noise.search(raw) or _re_pgcnt.match(raw):
            continue

        # ── Detect runner-block start ─────────────────────────────────────
        pp_num: int | None = None

        m_direct = _re_pp.match(raw)
        if m_direct:
            pp_num = int(m_direct.group(1))
        elif _re_digit.match(raw):
            j, nxt = _next_nonempty(i)
            m_pp2 = _re_pp.match(nxt)
            if m_pp2:
                pp_num = int(m_pp2.group(1))
                i = j + 1  # advance past PP label

        if pp_num is None or pp_num in seen_pp:
            continue

        # ── Horse name (next non-empty line after PP label) ───────────────
        j, horse_line = _next_nonempty(i)
        if j >= n:
            break
        i = j + 1

        if (not _re_horse.match(horse_line)
                or horse_line.upper().startswith('ML ')
                or len(horse_line) < 2):
            continue  # not a valid horse-name line

        is_scr = bool(re.search(r'\bSCR\b', horse_line))
        if is_scr:
            horse_line = re.sub(r'\s*\bSCR\b\s*', ' ', horse_line).strip()

        horse_name = horse_line.title()
        seen_pp.add(pp_num)

        jockey = trainer = ml_str = ml_dec = None

        # ── Scan block body for J:, T:, ML ───────────────────────────────
        limit = min(i + 20, n)
        while i < limit:
            l = lines[i].strip()
            i += 1

            if not l or _re_noise.search(l) or _re_pgcnt.match(l):
                continue

            if l in ('-', '–', '—'):  # lone visual separator
                continue

            # Next runner block starts with a bare integer followed by PP{N}
            if _re_digit.match(l):
                k, nxt2 = _next_nonempty(i)
                if k < n and _re_pp.match(nxt2):
                    i -= 1  # push back so outer loop picks up this integer
                    break
                continue  # standalone number inside stats — skip

            # Bare PP{N} (entry number missing in this block)
            if _re_pp.match(l):
                i -= 1
                break

            mj = _re_jockey.match(l)
            if mj:
                jockey = mj.group(1).strip() or None
                continue

            mt = _re_trainer.match(l)
            if mt:
                trainer = mt.group(1).strip() or None
                continue

            mml = _re_ml.search(l)
            if mml:
                raw_ml = mml.group(1).strip()
                ml_str, ml_dec = _parse_ml_1stbet(raw_ml)
                continue

        runners.append(
            _runner_dict(pp_num, horse_name, jockey, trainer, ml_str, ml_dec, is_scr)
        )

    if runners:
        runners.sort(key=lambda r: r['post_position'])
    _log.debug("1stbet multiline: %d runners", len(runners))
    return runners


def _pick_best_1stbet_parse(
    multiline: list[dict],
    compact:   list[dict],
    field_size_hdr: int | None,
) -> tuple[list[dict], str]:
    """Choose between multiline-block and compact-stream parse results.

    Primary: unique post-position count (more is better).
    If field_size_hdr is known: prefer the parse whose count is closer.
    Ties: multiline wins (it is the primary format for line-preserving exports).
    """
    ml_pps = len({r['post_position'] for r in multiline})
    cp_pps = len({r['post_position'] for r in compact})

    _log.debug(
        "1stbet pick_best: multiline=%d  compact=%d  field_size_hdr=%s",
        ml_pps, cp_pps, field_size_hdr,
    )

    if field_size_hdr and ml_pps != cp_pps:
        if abs(cp_pps - field_size_hdr) < abs(ml_pps - field_size_hdr):
            _log.debug(
                "1stbet pick_best: compact (closer to field_size %d)", field_size_hdr
            )
            return compact, "compact"

    if cp_pps > ml_pps:
        _log.debug("1stbet pick_best: compact (%d > %d PPs)", cp_pps, ml_pps)
        return compact, "compact"

    _log.debug("1stbet pick_best: multiline (%d PPs)", ml_pps)
    return multiline, "multiline"


# ── Generic runner line parsers ───────────────────────────────────────────────

def _parse_race_runners(text: str, warnings: list[str]) -> list[dict]:
    """Extract runners from generic race entry PDF text.
    Tries multiple patterns; falls back to simpler number+name extraction.
    """
    runners: list[dict] = []
    seen: set[str] = set()

    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 5:
            continue

        # Main pattern: "5. HORSE NAME  J. Jockey / T. Trainer  5-2"
        m = re.match(
            r'^(\d{1,2})[.\)\s]\s+'
            r'(?:\(\d{1,2}\)\s+)?'
            r'([A-Z][A-Za-z\'\s\-\.]{2,35?}?)'
            r'\s{2,}'
            r'(.*?)$',
            line
        )
        if not m:
            m2 = re.match(r'^(\d{1,2})[.\)]\s+([A-Z][A-Z\'\s\-\.]{2,34})\s*$', line)
            if m2:
                pp   = int(m2.group(1))
                name = m2.group(2).strip().title()
                if name and name not in seen and 2 <= len(name) <= 40:
                    seen.add(name)
                    runners.append(_runner_dict(pp, name))
            continue

        pp   = int(m.group(1))
        name = m.group(2).strip().title()
        rest = m.group(3).strip()

        if name in seen or len(name) < 2 or len(name) > 40:
            continue
        seen.add(name)

        odds_str = odds_dec = None
        om = re.search(r'(\d+[-/]\d+|\d+\.\d+)\s*$', rest)
        if om:
            odds_str = om.group(1)
            odds_dec = _norm_odds(odds_str)
            rest = rest[:om.start()].strip()

        jockey = trainer = None
        if "/" in rest:
            parts = rest.split("/", 1)
            jockey  = parts[0].strip() or None
            trainer = parts[1].strip() or None
        elif rest:
            jockey = rest or None

        is_scratched = bool(re.search(r'\bscratched?\b', line, re.I))
        runners.append(_runner_dict(pp, name, jockey, trainer, odds_str, odds_dec, is_scratched))

    if len(runners) < 2:
        skip_words = {
            "Race","Track","Post","Time","Date","Purse","Field","Surface",
            "Saturday","Sunday","Monday","Tuesday","Wednesday","Thursday","Friday",
        }
        for m in re.finditer(
            r'^(\d{1,2})[\s.\)]+([A-Z][A-Za-z\']+(?:\s+[A-Za-z\']+){0,4})',
            text, re.M
        ):
            pp   = int(m.group(1))
            name = m.group(2).strip().title()
            if name in seen or len(name) < 3 or name in skip_words:
                continue
            if any(s in name for s in skip_words):
                continue
            seen.add(name)
            runners.append(_runner_dict(pp, name))

    if not runners:
        warnings.append(
            "No runner lines found — PDF layout may be non-standard. "
            "Try the Screenshot Ingest tool for image-based PDFs."
        )

    return runners


def _runner_dict(
    pp: int,
    name: str,
    jockey: str | None = None,
    trainer: str | None = None,
    morning_line: str | None = None,
    morning_line_decimal: float | None = None,
    is_scratched: bool = False,
) -> dict:
    return {
        "program_number":       pp,
        "post_position":        pp,
        "horse_name":           name,
        "jockey":               jockey,
        "trainer":              trainer,
        "morning_line":         morning_line,
        "morning_line_decimal": morning_line_decimal,
        "current_odds":         None,
        "current_odds_decimal": None,
        "is_scratched":         is_scratched,
        "last_5":               [],
    }


def _parse_results_runners(text: str, warnings: list[str]) -> tuple[list[dict], list[dict]]:
    """Extract finish-order runner rows and scratch list from results chart text."""
    runners: list[dict] = []
    scratches: list[dict] = []
    seen: set[str] = set()

    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 5:
            continue

        is_scr = bool(re.search(r'\bscratched?\b', line, re.I))

        m = re.match(
            r'^(\d{1,2})[.\)\s]\s+'
            r'(?:\(\d{1,2}\)\s+)?'
            r'([A-Z][A-Za-z\'\s\-\.]{2,34?}?)\s{2,}'
            r'(.*?)$',
            line
        )
        if not m:
            continue

        pp   = int(m.group(1))
        name = m.group(2).strip().title()
        rest = m.group(3).strip()

        if name in seen or len(name) < 2:
            continue
        seen.add(name)

        if is_scr:
            scratches.append({"program_number": pp, "horse_name": name, "is_scratched": True})
            continue

        finish = None
        fm = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\b', rest)
        if fm:
            finish = int(fm.group(1))

        odds_str = odds_dec = None
        om = re.search(r'\b(\d+[-/]\d+|\d+\.\d+)\b', rest)
        if om:
            odds_str = om.group(1)
            odds_dec = _norm_odds(odds_str)

        jockey = trainer = None
        if "/" in rest:
            parts = rest.split("/", 1)
            jockey  = parts[0].strip() or None
            trainer = parts[1].strip() or None

        runners.append({
            "program_number":        pp,
            "post_position":         pp,
            "horse_name":            name,
            "official_finish":       finish,
            "finish_position":       finish,
            "official_odds":         odds_str,
            "official_odds_decimal": odds_dec,
            "jockey":                jockey,
            "trainer":               trainer,
            "is_scratched":          False,
            "is_disqualified":       False,
            "beaten_lengths":        None,
            "speed_figure":          None,
            "beyer_figure":          None,
            "earned_purse":          None,
            "final_time":            None,
            "comment":               None,
        })

    runners.sort(key=lambda r: r["official_finish"] or 99)

    if not runners:
        warnings.append(
            "No result lines found — PDF layout may be non-standard. "
            "Use the CSV import fallback below."
        )

    return runners, scratches


# ── 1/ST BET / Equibase chart result parser ───────────────────────────────────

def _parse_results_1stbet(text: str, warnings: list[str]) -> dict:
    """Parse Equibase official chart PDFs (the format served by 1/ST BET results).

    pdfplumber column extraction merges the 'Last Raced' column with each
    result row, yielding lines like:
        8Apr267PEN8 2 SpinningMusician(Beato,Inoel) 122 Lb 1 1 1 4 ... 0.80* 2p,...

    Chart rows appear in finish order, so finish_position is assigned sequentially.
    Returns {"finishers": list[dict], "scratches": list[str]}.
    """
    finishers: list[dict] = []
    scratches: list[str] = []

    lines = text.splitlines()

    header_idx = -1
    for i, line in enumerate(lines):
        if re.search(r'Last\s+Raced\s+Pgm\s+Horse\s+Name', line, re.I):
            header_idx = i
            break
    if header_idx == -1:
        return {"finishers": [], "scratches": []}

    _boundary = re.compile(
        r'^(?:Fractional\s+Times?|Winner\b|Pgm\s+Horse(?:\s+Name|\s+Win)\b|'
        r'Total\s+WPS|Past\s+Performance|Trainers?:|Owners?:|Footnotes?|Copyright)',
        re.I,
    )
    body: list[str] = []
    for line in lines[header_idx + 1:]:
        if _boundary.match(line.strip()):
            break
        body.append(line)

    # Each result row: {lastRaced}{raceRef} {pgm} {Name}({Jockey,Name}) {wgt} {ME} {pp} {rest…}
    # Scratch line appears after the Fractional Times boundary in Equibase charts,
    # so search the full text rather than just the body window.
    re_scratch = re.compile(r'Scratched\s+Horse\(?s?\)?[:\s]+(.+)', re.I)
    for line in lines:
        ms = re_scratch.match(line.strip())
        if ms:
            for part in re.split(r',(?=\s*[A-Z])', ms.group(1)):
                horse = re.sub(r'\s*\([^)]*\)\s*$', '', part).strip()
                if horse:
                    scratches.append(horse)

    re_result = re.compile(
        r'^\s*\d{1,2}[A-Za-z]{3}\d{2}\S*\s+'  # last-raced + race-ref (e.g. 8Apr267PEN8)
        r'(\d{1,2})\s+'                          # group 1: program number
        r'([A-Za-z]\S+?)'                         # group 2: HorseName (CamelCase, spaces removed by pdfplumber)
        r'\(([^)]+)\)\s+'                         # group 3: Jockey,Name
        r'\d{2,3}\s+'                             # weight
        r'\S+\s+'                                  # M/E flags (e.g. Lb, Lbf)
        r'(\d{1,2})\s+'                            # group 4: post position
        r'(.+)$'                                   # group 5: Start..Fin Odds Comments
    )

    finish_pos = 0
    for line in body:
        stripped = line.strip()
        if not stripped:
            continue

        mr = re_result.match(stripped)
        if not mr:
            continue

        pgm      = int(mr.group(1))
        pp       = int(mr.group(4))
        raw_name = mr.group(2)
        raw_jock = mr.group(3)
        rest     = mr.group(5)

        # Re-insert spaces lost to pdfplumber column compression (CamelCase → Title Case)
        horse_name = re.sub(r'([a-z])([A-Z])', r'\1 \2', raw_name)
        jockey     = re.sub(r',([A-Za-z])', r', \1', raw_jock)

        # Odds = rightmost pure-numeric token (decimal or integer, optional trailing *)
        # Fractional-position tokens contain "/" (e.g. 31/2, 53/4) and never match
        odds_raw: str | None = None
        for tok in rest.split():
            if re.match(r'^\d+(?:\.\d+)?\*?$', tok):
                odds_raw = tok

        if odds_raw:
            odds_str = odds_raw.rstrip('*')
            odds_dec = _norm_odds(odds_str)
        else:
            odds_str = odds_dec = None
            warnings.append(f"1/ST BET results: no odds token found for {horse_name}")

        finish_pos += 1
        finishers.append({
            "program_number":        pgm,
            "post_position":         pp,
            "horse_name":            horse_name,
            "finish_position":       finish_pos,
            "official_finish":       finish_pos,
            "final_odds":            odds_str,
            "official_odds":         odds_str,
            "official_odds_decimal": odds_dec,
            "jockey":                jockey,
            "trainer":               None,
            "is_scratched":          False,
            "is_disqualified":       False,
            "beaten_lengths":        None,
            "speed_figure":          None,
            "beyer_figure":          None,
            "earned_purse":          None,
            "final_time":            None,
            "comment":               None,
        })

    return {"finishers": finishers, "scratches": scratches}


# ── Public API ────────────────────────────────────────────────────────────────

def parse_race_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    """Parse a pre-race PDF (1/ST BET race detail, Equibase, DRF, sportsbook).

    Returns:
        ok, error, warnings,
        track_code, track_name, race_date, race_number,
        distance_text, surface, race_type, purse_usd, field_size,
        runners  — list of runner dicts (see _runner_dict)
        is_1stbet — True when the 1/ST BET-specific parser was used
    """
    warnings: list[str] = []

    try:
        text = _extract_text(pdf_bytes)
    except ImportError as e:
        return {"ok": False, "error": str(e), "warnings": [], "runners": []}
    except Exception as e:
        return {"ok": False, "error": f"PDF read error: {e}", "warnings": [], "runners": []}

    if not text.strip():
        return {
            "ok": False,
            "error": (
                "No text found in PDF — likely a scanned/image-only document. "
                "Use the Screenshot Ingest tool (requires ANTHROPIC_API_KEY)."
            ),
            "warnings": [], "runners": [],
        }

    detected_1stbet = _is_1stbet(text)
    _log.debug(
        "parse_race_pdf: raw_text=%d chars  is_1stbet=%s",
        len(text), detected_1stbet,
    )

    if detected_1stbet:
        # ── 1/ST BET path ────────────────────────────────────────────────────
        race_date     = _extract_date_1stbet(text, warnings)
        race_number   = _extract_race_number_1stbet(text)
        track_code, track_name = _extract_track_1stbet(text)
        hdr           = _extract_1stbet_header(text)
        distance_text = hdr.get("distance_text") or _extract_distance(text)
        surface       = hdr.get("surface") or _extract_surface(text)
        race_type     = hdr.get("race_type") or _extract_race_type(text)
        purse_usd     = hdr.get("purse_usd") or _extract_purse(text)
        # field_size from header is more reliable than counting runners
        # (includes scratches already present in the PDF)
        field_size_hdr = hdr.get("field_size")

        _log.debug(
            "parse_race_pdf: is_1stbet=True  raw=%d chars  field_size_hdr=%s",
            len(text), field_size_hdr,
        )

        # ── Two-mode runner extraction ────────────────────────────────────
        # Run both parsers independently; pick the one with more unique PPs
        # (or the one closest to the header field count when available).
        _ml_warns: list[str] = []
        _cp_warns: list[str] = []
        runners_primary  = _parse_race_runners_1stbet_multiline(text.splitlines(), _ml_warns)
        runners_fallback = _parse_race_runners_1stbet_fallback(text, _cp_warns)

        _log.debug(
            "1stbet runners: multiline=%d  compact=%d",
            len(runners_primary), len(runners_fallback),
        )

        runners, _parse_source = _pick_best_1stbet_parse(
            runners_primary, runners_fallback, field_size_hdr,
        )
        # Merge warnings from the winning parser only
        warnings.extend(_ml_warns if _parse_source == "multiline" else _cp_warns)

        _log.debug(
            "1stbet canonical: %d runners  source=%s",
            len(runners), _parse_source,
        )

        # Hard error: both parsers should not return 0 if PP anchors are present
        if "PP1" in text and "PP2" in text and not runners:
            _log.error(
                "1stbet: PP1+PP2 in raw_text but 0 canonical runners — "
                "multiline=%d compact=%d  raw_text[:1500]=%r",
                len(runners_primary), len(runners_fallback), text[:1500],
            )
    else:
        # ── Generic path ─────────────────────────────────────────────────────
        race_date     = _extract_date(text)
        race_number   = _extract_race_number(text)
        track_code, track_name = _extract_track(text)
        distance_text = _extract_distance(text)
        surface       = _extract_surface(text)
        race_type     = _extract_race_type(text)
        purse_usd     = _extract_purse(text)
        runners          = _parse_race_runners(text, warnings)
        runners_primary: list[dict] = runners
        runners_fallback: list[dict] = []
        field_size_hdr = None

    if not race_date:
        warnings.append("Could not extract race date")
    if race_number is None:
        warnings.append("Could not extract race number")
    if not track_code:
        warnings.append(
            "Could not extract track code — will abbreviate from track name if available"
        )
    if not distance_text:
        warnings.append("Could not extract distance")
    if not surface:
        warnings.append("Could not extract surface")

    active     = [r for r in runners if not r.get("is_scratched")]
    field_size = field_size_hdr or len(active)

    # Validate parsed runner count against the header field size (1/ST BET path)
    if field_size_hdr and runners:
        if not (field_size_hdr - 1 <= len(runners) <= field_size_hdr + 1):
            warnings.append(
                f"Runner count mismatch: header says {field_size_hdr} horses "
                f"but {len(runners)} were parsed — verify the PDF format."
            )

    _res = _resolve_track(track_name=track_name, track_code=track_code)

    return {
        "ok":            True,
        "error":         None,
        "warnings":      warnings,
        "track_code":    track_code,
        "track_name":    track_name,
        "race_date":     race_date,
        "race_number":   race_number,
        "distance_text": distance_text,
        "surface":       surface,
        "race_type":     race_type,
        "purse_usd":     purse_usd,
        "field_size":    field_size,
        "runners":          runners,
        "runners_count":    len(runners),
        "runners_primary":  runners_primary,
        "runners_fallback": runners_fallback,
        "is_1stbet":        detected_1stbet,
        "raw_text":      text,
        # resolver enrichment — never overwrites the raw parsed fields above
        "track_code_resolved":     _res["track_code"],
        "track_name_canonical":    _res["track_name_canonical"],
        "track_resolution_source": _res["resolution_source"],
    }


def parse_results_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    """Parse an Equibase official chart PDF (post-race results).

    Returns:
        ok, error, warnings,
        track_code, track_name, race_date, race_number,
        distance_text, surface, race_type, purse_usd,
        track_condition, final_time, field_size,
        runners  — ordered by finish position
        scratches — scratch-only rows
        footnotes — raw text if present (≤500 chars)
    """
    warnings: list[str] = []

    try:
        text = _extract_text(pdf_bytes)
    except ImportError as e:
        return {"ok": False, "error": str(e), "warnings": [], "runners": [], "scratches": []}
    except Exception as e:
        return {
            "ok": False, "error": f"PDF read error: {e}",
            "warnings": [], "runners": [], "scratches": [],
        }

    if not text.strip():
        return {
            "ok": False,
            "error": "No text found in PDF — use the CSV Results Import fallback.",
            "warnings": [], "runners": [], "scratches": [],
        }

    race_date     = _extract_date(text)
    race_number   = _extract_race_number(text)
    track_code, track_name = _extract_track(text)
    distance_text = _extract_distance(text)
    surface       = _extract_surface(text)
    race_type     = _extract_race_type(text)
    purse_usd     = _extract_purse(text)

    track_condition = None
    mc = re.search(
        r'\b(fast|good|sloppy|muddy|heavy|yielding|firm|soft|wet[\s-]fast)\b', text, re.I
    )
    if mc:
        track_condition = mc.group(1).title()

    final_time = None
    mt = re.search(r'(?:final\s+time|time)[:\s]+(\d+:\d{2}(?:\.\d+)?|\d+\.\d+)', text, re.I)
    if mt:
        final_time = mt.group(1)

    footnotes = None
    mf = re.search(r'FOOTNOTES?\s*[:.]?\s*(.*?)(?:\n\n|\Z)', text, re.I | re.S)
    if mf:
        footnotes = mf.group(1).strip()[:500]

    if not race_date:
        warnings.append("Could not extract race date")
    if race_number is None:
        warnings.append("Could not extract race number")
    if not track_code:
        warnings.append("Could not extract track code")

    # 1/ST BET / Equibase chart: detected by the standard results-chart header
    runners: list[dict] = []
    scratches: list[dict] = []
    if re.search(r'Last\s+Raced\s+Pgm\s+Horse\s+Name', text):
        parsed = _parse_results_1stbet(text, warnings)
        runners  = parsed["finishers"]
        scratches = [{"horse_name": n, "is_scratched": True} for n in parsed["scratches"]]
    if not runners:
        runners, scratches = _parse_results_runners(text, warnings)

    finishers = [r for r in runners if not r.get("is_scratched")]

    return {
        "ok":              True,
        "error":           None,
        "warnings":        warnings,
        "track_code":      track_code,
        "track_name":      track_name,
        "race_date":       race_date,
        "race_number":     race_number,
        "distance_text":   distance_text,
        "surface":         surface,
        "race_type":       race_type,
        "purse_usd":       purse_usd,
        "track_condition": track_condition,
        "final_time":      final_time,
        "field_size":      len(finishers),
        "runners":         runners,
        "scratches":       scratches,
        "footnotes":       footnotes,
    }
