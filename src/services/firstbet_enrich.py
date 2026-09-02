"""
1/ST BET PDF enrichment service.

parse_race_pdf() in pdf_ingest.py creates the race card and entries.
This service re-scans the same extracted text to pull the deeper data
that the runner parser skips:
    · Career W / P / S %
    · RECENT 5 summary (ITM / wins in last 5 starts)
    · Up to 5 past-performance start blocks
    · Trainer / jockey (already in runners — this persists them)

Public API
----------
ensure_firstbet_pp_table(conn)
    Idempotent DDL.  Call once per connection before any other function.

enrich_runners_1stbet(text, runners, *, race_date, race_distance_yards)
    Pure text-parsing step.  Returns enriched copies of the runner dicts:
        career_win_pct, career_place_pct, career_itm_pct  (float 0-1 or None)
        recent_5_itm, recent_5_wins                       (int or None)
        last_5   — list of past-start dicts (most-recent = index 0)

enrich_entries_from_1stbet(conn, card_id, enriched_runners, *, race_date, race_distance_yards)
    DB-write step.  Returns {ok, n_enriched, n_pp_rows, n_stat_rows, warnings}.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import Any


# ── Table DDL ──────────────────────────────────────────────────────────────────

_PP_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS firstbet_pp_starts (
    pp_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id        INTEGER NOT NULL,
    entry_id       INTEGER NOT NULL REFERENCES entries(entry_id),
    start_rank     INTEGER NOT NULL,
    race_date      TEXT,
    track_code     TEXT,
    finish_position INTEGER,
    field_size     INTEGER,
    odds_str       TEXT,
    distance_text  TEXT,
    surface        TEXT,
    race_class     TEXT,
    purse          INTEGER,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(entry_id, start_rank)
)"""

_STAT_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS firstbet_career_stats (
    stat_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id         INTEGER NOT NULL UNIQUE REFERENCES entries(entry_id),
    card_id          INTEGER NOT NULL,
    career_win_pct   REAL,
    career_place_pct REAL,
    career_itm_pct   REAL,
    recent_5_itm     INTEGER,
    recent_5_wins    INTEGER,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)"""


def ensure_firstbet_pp_table(conn: sqlite3.Connection) -> None:
    """Create firstbet_pp_starts and firstbet_career_stats if absent. Idempotent."""
    conn.execute(_PP_TABLE_DDL)
    conn.execute(_STAT_TABLE_DDL)
    conn.commit()


# ── Surface / class maps ───────────────────────────────────────────────────────

_SURF_NORM: dict[str, str] = {
    "dirt": "D",  "turf": "T",  "synthetic": "S",
    "all-weather": "AW",  "all weather": "AW",  "polytrack": "S",
}

_CLASS_MAP: dict[str, str] = {
    "CLM": "Claiming",         "MCL": "Maiden Claiming",
    "MSW": "Maiden Special Weight", "MSP": "Maiden Special Weight",
    "ALW": "Allowance",        "AOC": "Allowance Optional Claiming",
    "OC":  "Optional Claiming","STK": "Stakes",
    "HCP": "Handicap",         "WMC": "Waiver Maiden Claiming",
}


# ── Date helpers ───────────────────────────────────────────────────────────────

def _parse_pp_date(s: str, race_year: int) -> str | None:
    """Parse 'M/D/YY' or 'M/D' → ISO 'YYYY-MM-DD'."""
    m = re.match(r'^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$', s.strip())
    if not m:
        return None
    mo, day = int(m.group(1)), int(m.group(2))
    yr_raw = m.group(3)
    if yr_raw:
        yr = int(yr_raw)
        if yr < 100:
            yr += 2000
    else:
        yr = race_year
    try:
        date(yr, mo, day)
        return f"{yr}-{mo:02d}-{day:02d}"
    except ValueError:
        return None


def _days_since(past_iso: str | None, race_iso: str) -> int | None:
    """Days from past_iso to race_iso; None on any error."""
    if not past_iso:
        return None
    try:
        diff = (date.fromisoformat(race_iso) - date.fromisoformat(past_iso)).days
        return diff if diff >= 0 else None
    except (ValueError, TypeError):
        return None


# ── Distance helper ────────────────────────────────────────────────────────────

def _parse_distance_yards(dist_text: str | None) -> int | None:
    """Parse 'NF', 'N 1/2F', 'NM', 'N 1/16 Miles' etc. to integer yards."""
    if not dist_text:
        return None
    s = dist_text.strip().lower().replace("½", " 1/2")
    # "4 1/2f" — must precede generic digit+f match
    m = re.match(r'^(\d+)\s+1/2\s*f', s)
    if m:
        return int(round((float(m.group(1)) + 0.5) * 220))
    m = re.match(r'^(\d+(?:\.\d+)?)\s*f', s)
    if m:
        return int(round(float(m.group(1)) * 220))
    m = re.match(r'^(\d+)\s+(\d+)/(\d+)\s*m', s)
    if m:
        furlongs = (int(m.group(1)) + int(m.group(2)) / int(m.group(3))) * 8
        return int(round(furlongs * 220))
    m = re.match(r'^(\d+(?:\.\d+)?)\s*m', s)
    if m:
        return int(round(float(m.group(1)) * 8 * 220))
    return None


def _dist_match(dist_text: str | None, race_yards: int) -> bool:
    """True if dist_text is within ±110 yards (±0.5f) of race_yards."""
    yards = _parse_distance_yards(dist_text)
    return yards is not None and abs(yards - race_yards) <= 110


# ── Horse-section extractor ────────────────────────────────────────────────────

_RE_DIGIT = re.compile(r'^\d{1,2}$')


def _candidate_horse(line: str) -> tuple[str, str] | None:
    """Return (name_part, trailing_token) if line looks like a 1/ST BET horse header."""
    parts = line.rsplit(None, 1)
    if len(parts) != 2:
        return None
    name_part, trailing = parts
    trailing = trailing.strip()
    if trailing.upper() == "SCR":
        pass
    elif (not re.match(r'^\d+[-/]\d+$', trailing)
          and not re.match(r'^\d+(?:\.\d+)?$', trailing)):
        return None
    if not re.match(r'^[A-Z][A-Z0-9\'\s\-\.]+$', name_part):
        return None
    return name_part.strip(), trailing


def _find_horse_sections(text: str) -> dict[str, list[str]]:
    """Return {horse_name_title: section_lines} for each confirmed horse block.

    Uses the same 2-line lookahead rule as the runner parser to avoid false
    positives (e.g. 'PENN NATIONAL R 6' looks like a horse name but isn't).
    """
    lines = text.splitlines()
    n = len(lines)
    block_starts: list[tuple[int, str]] = []

    i = 0
    while i < n:
        line = lines[i].strip()
        if line:
            cand = _candidate_horse(line)
            if cand:
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and _RE_DIGIT.match(lines[j].strip()):
                    block_starts.append((i, cand[0].title()))
        i += 1

    sections: dict[str, list[str]] = {}
    for idx, (start_i, name) in enumerate(block_starts):
        end_i = block_starts[idx + 1][0] if idx + 1 < len(block_starts) else n
        sections[name] = [lines[k].strip() for k in range(start_i, end_i)]

    return sections


# ── Per-section parsers ────────────────────────────────────────────────────────

def _parse_career_stats(lines: list[str]) -> dict[str, float | None]:
    """Extract 'W N% P N% S N%' from section lines.

    Returns fractions in [0, 1]: {"w": float|None, "p": float|None, "s": float|None}.
    """
    for line in lines:
        mw = re.search(r'\bW\s+(\d+(?:\.\d+)?)\s*%', line, re.I)
        mp = re.search(r'\bP\s+(\d+(?:\.\d+)?)\s*%', line, re.I)
        ms = re.search(r'\bS\s+(\d+(?:\.\d+)?)\s*%', line, re.I)
        if mw or mp or ms:
            return {
                "w": round(float(mw.group(1)) / 100, 4) if mw else None,
                "p": round(float(mp.group(1)) / 100, 4) if mp else None,
                "s": round(float(ms.group(1)) / 100, 4) if ms else None,
            }
    return {"w": None, "p": None, "s": None}


def _parse_recent5(lines: list[str]) -> dict[str, int | None]:
    """Extract 'RECENT 5 - TOP 3 N' (and optional 'WINS N').

    Returns {"wins": int|None, "itm": int|None}.
    """
    for line in lines:
        if not re.search(r'RECENT\s*5', line, re.I):
            continue
        itm_m   = re.search(r'TOP\s*3\s+(\d+)',  line, re.I)
        wins_m  = re.search(r'WINS\s+(\d+)',      line, re.I)
        if not wins_m:
            wins_m = re.search(r'\bW\s+(\d+)\b',  line)
        return {
            "itm":  int(itm_m.group(1))  if itm_m  else None,
            "wins": int(wins_m.group(1)) if wins_m else None,
        }
    return {"wins": None, "itm": None}


# ── Past-performance block parser ──────────────────────────────────────────────

# Track/date line: "Penn National 16 Apr 2026" or "Parx Racing 3 May 2026"
# The date portion is "D Mon YYYY" at the end of the line.
_MONTH_ABBREVS = (
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    "January|February|March|April|June|July|August|"
    "September|October|November|December"
)
_RE_PP_TRACK_DATE = re.compile(
    r'^(.+?)\s+(\d{1,2})\s+(' + _MONTH_ABBREVS + r')\s+(\d{4})\s*$',
    re.I,
)

_MONTH_MAP_EN: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,  "may": 5,  "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

# Track lookup: lower-cased partial name → code (reuse _TRACK_CODES from pdf_ingest)
_PP_TRACK_LOOKUP: dict[str, str] = {
    "penn national": "PEN", "penn nat": "PEN",
    "parx":          "PRX",
    "aqueduct":      "AQU",
    "belmont":       "BEL",
    "saratoga":      "SAR",
    "gulfstream":    "GP",
    "santa anita":   "SA",
    "keeneland":     "KEE",
    "churchill":     "CD",
    "pimlico":       "PIM",
    "del mar":       "DMR",
    "oaklawn":       "OP",
    "fair grounds":  "FG",
    "turfway":       "TP",
    "woodbine":      "WO",
    "monmouth":      "MTH",
    "laurel":        "LRL",
    "tampa bay":     "TAM",
    "charles town":  "CT",
    "remington":     "RP",
    "hawthorne":     "HAW",
    "colonial":      "CNL",
    "suffolk":       "SUF",
    "finger lakes":  "FL",
    "presque isle":  "PID",
    "evangeline":    "EVD",
    "golden gate":   "GG",
}


def _norm_pp_track(raw_name: str) -> str:
    """Convert full track name to code abbreviation, or return first 4 chars."""
    low = raw_name.strip().lower()
    for key, code in _PP_TRACK_LOOKUP.items():
        if key in low:
            return code
    return raw_name.strip().upper()[:4]


def _parse_pp_date_long(day_s: str, month_s: str, year_s: str) -> str | None:
    """Convert 'D Mon YYYY' tokens to ISO 'YYYY-MM-DD'."""
    mo = _MONTH_MAP_EN.get(month_s.strip().lower())
    if not mo:
        return None
    try:
        yr = int(year_s)
        d  = int(day_s)
        date(yr, mo, d)  # validate
        return f"{yr}-{mo:02d}-{d:02d}"
    except (ValueError, TypeError):
        return None


# Distance: "8.3F", "6F", "1M", "1 1/16M"
_RE_DIST_STR = re.compile(
    r'\b((?:\d+\s+\d+/\d+|\d+(?:\.\d+)?)\s*[MFmf])\b'
)

_RE_SURFACE = re.compile(r'\b(Dirt|Turf|Synthetic|All.?Weather)\b', re.I)

# Result line: "2nd (16/1) 6 Horses Claiming $14,000 Race 7"
_RE_FINISH  = re.compile(r'^(\d{1,2})(?:st|nd|rd|th)\b', re.I)
_RE_ODDS_PP = re.compile(r'\((\d+/\d+)\)')          # odds in parens: (16/1)
_RE_FIELD   = re.compile(r'\b(\d{1,2})\s+Horses\b', re.I)
_RE_PURSE   = re.compile(r'\$([\d,]+)')

# Full-text class names in result line
_RE_CLASS_FULL = re.compile(
    r'\b(Maiden\s+Special\s+Weight|Maiden\s+Claiming|Allowance\s+Optional\s+Claiming|'
    r'Optional\s+Claiming|Allowance|Claiming|Stakes?|Handicap|'
    r'Waiver\s+Maiden(?:\s+Claiming)?|Starter\s+Allowance)\b',
    re.I,
)


def _parse_result_line(line: str) -> dict[str, Any]:
    """Extract finish, odds, field_size, race_class, purse from a 1/ST BET result line.

    Expected format: "2nd (16/1) 6 Horses Claiming $14,000 Race 7"
    """
    result: dict[str, Any] = {}

    m = _RE_FINISH.match(line.strip())
    if m:
        result["finish_position"] = int(m.group(1))

    m = _RE_ODDS_PP.search(line)
    if m:
        result["odds_str"] = m.group(1)

    m = _RE_FIELD.search(line)
    if m:
        result["field_size"] = int(m.group(1))

    m = _RE_CLASS_FULL.search(line)
    if m:
        result["race_class"] = m.group(1).strip().title()

    m = _RE_PURSE.search(line)
    if m:
        try:
            result["purse"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    return result


def _parse_pp_starts(
    lines: list[str],
    race_year: int,
    max_starts: int = 5,
) -> list[dict[str, Any]]:
    """Parse up to max_starts past-performance blocks from post-T: section lines.

    Each 1/ST BET block:
        "Track Name D Mon YYYY"   ← block-start signal
        "Nth (odds) N Horses CLASS $PURSE Race N"
        "NF Surface/Condition"
        "notes free text"         ← optional
    """
    starts: list[dict[str, Any]] = []
    i = 0
    n = len(lines)

    while i < n and len(starts) < max_starts:
        line = lines[i]
        m_td = _RE_PP_TRACK_DATE.match(line)
        if not m_td:
            i += 1
            continue

        track_code = _norm_pp_track(m_td.group(1))
        pp_date    = _parse_pp_date_long(m_td.group(2), m_td.group(3), m_td.group(4))

        start: dict[str, Any] = {
            "track_code":      track_code,
            "race_date":       pp_date,
            "finish_position": None,
            "field_size":      None,
            "odds_str":        None,
            "distance_text":   None,
            "surface":         None,
            "race_class":      None,
            "purse":           None,
            "notes":           None,
        }
        i += 1

        # Skip blank lines, consume result line
        while i < n and not lines[i]:
            i += 1
        if i < n and not _RE_PP_TRACK_DATE.match(lines[i]):
            start.update({k: v for k, v in _parse_result_line(lines[i]).items()
                          if v is not None})
            i += 1

            # Skip blank, consume distance/surface line
            while i < n and not lines[i]:
                i += 1
            if i < n and not _RE_PP_TRACK_DATE.match(lines[i]):
                dl = lines[i]
                md = _RE_DIST_STR.search(dl)
                if md:
                    start["distance_text"] = md.group(1).strip()
                ms = _RE_SURFACE.search(dl)
                if ms:
                    raw = ms.group(1).lower().replace(" ", "-")
                    start["surface"] = _SURF_NORM.get(raw, ms.group(1)[:1].upper())
                i += 1

                # Skip blank, consume optional notes
                while i < n and not lines[i]:
                    i += 1
                if (i < n
                        and not _RE_PP_TRACK_DATE.match(lines[i])
                        and not _candidate_horse(lines[i])):
                    start["notes"] = lines[i]
                    i += 1

        starts.append(start)

    return starts


# ── Main text enricher ─────────────────────────────────────────────────────────

def enrich_runners_1stbet(
    text: str,
    runners: list[dict[str, Any]],
    *,
    race_date: str,
    race_distance_yards: int,
) -> list[dict[str, Any]]:
    """Enrich runner dicts with career stats, recent-5 summary, and PP history.

    Accepts the raw extracted PDF text and the already-parsed runners list.
    Returns a new list of dicts; originals are not mutated.

    Added keys per runner:
        career_win_pct, career_place_pct, career_itm_pct
        recent_5_itm, recent_5_wins
        last_5   — list[dict] (most-recent start is index 0)
    """
    try:
        race_year = int(race_date[:4])
    except (ValueError, TypeError, IndexError):
        race_year = 2026

    sections = _find_horse_sections(text)
    enriched: list[dict[str, Any]] = []

    for runner in runners:
        r = dict(runner)
        name = r.get("horse_name", "")

        # Case-insensitive section lookup
        section = sections.get(name)
        if section is None:
            for sname, slines in sections.items():
                if sname.lower() == name.lower():
                    section = slines
                    break

        r.update({
            "career_win_pct":   None,
            "career_place_pct": None,
            "career_itm_pct":   None,
            "recent_5_itm":     None,
            "recent_5_wins":    None,
            "last_5":           [],
        })

        if section:
            # Everything after the T: line is stats / PP data
            t_idx = next(
                (i for i, l in enumerate(section) if re.match(r'^T:', l, re.I)),
                None,
            )
            post_t = section[t_idx + 1:] if t_idx is not None else section

            stats = _parse_career_stats(post_t)
            r["career_win_pct"]   = stats["w"]
            r["career_place_pct"] = stats["p"]
            r["career_itm_pct"]   = stats["s"]

            rec5 = _parse_recent5(post_t)
            r["recent_5_itm"]  = rec5["itm"]
            r["recent_5_wins"] = rec5["wins"]

            r["last_5"] = _parse_pp_starts(post_t, race_year)

        enriched.append(r)

    return enriched


# ── DB writer ──────────────────────────────────────────────────────────────────

def _upsert_person(conn: sqlite3.Connection, full_name: str, role: str) -> int | None:
    """INSERT OR IGNORE person; return person_id, or None if name is blank."""
    full_name = (full_name or "").strip()
    if not full_name:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO people (full_name, role) VALUES (?, ?)",
        (full_name, role),
    )
    row = conn.execute(
        "SELECT person_id FROM people WHERE full_name = ? AND role = ?",
        (full_name, role),
    ).fetchone()
    return row[0] if row else None


def enrich_entries_from_1stbet(
    conn: sqlite3.Connection,
    card_id: int,
    enriched_runners: list[dict[str, Any]],
    *,
    race_date: str,
    race_distance_yards: int,
) -> dict[str, Any]:
    """Persist enrichment data from enrich_runners_1stbet() to the database.

    Writes to:
        people              — trainer and jockey rows (INSERT OR IGNORE)
        entries             — trainer_id, jockey_id, last_race_days,
                              last_race_finish, dirt_starts, dist_starts
                              (only writes fields whose entries column is NULL)
        firstbet_pp_starts  — one row per past start (up to 5 per entry)
        firstbet_career_stats — one row per entry with W/P/S% and RECENT 5

    Returns {ok, n_enriched, n_pp_rows, n_stat_rows, warnings}.
    """
    warnings: list[str] = []
    n_enriched  = 0
    n_pp_rows   = 0
    n_stat_rows = 0

    try:
        ensure_firstbet_pp_table(conn)

        for r in enriched_runners:
            if r.get("is_scratched"):
                # Retain source scratches, but never give them PP-backed signal.
                continue
            name = r.get("horse_name", "")
            pp   = r.get("post_position") or r.get("program_number")
            if not pp:
                warnings.append(f"'{name}': no post position — skipped")
                continue

            row = conn.execute(
                "SELECT entry_id FROM entries WHERE card_id = ? AND post_position = ?",
                (card_id, int(pp)),
            ).fetchone()
            if not row:
                warnings.append(
                    f"'{name}' PP{pp}: entry not found in card {card_id}"
                )
                continue
            entry_id = row[0]

            # ── Trainer / jockey → people table ─────────────────────────────
            trainer_id = _upsert_person(conn, r.get("trainer") or "", "trainer")
            jockey_id  = _upsert_person(conn, r.get("jockey")  or "", "jockey")

            updates: dict[str, Any] = {}
            if trainer_id:
                updates["trainer_id"] = trainer_id
            if jockey_id:
                updates["jockey_id"] = jockey_id

            # ── Derive stats from last_5 ─────────────────────────────────────
            last5 = [s for s in (r.get("last_5") or [])
                     if s.get("finish_position") is not None]

            if last5:
                lrd = _days_since(last5[0].get("race_date"), race_date)
                if lrd is not None:
                    updates["last_race_days"] = lrd
                lrf = last5[0].get("finish_position")
                if lrf is not None:
                    updates["last_race_finish"] = int(lrf)

                # Partial surface / distance stats from last 5 (only fill NULLs)
                existing = conn.execute(
                    "SELECT dirt_starts, dist_starts FROM entries WHERE entry_id = ?",
                    (entry_id,),
                ).fetchone()
                if existing:
                    if existing[0] is None:
                        updates["dirt_starts"] = sum(
                            1 for s in last5
                            if (s.get("surface") or "").upper() == "D"
                        )
                    if existing[1] is None:
                        updates["dist_starts"] = sum(
                            1 for s in last5
                            if _dist_match(s.get("distance_text"), race_distance_yards)
                        )

            # ── Apply entry updates ──────────────────────────────────────────
            if updates:
                set_sql = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE entries SET {set_sql} WHERE entry_id = ?",
                    list(updates.values()) + [entry_id],
                )
                n_enriched += 1

            # ── firstbet_pp_starts ───────────────────────────────────────────
            for rank, start in enumerate((r.get("last_5") or [])[:5], start=1):
                try:
                    conn.execute(
                        """INSERT OR REPLACE INTO firstbet_pp_starts
                           (card_id, entry_id, start_rank, race_date, track_code,
                            finish_position, field_size, odds_str,
                            distance_text, surface, race_class, purse, notes)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            card_id, entry_id, rank,
                            start.get("race_date"),     start.get("track_code"),
                            start.get("finish_position"), start.get("field_size"),
                            start.get("odds_str"),
                            start.get("distance_text"), start.get("surface"),
                            start.get("race_class"),    start.get("purse"),
                            start.get("notes"),
                        ),
                    )
                    n_pp_rows += 1
                except Exception as exc:
                    warnings.append(f"'{name}' PP rank {rank}: {exc}")

            # ── firstbet_career_stats ────────────────────────────────────────
            w_pct = r.get("career_win_pct")
            p_pct = r.get("career_place_pct")
            s_pct = r.get("career_itm_pct")
            r5itm = r.get("recent_5_itm")
            r5win = r.get("recent_5_wins")

            if any(v is not None for v in (w_pct, p_pct, s_pct, r5itm, r5win)):
                try:
                    conn.execute(
                        """INSERT OR REPLACE INTO firstbet_career_stats
                           (entry_id, card_id, career_win_pct, career_place_pct,
                            career_itm_pct, recent_5_itm, recent_5_wins)
                           VALUES (?,?,?,?,?,?,?)""",
                        (entry_id, card_id, w_pct, p_pct, s_pct, r5itm, r5win),
                    )
                    n_stat_rows += 1
                except Exception as exc:
                    warnings.append(f"'{name}' career stats: {exc}")

        conn.commit()
        return {
            "ok":           True,
            "n_enriched":   n_enriched,
            "n_pp_rows":    n_pp_rows,
            "n_stat_rows":  n_stat_rows,
            "warnings":     warnings,
        }

    except Exception as exc:
        conn.rollback()
        return {
            "ok":           False,
            "n_enriched":   n_enriched,
            "n_pp_rows":    n_pp_rows,
            "n_stat_rows":  n_stat_rows,
            "warnings":     warnings + [f"Enrichment DB error: {exc}"],
        }
