"""Race card and entry builder.

Creates new races (tracks → race_cards → entries) from structured data.
Called by odds_intake.ingest_new_race_odds_csv() and from the app's
screenshot promotion panel.

All write helpers are idempotent:
  - tracks:     INSERT OR IGNORE on (abbrev)
  - horses:     INSERT OR IGNORE on (name COLLATE NOCASE)
  - race_cards: INSERT OR IGNORE on (track_id, card_date, race_number)
  - entries:    INSERT OR IGNORE on (card_id, post_position)
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

# ── Surface normalization ─────────────────────────────────────────────────────
_SURFACE_MAP: dict[str, str] = {
    "d": "dirt",        "dirt": "dirt",
    "t": "turf",        "turf": "turf",
    "s": "synthetic",   "synthetic": "synthetic",
    "e": "all_weather", "aw": "all_weather", "all_weather": "all_weather",
    "a": "all_weather",
}
DEFAULT_SURFACE        = "dirt"
DEFAULT_DISTANCE_YARDS = 1760   # 8 furlongs / 1 mile
DEFAULT_MORNING_LINE   = 9.0    # 9-1 fallback when no ML found


def norm_surface(raw: str) -> str:
    return _SURFACE_MAP.get((raw or "").strip().lower(), DEFAULT_SURFACE)


# ── Distance parsing ──────────────────────────────────────────────────────────
def parse_distance_yards(s: str | None) -> int:
    """Convert a human distance string to integer yards.

    Handles: "4 1/2F", "4½F", "6f", "6 furlongs", "1 Mile", "1 1/16 Miles",
             "1.25m", "6.5", plain numbers (assumed furlongs if < 100).
    Falls back to DEFAULT_DISTANCE_YARDS (1760 = 1 mile) on parse failure.
    """
    if not s:
        return DEFAULT_DISTANCE_YARDS
    s = s.strip().lower().replace("½", " 1/2")

    # "4 1/2f" / "4 1/2 furlongs" — must precede generic digit+f match
    m = re.match(r"^(\d+)\s+1/2\s*f", s)
    if m:
        return int(round((float(m.group(1)) + 0.5) * 220))

    # "6f" / "6.5f" / "6 furlongs"
    m = re.match(r"^(\d+(?:\.\d+)?)\s*f", s)
    if m:
        return int(round(float(m.group(1)) * 220))

    # "1 mile" / "1.0 miles"
    m = re.match(r"^(\d+(?:\.\d+)?)\s*miles?$", s)
    if m:
        return int(round(float(m.group(1)) * 8 * 220))

    # "1 1/16 miles" / "1 1/16m"
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)\s*m", s)
    if m:
        furlongs = (int(m.group(1)) + int(m.group(2)) / int(m.group(3))) * 8
        return int(round(furlongs * 220))

    try:
        v = float(s)
        if v >= 500:          # looks like yards already
            return int(v)
        if v >= 3:            # treat as furlongs
            return int(round(v * 220))
    except ValueError:
        pass

    return DEFAULT_DISTANCE_YARDS


# ── Morning line parsing ──────────────────────────────────────────────────────
def parse_morning_line(s: str | None) -> float | None:
    """Parse "5-2", "5/2", "3.50" to decimal odds. Returns None on failure."""
    if not s:
        return None
    s = str(s).strip()
    for pat in (r"^(\d+)-(\d+)$", r"^(\d+)/(\d+)$"):
        m = re.match(pat, s)
        if m:
            num, den = int(m.group(1)), int(m.group(2))
            if den == 0:
                return None
            return round(num / den + 1.0, 3)
    try:
        v = float(s)
        return v if v >= 1.0 else None
    except ValueError:
        return None


# ── DB helpers ────────────────────────────────────────────────────────────────
def _get_or_create_track(conn: sqlite3.Connection, abbrev: str) -> int:
    """Return track_id for abbrev, creating a placeholder row if absent."""
    abbrev = abbrev.strip().upper()[:6]     # schema allows short codes
    row = conn.execute(
        "SELECT track_id FROM tracks WHERE abbrev = ?", (abbrev,)
    ).fetchone()
    if row:
        return row[0]
    conn.execute(
        "INSERT OR IGNORE INTO tracks (name, abbrev, country) VALUES (?, ?, 'USA')",
        (abbrev, abbrev),
    )
    conn.commit()
    return conn.execute(
        "SELECT track_id FROM tracks WHERE abbrev = ?", (abbrev,)
    ).fetchone()[0]


def _get_or_create_horse(conn: sqlite3.Connection, name: str) -> int:
    """Return horse_id for name (case-insensitive), creating a row if absent."""
    row = conn.execute(
        "SELECT horse_id FROM horses WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row:
        return row[0]
    conn.execute("INSERT OR IGNORE INTO horses (name) VALUES (?)", (name,))
    conn.commit()
    return conn.execute(
        "SELECT horse_id FROM horses WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()[0]


# ── Race card lookup / creation ───────────────────────────────────────────────
def find_race_card(
    conn: sqlite3.Connection,
    track_code: str,
    race_date: str,
    race_number: int,
) -> int | None:
    """Return card_id if race exists, else None."""
    row = conn.execute(
        """SELECT rc.card_id FROM race_cards rc
           JOIN tracks t ON rc.track_id = t.track_id
           WHERE t.abbrev = ? AND rc.card_date = ? AND rc.race_number = ?""",
        (track_code.strip().upper(), race_date, race_number),
    ).fetchone()
    return row[0] if row else None


def create_race_card(
    conn: sqlite3.Connection,
    track_code: str,
    race_date: str,
    race_number: int,
    *,
    distance_yards: int = DEFAULT_DISTANCE_YARDS,
    surface: str = DEFAULT_SURFACE,
    stakes_name: str | None = None,
    race_class: str | None = None,
    purse: int | None = None,
    conditions: str | None = None,
    field_size: int | None = None,
) -> int:
    """Create a race_cards row and return its card_id.

    Caller must check find_race_card first — this will fail on duplicate key.
    """
    track_id = _get_or_create_track(conn, track_code)
    conn.execute(
        """INSERT OR IGNORE INTO race_cards
           (track_id, card_date, race_number, stakes_name, race_class,
            purse, distance_yards, surface, conditions, field_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (track_id, race_date, race_number, stakes_name or None,
         race_class or None, purse, distance_yards, surface,
         conditions or None, field_size),
    )
    conn.commit()
    return find_race_card(conn, track_code, race_date, race_number)


# ── Entry creation ────────────────────────────────────────────────────────────
def create_entries_from_runners(
    conn: sqlite3.Connection,
    card_id: int,
    runners: list[dict[str, Any]],
    *,
    default_morning_line: float = DEFAULT_MORNING_LINE,
) -> tuple[int, list[str]]:
    """Insert entries for runners on an existing race card.

    Each runner dict may have:
        horse_name          (required)
        post_position       (int; if absent, program_number or 1-based index used)
        morning_line        (str like "5-2")
        morning_line_decimal(float, overrides morning_line string)
        is_scratched       (bool; source-confirmed scratch)

    Uses INSERT OR IGNORE so existing entries are skipped, not overwritten.
    Returns (n_inserted, warnings).
    """
    n_inserted = 0
    warnings: list[str] = []
    used_pp: set[int] = set()

    # Pre-load PPs already on this card so we don't produce duplicates
    existing = {
        r[0] for r in conn.execute(
            "SELECT post_position FROM entries WHERE card_id=?", (card_id,)
        ).fetchall()
    }
    used_pp |= existing

    for idx, r in enumerate(runners, start=1):
        name = (r.get("horse_name") or "").strip()
        if not name:
            warnings.append(f"Runner {idx}: blank horse_name — skipped")
            continue

        # Post position resolution
        pp = r.get("post_position")
        if pp is None:
            raw_pgm = r.get("program_number")
            if raw_pgm is not None:
                try:
                    pp = int(str(raw_pgm).strip())
                except ValueError:
                    pp = None
        if pp is None:
            # Find a free post position
            pp = idx
            while pp in used_pp:
                pp += 1

        try:
            pp = int(pp)
        except (TypeError, ValueError):
            warnings.append(f"'{name}': unparseable post_position — skipped")
            continue

        if pp in used_pp:
            if r.get("is_scratched"):
                # A re-sync may be the first source artifact that records the
                # scratch; preserve that source truth on an existing entry.
                conn.execute(
                    "UPDATE entries SET scratch_flag=1 WHERE card_id=? AND post_position=?",
                    (card_id, pp),
                )
                continue
            warnings.append(f"'{name}': post_position {pp} already occupied — skipped")
            continue

        # Morning line
        ml = r.get("morning_line_decimal")
        if ml and r.get("morning_line_decimal_includes_stake"):
            ml = float(ml) - 1.0
        if not (ml and float(ml) > 0):
            ml = parse_morning_line(r.get("morning_line"))
        if not (ml and float(ml) > 0):
            ml = default_morning_line
            if not r.get("is_scratched"):
                warnings.append(f"'{name}': no morning line — using {default_morning_line:.0f}-1")

        horse_id = _get_or_create_horse(conn, name)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO entries
                   (card_id, horse_id, post_position, morning_line_odds, scratch_flag)
                   VALUES (?, ?, ?, ?, ?)""",
                (card_id, horse_id, pp, float(ml), int(bool(r.get("is_scratched")))),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                n_inserted += 1
                used_pp.add(pp)
        except Exception as exc:
            warnings.append(f"'{name}': entry insert error — {exc}")

    conn.commit()
    return n_inserted, warnings


# ── Combined find-or-create ───────────────────────────────────────────────────
def find_or_create_race(
    conn: sqlite3.Connection,
    track_code: str,
    race_date: str,
    race_number: int,
    runners: list[dict[str, Any]],
    *,
    distance_yards: int = DEFAULT_DISTANCE_YARDS,
    surface: str = DEFAULT_SURFACE,
    stakes_name: str | None = None,
    race_class: str | None = None,
    purse: int | None = None,
    conditions: str | None = None,
    field_size: int | None = None,
) -> tuple[int, bool, int, list[str]]:
    """Find or create race_card + entries for runners.

    Returns (card_id, was_created, n_entries_inserted, warnings).
    """
    card_id = find_race_card(conn, track_code, race_date, race_number)
    was_created = card_id is None
    if was_created:
        card_id = create_race_card(
            conn, track_code, race_date, race_number,
            distance_yards=distance_yards, surface=surface,
            stakes_name=stakes_name, race_class=race_class, purse=purse,
            conditions=conditions, field_size=field_size,
        )
    else:
        # A source-specific re-sync may fill metadata that an earlier generic
        # import could not parse; never overwrite it with nulls.
        conn.execute(
            """UPDATE race_cards SET
                   race_class=COALESCE(?, race_class),
                   purse=COALESCE(?, purse),
                   conditions=COALESCE(?, conditions),
                   field_size=COALESCE(?, field_size)
               WHERE card_id=?""",
            (race_class, purse, conditions, field_size, card_id),
        )
        conn.commit()

    n_entries, warnings = create_entries_from_runners(conn, card_id, runners)
    return card_id, was_created, n_entries, warnings


# ── Screenshot result → race card ─────────────────────────────────────────────
def create_race_from_screenshot_result(
    conn: sqlite3.Connection,
    sr: dict[str, Any],
) -> dict[str, Any]:
    """Create race card + entries from ingest_sportsbook_screenshot() result dict.

    sr must have ok=True and contain:
        track_id | track_name  — Equibase code preferred
        race_date               — ISO YYYY-MM-DD
        race_number             — int
        distance_text           — e.g. "1 Mile" (optional)
        surface                 — "D"/"T"/etc. (optional)
        race_type               — e.g. "Maiden Claiming" (optional)
        purse_usd               — int (optional)
        runners_raw             — raw runner list from ParsedScreenshot

    Returns {card_id, created, n_entries, warnings, error, track_code, race_date, race_number}.
    """
    track_code  = (sr.get("track_id") or "").strip()
    if not track_code:
        raw_name = (sr.get("track_name") or "").strip()
        # abbreviate long names to 4 chars as a fallback
        track_code = raw_name[:4].upper() if raw_name else "UNK"

    race_date   = (sr.get("race_date") or "").strip()
    race_number = sr.get("race_number")

    missing = []
    if not track_code or track_code == "UNK":
        missing.append("track_code")
    if not race_date:
        missing.append("race_date")
    if race_number is None:
        missing.append("race_number")
    if missing:
        return {
            "card_id": None, "created": False, "n_entries": 0,
            "warnings": [], "track_code": track_code,
            "race_date": race_date, "race_number": race_number,
            "error": f"Cannot create race — missing: {', '.join(missing)}",
        }

    dist_yards = parse_distance_yards(sr.get("distance_text"))
    surface    = norm_surface(sr.get("surface") or "")

    # Build runner list from runners_raw (raw ParsedScreenshot dicts)
    runners_raw = sr.get("runners_raw") or []
    runners_for_build = []
    for r in runners_raw:
        if r.get("is_scratched"):
            continue
        runners_for_build.append({
            "horse_name":         (r.get("horse_name") or "").strip(),
            "post_position":      r.get("post_position"),
            "program_number":     r.get("program_number"),
            "morning_line":       r.get("morning_line"),
            "morning_line_decimal": r.get("current_odds_decimal"),
        })

    card_id, created, n_entries, warnings = find_or_create_race(
        conn, track_code, race_date, int(race_number),
        runners_for_build,
        distance_yards=dist_yards,
        surface=surface,
        stakes_name=(sr.get("race_type") or None),
        purse=(sr.get("purse_usd") or None),
    )

    return {
        "card_id":     card_id,
        "created":     created,
        "n_entries":   n_entries,
        "warnings":    warnings,
        "error":       None,
        "track_code":  track_code,
        "race_date":   race_date,
        "race_number": race_number,
    }
