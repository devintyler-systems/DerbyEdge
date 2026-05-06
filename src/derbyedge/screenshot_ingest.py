"""Sportsbook screenshot ingestor (v0.5.1).

Drop a sportsbook (BetOnline, FanDuel, DRF, etc.) race-card screenshot in,
get a race shell + entries + odds snapshot inserted into the DB. Vision
parsing via Anthropic Claude.

Honest limits:
    - This path inserts a race with ZERO past-performance history.
    - The trained model has nothing to learn from for those entries, so
      `score_entries` returns the base-rate fallback. Use this for the
      odds dashboard / devig / Kelly math, not for genuine model edge.

Wire:
    parsed = parse_screenshot(image_bytes)        # vision call
    race_id = ingest_parsed_race(conn, parsed)    # insert race + entries
    n = ingest_parsed_odds(conn, parsed, race_id) # insert odds snapshot
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Anthropic SDK is optional for non-vision callers / tests.
try:
    import anthropic  # type: ignore
    _HAS_ANTHROPIC = True
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore
    _HAS_ANTHROPIC = False


DEFAULT_MODEL = "claude-sonnet-4-5"

# JSON schema we coerce the vision model into. Strict keys; nullable values.
EXTRACTION_PROMPT = """You are extracting structured data from a horse-racing
sportsbook race-card screenshot. Return ONE JSON object, no prose. Schema:

{
  "track_name": str | null,         // e.g. "Mountaineer Park"
  "track_id": str | null,           // 2-4 letter Equibase code if visible, else null. e.g. "MNR", "CD", "AQU"
  "race_date": str | null,          // ISO YYYY-MM-DD
  "race_number": int | null,
  "post_time": str | null,          // local race-card time as displayed, e.g. "7:47 PM EST"
  "distance_text": str | null,      // verbatim, e.g. "1 Mile" or "6 Furlongs"
  "surface": str | null,            // "D" dirt, "T" turf, "E" all-weather, else null
  "race_type": str | null,          // e.g. "Maiden Claiming", "Allowance", "Stakes"
  "purse_usd": int | null,
  "book_id": str | null,            // sportsbook source: "betonline","fanduel","draftkings","twinspires", or null
  "runners": [
    {
      "program_number": str,        // "1", "1A", etc. REQUIRED.
      "horse_name": str,            // REQUIRED.
      "post_position": int | null,
      "jockey": str | null,
      "trainer": str | null,
      "morning_line": str | null,   // e.g. "5-2", "9/2"
      "current_odds_decimal": float | null,  // if shown as decimal
      "current_odds_american": int | null,   // if shown as American (e.g. +450)
      "current_odds_fractional": str | null, // if shown as fractional, e.g. "9/2"
      "is_scratched": bool          // true if visibly scratched / struck through
    }
  ]
}

Rules:
- Output JSON only. No markdown fences. No commentary.
- If a field is unreadable or absent, return null (not "" or 0).
- For odds: fill whichever ONE format is shown. Don't convert.
- Surface map: Dirt->"D", Turf->"T", All Weather/Synthetic/Tapeta->"E".
- track_id: only fill if you see an explicit code. Don't guess from name.
- Include every runner row, including scratches.
"""


# ---------------------------------------------------------------------------
# Track-name heuristics (only used when vision model returns null track_id)
TRACK_NAME_TO_ID = {
    # USA majors (Equibase codes)
    "aqueduct": "AQU", "belmont": "BEL", "saratoga": "SAR",
    "churchill downs": "CD", "keeneland": "KEE", "ellis park": "ELP",
    "del mar": "DMR", "santa anita": "SA", "los alamitos": "LRC",
    "gulfstream": "GP", "tampa bay": "TAM", "tampa bay downs": "TAM",
    "fair grounds": "FG", "oaklawn": "OP", "lone star": "LS",
    "hawthorne": "HAW", "arlington": "AP",
    "delaware park": "DEL", "laurel": "LRL", "pimlico": "PIM",
    "monmouth": "MTH", "parx": "PRX", "penn national": "PEN",
    "presque isle": "PID", "thistledown": "TDN", "mountaineer": "MNR",
    "mountaineer park": "MNR",
    "remington": "RP", "will rogers": "WRD", "fonner": "FON",
    "louisiana downs": "LAD", "evangeline": "EVD",
    "finger lakes": "FL", "turfway": "TP",
    "woodbine": "WO", "century mile": "CTM", "century downs": "CTD",
    "hastings": "HST",
}


# ---------------------------------------------------------------------------
@dataclass
class ParsedScreenshot:
    """Structured result from a vision parse."""
    track_id: str | None = None
    track_name: str | None = None
    race_date: str | None = None
    race_number: int | None = None
    post_time: str | None = None
    distance_text: str | None = None
    surface: str | None = None
    race_type: str | None = None
    purse_usd: int | None = None
    book_id: str | None = None
    runners: list[dict[str, Any]] = field(default_factory=list)
    raw_response: str = ""

    def race_id(self) -> str:
        if not (self.track_id and self.race_date and self.race_number):
            raise ValueError(
                "Need track_id + race_date + race_number to build race_id. "
                f"Got track_id={self.track_id!r}, race_date={self.race_date!r}, "
                f"race_number={self.race_number!r}."
            )
        return f"{self.track_id}|{self.race_date}|{self.race_number}"


# ---------------------------------------------------------------------------
# Vision call

def _read_image_bytes(image: bytes | str | Path) -> tuple[bytes, str]:
    """Returns (raw_bytes, media_type)."""
    if isinstance(image, (str, Path)):
        p = Path(image)
        raw = p.read_bytes()
        ext = p.suffix.lower().lstrip(".")
    else:
        raw = image
        ext = ""
    media = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "image/jpeg")
    return raw, media


def _strip_json_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"```\s*$", "", s)
    return s.strip()


def _backfill_track_id(parsed: dict[str, Any]) -> dict[str, Any]:
    if parsed.get("track_id"):
        return parsed
    name = (parsed.get("track_name") or "").lower().strip()
    if name in TRACK_NAME_TO_ID:
        parsed["track_id"] = TRACK_NAME_TO_ID[name]
    return parsed


def parse_screenshot(image: bytes | str | Path,
                     api_key: str | None = None,
                     model: str = DEFAULT_MODEL) -> ParsedScreenshot:
    """Send a screenshot to Claude vision, return structured ParsedScreenshot.

    Reads ANTHROPIC_API_KEY from env if api_key not provided.
    """
    if not _HAS_ANTHROPIC:
        raise RuntimeError(
            "anthropic SDK not installed. `pip install anthropic`."
        )
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. "
            "Set it in your shell or pass api_key=..."
        )

    raw, media = _read_image_bytes(image)
    b64 = base64.standard_b64encode(raw).decode("ascii")

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media, "data": b64,
                }},
                {"type": "text", "text": EXTRACTION_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    cleaned = _strip_json_fences(text)
    try:
        parsed_dict = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Vision returned non-JSON: {e}\n---\n{text[:500]}")

    parsed_dict = _backfill_track_id(parsed_dict)

    return ParsedScreenshot(
        track_id=parsed_dict.get("track_id"),
        track_name=parsed_dict.get("track_name"),
        race_date=parsed_dict.get("race_date"),
        race_number=parsed_dict.get("race_number"),
        post_time=parsed_dict.get("post_time"),
        distance_text=parsed_dict.get("distance_text"),
        surface=parsed_dict.get("surface"),
        race_type=parsed_dict.get("race_type"),
        purse_usd=parsed_dict.get("purse_usd"),
        book_id=parsed_dict.get("book_id"),
        runners=parsed_dict.get("runners", []) or [],
        raw_response=text,
    )


# ---------------------------------------------------------------------------
# Distance text -> (distance_id, distance_unit)
# Equibase convention: distance_id stored as "yards" int when unit="Y";
# for furlongs the SIMD encodes distance_id as the integer hundredths-of-mile
# (e.g. 600 = 6 furlongs = 0.75 mile -> stored as 600). We keep that pattern.

_DISTANCE_PAT = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>furlongs?|f|miles?|mi|m|yards?|yds?|y)\b",
    re.IGNORECASE,
)
_FRACTIONAL_MILE = re.compile(
    r"(?P<whole>\d+)?\s*(?P<num>\d+)\s*[/⁄]\s*(?P<den>\d+)\s*(?:miles?|mi)\b",
    re.IGNORECASE,
)


def parse_distance(text: str | None) -> tuple[int | None, str | None, str | None]:
    """Parse '1 Mile' / '6 Furlongs' / '5 1/2 furlongs' -> (dist_id, unit, published).

    Equibase stores distance as integer hundredths-of-mile in the SIMD,
    e.g. 1 mile -> 800, 6f -> 600, 5.5f -> 550, 1 1/16 mi -> 850.
    Unit code: "F" furlongs, "M" miles, "Y" yards.
    """
    if not text:
        return None, None, None
    t = text.strip().lower()

    # Whole/half furlongs explicit (e.g. "5 1/2 furlongs", "5.5f")
    m_frac_f = re.match(
        r"^\s*(?P<whole>\d+)\s+(?P<n>\d+)\s*[/⁄]\s*(?P<d>\d+)\s*(?:furlongs?|f)\s*$", t,
    )
    if m_frac_f:
        whole = float(m_frac_f["whole"])
        frac = float(m_frac_f["n"]) / float(m_frac_f["d"])
        f = whole + frac
        return int(round(f * 100)), "F", text

    # Fractional miles "1 1/16 mile"
    m_frac_mi = re.match(
        r"^\s*(?P<whole>\d+)\s+(?P<n>\d+)\s*[/⁄]\s*(?P<d>\d+)\s*(?:miles?|mi)\s*$", t,
    )
    if m_frac_mi:
        whole = float(m_frac_mi["whole"])
        frac = float(m_frac_mi["n"]) / float(m_frac_mi["d"])
        mi = whole + frac
        return int(round(mi * 800)), "M", text

    m = _DISTANCE_PAT.search(t)
    if not m:
        return None, None, text
    num = float(m["num"])
    unit_raw = m["unit"].lower()
    if unit_raw.startswith("f"):
        return int(round(num * 100)), "F", text
    if unit_raw.startswith("m"):
        return int(round(num * 800)), "M", text
    if unit_raw.startswith("y"):
        return int(round(num)), "Y", text
    return None, None, text


# ---------------------------------------------------------------------------
# Morning-line / odds helpers (mirror odds_math conventions)

def _ml_to_decimal(ml: str | None) -> float | None:
    if not ml:
        return None
    s = ml.strip().replace("⁄", "/").replace("-", "/")
    if "/" not in s:
        try:
            return float(s)
        except ValueError:
            return None
    try:
        n, d = s.split("/", 1)
        return 1.0 + float(n) / float(d)
    except (ValueError, ZeroDivisionError):
        return None


def _american_to_decimal(am: int | None) -> float | None:
    if am is None:
        return None
    a = int(am)
    if a > 0:
        return 1.0 + a / 100.0
    if a < 0:
        return 1.0 + 100.0 / abs(a)
    return None


# ---------------------------------------------------------------------------
# DB inserters

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_synth_id(*parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return h


def ingest_parsed_race(conn: sqlite3.Connection,
                       parsed: ParsedScreenshot,
                       overwrite: bool = False) -> str:
    """Insert tracks/races/horses/entries rows for a parsed screenshot.

    Returns the canonical race_id. If race already exists and overwrite=False,
    raises ValueError. Set overwrite=True to wipe entries+race and re-insert.
    """
    race_id = parsed.race_id()
    cur = conn.cursor()

    existing = cur.execute(
        "SELECT 1 FROM races WHERE race_id = ?", (race_id,)
    ).fetchone()
    if existing and not overwrite:
        raise ValueError(
            f"race_id {race_id!r} already exists. Pass overwrite=True to replace."
        )
    if existing and overwrite:
        cur.execute(
            "DELETE FROM odds_snapshots WHERE race_id = ?", (race_id,)
        )
        cur.execute(
            "DELETE FROM odds_features WHERE race_id = ?", (race_id,)
        )
        cur.execute("DELETE FROM entries WHERE race_id = ?", (race_id,))
        cur.execute("DELETE FROM races WHERE race_id = ?", (race_id,))

    # tracks (idempotent)
    cur.execute(
        "INSERT OR IGNORE INTO tracks (track_id, track_name, country) VALUES (?,?,?)",
        (parsed.track_id, parsed.track_name or parsed.track_id, "USA"),
    )

    # races
    dist_id, dist_unit, dist_pub = parse_distance(parsed.distance_text)
    cur.execute(
        """INSERT INTO races
           (race_id, track_id, race_date, race_number,
            day_evening, breed_type, course_type, surface,
            distance_id, distance_unit, distance_published, about_distance,
            age_restriction, sex_restriction,
            race_type, race_type_desc, race_name, grade,
            purse_usa, min_claim_price, max_claim_price,
            post_time, number_of_runners, conditions_text)
           VALUES (?,?,?,?,  ?,?,?,?,  ?,?,?,?,  ?,?,  ?,?,?,?,  ?,?,?,  ?,?,?)""",
        (
            race_id, parsed.track_id, parsed.race_date, parsed.race_number,
            "D", "TB", "M", parsed.surface,
            dist_id, dist_unit, dist_pub, "N",
            None, None,
            parsed.race_type, parsed.race_type, None, None,
            float(parsed.purse_usd) if parsed.purse_usd else None, None, None,
            parsed.post_time, len(parsed.runners), "[ingested from screenshot]",
        ),
    )

    # horses + entries
    for r in parsed.runners:
        if not r.get("horse_name") or not r.get("program_number"):
            continue
        # Synthetic registration_number: hash of (track, date, name)
        # so re-ingesting the same race is idempotent.
        reg = "SH-" + _hash_synth_id(parsed.track_id or "", parsed.race_date or "",
                                     str(r["horse_name"]))
        cur.execute(
            """INSERT OR IGNORE INTO horses
               (registration_number, horse_name) VALUES (?,?)""",
            (reg, r["horse_name"]),
        )
        entry_id = "E-" + _hash_synth_id(race_id, str(r["program_number"]), reg)
        cur.execute(
            """INSERT INTO entries
               (entry_id, race_id, program_number, post_position,
                horse_reg, weight_carried, coupled_indicator,
                couple_type, equipment_code,
                apprentice_type, apprentice_wt_allow, eligibility_text)
               VALUES (?,?,?,?,  ?,?,?,?,?,  ?,?,?)""",
            (
                entry_id, race_id, str(r["program_number"]), r.get("post_position"),
                reg, None, None,
                None, None,
                None, None, None,
            ),
        )

    conn.commit()
    return race_id


def ingest_parsed_odds(conn: sqlite3.Connection,
                       parsed: ParsedScreenshot,
                       race_id: str | None = None) -> int:
    """Insert one odds snapshot per runner. Returns row count."""
    rid = race_id or parsed.race_id()
    book = (parsed.book_id or "screenshot").lower().strip()
    captured = _now_utc_iso()
    cur = conn.cursor()
    n = 0
    for r in parsed.runners:
        prog = str(r.get("program_number") or "").strip()
        if not prog:
            continue
        # Resolve entry_id
        row = cur.execute(
            "SELECT entry_id FROM entries WHERE race_id = ? AND program_number = ?",
            (rid, prog),
        ).fetchone()
        entry_id = row[0] if row else None

        dec = r.get("current_odds_decimal")
        if dec is None and r.get("current_odds_american") is not None:
            dec = _american_to_decimal(r["current_odds_american"])
        if dec is None and r.get("current_odds_fractional"):
            dec = _ml_to_decimal(r["current_odds_fractional"])
        ml_dec = _ml_to_decimal(r.get("morning_line"))

        # Live odds row (if we have a current price)
        if dec is not None:
            cur.execute(
                """INSERT INTO odds_snapshots
                   (captured_at, book_id, race_id, program_number, entry_id,
                    decimal_odds, american_odds, is_scratched, is_morning_line, raw_payload)
                   VALUES (?,?,?,?,?,  ?,?,?,?,?)""",
                (captured, book, rid, prog, entry_id,
                 float(dec), None, int(bool(r.get("is_scratched"))), 0, None),
            )
            n += 1

        # Morning-line row (if shown)
        if ml_dec is not None:
            cur.execute(
                """INSERT INTO odds_snapshots
                   (captured_at, book_id, race_id, program_number, entry_id,
                    decimal_odds, american_odds, is_scratched, is_morning_line, raw_payload)
                   VALUES (?,?,?,?,?,  ?,?,?,?,?)""",
                (captured, "morningline", rid, prog, entry_id,
                 float(ml_dec), None, int(bool(r.get("is_scratched"))), 1,
                 r.get("morning_line")),
            )
            n += 1
    conn.commit()
    return n


def ingest_screenshot(conn: sqlite3.Connection,
                      image: bytes | str | Path,
                      api_key: str | None = None,
                      overwrite: bool = False,
                      model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """One-shot: parse → insert race → insert odds. Returns summary dict."""
    parsed = parse_screenshot(image, api_key=api_key, model=model)
    race_id = ingest_parsed_race(conn, parsed, overwrite=overwrite)
    n_odds = ingest_parsed_odds(conn, parsed, race_id)
    return {
        "race_id": race_id,
        "track_id": parsed.track_id,
        "race_date": parsed.race_date,
        "race_number": parsed.race_number,
        "n_runners": len(parsed.runners),
        "n_odds_rows": n_odds,
        "no_pp_history": True,
    }
