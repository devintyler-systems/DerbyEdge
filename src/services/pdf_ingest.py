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
import re
from typing import Any

# ── Track code lookup ─────────────────────────────────────────────────────────
_TRACK_CODES: dict[str, str] = {
    "churchill":      "CD",  "pimlico":      "PIM", "belmont":    "BEL",
    "keeneland":      "KEE", "santa anita":  "SA",  "gulfstream": "GP",
    "aqueduct":       "AQU", "del mar":      "DMR", "saratoga":   "SAR",
    "oaklawn":        "OP",  "fair grounds": "FG",  "turfway":    "TP",
    "woodbine":       "WO",  "golden gate":  "GG",  "monmouth":   "MTH",
    "penn national":  "PEN", "parx":         "PRX", "laurel":     "LRL",
    "tampa bay":      "TAM", "charles town": "CT",  "remington":  "RP",
    "hawthorne":      "HAW", "colonial":     "CNL", "suffolk":    "SUF",
    "finger lakes":   "FL",  "presque isle": "PID", "evangeline": "EVD",
}

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

    Heuristic: the phrase "1/ST BET" appears in the browser tab title that
    pdfplumber extracts from the top of a printed/saved 1/ST BET page.
    It is printed on line 0 and again on each page-break repeat header, so
    searching just the first 300 characters is sufficient and fast.
    """
    return bool(re.search(r'1/ST\s+BET', text[:300], re.I))


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
    """Parse the 1/ST BET race-info line (line 2):
        "5:10 PM 5 Horses CLM $14,000 1M Dirt / Sloppy"
        "3:45 PM 8 Horses MSW $35,000 6F Turf / Firm"

    Returns a dict with keys: field_size, race_type, purse_usd,
    distance_text, surface  (all optional, absent if not found).
    """
    result: dict[str, Any] = {}
    # Find the header line: contains "X Horses" and a dollar amount
    header = None
    for line in text.splitlines()[:8]:
        if re.search(r'\d+\s+Horses', line, re.I) and re.search(r'\$[\d,]+', line):
            header = line
            break
    if not header:
        return result

    # Field size
    m = re.search(r'(\d+)\s+Horses', header, re.I)
    if m:
        result["field_size"] = int(m.group(1))

    # Race class abbreviation
    for abbr, full in _1STBET_CLASS_MAP.items():
        if re.search(r'\b' + abbr + r'\b', header):
            result["race_type"] = full
            break

    # Purse: "$14,000"
    m = re.search(r'\$([\d,]+)', header)
    if m:
        try:
            result["purse_usd"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Distance — try compound fractions first: "1 1/16M", "1 1/8M"
    m = re.search(
        r'\b1\s+(1/16|1/8|3/16|1/4|5/16|3/8|1/2|5/8|3/4)\s*M\b', header, re.I
    )
    if m:
        result["distance_text"] = f"1 {m.group(1)} Miles"
    else:
        # Simple: "1M", "1.5M", "6F", "8.5F"
        m = re.search(r'\b(\d+(?:\.\d+)?)\s*([FM])\b', header)
        if m:
            val, unit = float(m.group(1)), m.group(2).upper()
            if unit == "M" and 0.25 <= val <= 3.0:
                result["distance_text"] = "1 Mile" if val == 1.0 else f"{val} Miles"
            elif unit == "F" and 2.0 <= val <= 20.0:
                result["distance_text"] = f"{val:g} Furlongs"

    # Surface
    m = re.search(r'\b(Dirt|Turf|Synthetic)\b', header, re.I)
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
        runners       = _parse_race_runners_1stbet(text.splitlines(), warnings)
        # field_size from header is more reliable than counting runners
        # (includes scratches already present in the PDF)
        field_size_hdr = hdr.get("field_size")
    else:
        # ── Generic path ─────────────────────────────────────────────────────
        race_date     = _extract_date(text)
        race_number   = _extract_race_number(text)
        track_code, track_name = _extract_track(text)
        distance_text = _extract_distance(text)
        surface       = _extract_surface(text)
        race_type     = _extract_race_type(text)
        purse_usd     = _extract_purse(text)
        runners       = _parse_race_runners(text, warnings)
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
        "runners":       runners,
        "is_1stbet":     detected_1stbet,
        "raw_text":      text,
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
