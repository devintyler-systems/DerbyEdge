"""Past-performance CSV/TSV intake service.

Normalizes semi-structured PP export tables (Equibase DRF-style, pasted grids,
or TSV exports) into rows suitable for horse_starts insertion.

Supported column aliases for each logical field:

  horse_name       : horse_name | horse | name | entry
  race_date        : race_date | date | race_dt
  track_code       : track_code | track | trk | track_abbrev
  distance         : distance | dist | dist_f | distance_furlongs
  surface          : surface | surf | trk_surface
  finish_position  : finish_position | finish | fin | pos | position
  jockey           : jockey | jock | rider
  speed_figure     : speed_figure | speed_fig | fig | spd_fig
  beyer_figure     : beyer_figure | beyer | bf | beyer_fig
  lengths_behind   : lengths_behind | len_behind | lb | lengths
  earned_purse     : earned_purse | earned | purse | earnings

Distance handles: "6f", "6", "1m", "1 1/16m", "8.5", mixed formats.
Surface: d/dirt→dirt, t/turf→turf, s/synthetic→synthetic, a/aw→all_weather.
"""
from __future__ import annotations

import csv
import difflib
import io
import re
import sqlite3
from datetime import datetime
from typing import Any

HORSE_NAME_ALIASES  = {"horse_name", "horse", "name", "entry"}
RACE_DATE_ALIASES   = {"race_date", "date", "race_dt"}
TRACK_CODE_ALIASES  = {"track_code", "track", "trk", "track_abbrev"}
DISTANCE_ALIASES    = {"distance", "dist", "dist_f", "distance_furlongs"}
SURFACE_ALIASES     = {"surface", "surf", "trk_surface"}
FINISH_ALIASES      = {"finish_position", "finish", "fin", "pos", "position"}
JOCKEY_ALIASES      = {"jockey", "jock", "rider"}
SPEED_FIG_ALIASES   = {"speed_figure", "speed_fig", "fig", "spd_fig"}
BEYER_ALIASES       = {"beyer_figure", "beyer", "bf", "beyer_fig"}
LENGTHS_ALIASES     = {"lengths_behind", "len_behind", "lb", "lengths"}
EARNED_ALIASES      = {"earned_purse", "earned", "purse", "earnings"}

SURFACE_MAP = {
    "d": "dirt",      "dirt": "dirt",
    "t": "turf",      "turf": "turf",
    "s": "synthetic", "synthetic": "synthetic",
    "a": "all_weather", "aw": "all_weather", "all_weather": "all_weather",
    "inner turf": "turf", "inner": "turf",
}

ParseResult = dict[str, Any]
ParseError  = dict[str, Any]


def _find_col(fieldnames: list[str], aliases: set[str]) -> str | None:
    for col in fieldnames:
        if col.strip().lower() in aliases:
            return col
    return None


def _parse_date(s: str) -> str | None:
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d-%b-%Y",
                "%d-%b-%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _parse_distance(s: str) -> float | None:
    """Parse distance string to furlongs."""
    if not s:
        return None
    s = s.strip().lower().replace(",", "")
    s = re.sub(r"furlongs?.*$", "", s).strip()
    # "1 1/16m" or "1 1/16 miles"
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)\s*m", s)
    if m:
        return round((int(m.group(1)) + int(m.group(2)) / int(m.group(3))) * 8, 2)
    # "1.25m"
    m = re.match(r"^(\d+(?:\.\d+)?)\s*m$", s)
    if m:
        return round(float(m.group(1)) * 8, 2)
    # "1 1/16" (implied furlongs * 8 = miles? No — treat as furlongs of fraction)
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)$", s)
    if m:
        val = int(m.group(1)) + int(m.group(2)) / int(m.group(3))
        # if whole part <= 2, likely miles; else furlongs
        return round(val * 8 if val <= 2 else val, 2)
    # Plain number or "6f"
    m = re.match(r"^(\d+(?:\.\d+)?)f?$", s)
    if m:
        val = float(m.group(1))
        return round(val, 2) if val < 20 else None
    return None


def _parse_surface(s: str) -> str | None:
    return SURFACE_MAP.get((s or "").strip().lower())


def _parse_int(s: str) -> int | None:
    if not s:
        return None
    try:
        return int(str(s).strip().split(".")[0])
    except ValueError:
        return None


def _parse_float(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(str(s).strip())
    except ValueError:
        return None


def _norm_name(s: str) -> str:
    return " ".join(s.strip().split()).title() if s else ""


def parse_pp_csv(raw: bytes | str) -> tuple[list[ParseResult], list[ParseError]]:
    """Parse PP bytes/string into normalized rows + per-row errors.

    Auto-detects TSV vs CSV from header row.
    Returns (parsed_rows, error_rows).
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw

    first = text.split("\n")[0] if text else ""
    sep = "\t" if "," not in first and "\t" in first else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    fnames = reader.fieldnames or []
    if not fnames:
        return [], [{"row": 0, "raw": {}, "reason": "No headers found in file"}]

    c_horse   = _find_col(fnames, HORSE_NAME_ALIASES)
    c_date    = _find_col(fnames, RACE_DATE_ALIASES)
    c_track   = _find_col(fnames, TRACK_CODE_ALIASES)
    c_dist    = _find_col(fnames, DISTANCE_ALIASES)
    c_surf    = _find_col(fnames, SURFACE_ALIASES)
    c_finish  = _find_col(fnames, FINISH_ALIASES)
    c_jockey  = _find_col(fnames, JOCKEY_ALIASES)
    c_spdfig  = _find_col(fnames, SPEED_FIG_ALIASES)
    c_beyer   = _find_col(fnames, BEYER_ALIASES)
    c_lengths = _find_col(fnames, LENGTHS_ALIASES)
    c_earned  = _find_col(fnames, EARNED_ALIASES)

    missing = []
    if not c_horse:
        missing.append("horse_name")
    if not c_date:
        missing.append("race_date")
    if missing:
        return [], [{
            "row": 0, "raw": {},
            "reason": f"Required columns not found: {', '.join(missing)}. "
                      f"Headers detected: {', '.join(fnames)}",
        }]

    parsed: list[ParseResult] = []
    errors: list[ParseError]  = []

    for row_n, row in enumerate(reader, start=2):
        raw_dict = dict(row)
        horse = _norm_name(row.get(c_horse, ""))
        if not horse:
            errors.append({"row": row_n, "raw": raw_dict, "reason": "Blank horse_name"})
            continue

        race_date = _parse_date(row.get(c_date, ""))
        if not race_date:
            errors.append({"row": row_n, "raw": raw_dict,
                           "reason": f"Unparseable date: {row.get(c_date, '')!r}"})
            continue

        parsed.append({
            "horse_name":       horse,
            "race_date":        race_date,
            "track_code":       (row.get(c_track) or "").strip().upper() if c_track else None,
            "distance_furlongs":_parse_distance(row.get(c_dist) or "") if c_dist else None,
            "surface":          _parse_surface(row.get(c_surf) or "") if c_surf else None,
            "finish_position":  _parse_int(row.get(c_finish) or "") if c_finish else None,
            "jockey":           (row.get(c_jockey) or "").strip() if c_jockey else None,
            "speed_figure":     _parse_int(row.get(c_spdfig) or "") if c_spdfig else None,
            "beyer_figure":     _parse_int(row.get(c_beyer) or "") if c_beyer else None,
            "lengths_behind":   _parse_float(row.get(c_lengths) or "") if c_lengths else None,
            "earned_purse":     _parse_int(row.get(c_earned) or "") if c_earned else None,
        })

    return parsed, errors


def preview_pp_match(
    conn: sqlite3.Connection,
    parsed_rows: list[ParseResult],
    active_card_id: int,
    threshold: float = 0.72,
) -> dict:
    """Dry-run match: return matched / unmatched / duplicate breakdown without inserting."""
    db_horses = conn.execute("SELECT horse_id, name FROM horses").fetchall()
    id_map  = {r[1].lower(): r[0] for r in db_horses}
    names   = list(id_map.keys())

    active_horse_ids = {
        r[0] for r in conn.execute(
            "SELECT horse_id FROM entries WHERE card_id=? AND scratch_flag=0",
            (active_card_id,)
        ).fetchall()
    }

    matched, unmatched, duplicates = [], [], []

    for row in parsed_rows:
        h_lower = row["horse_name"].lower()
        horse_id = id_map.get(h_lower)
        score = 1.0
        matched_name = row["horse_name"]

        if horse_id is None:
            candidates = difflib.get_close_matches(h_lower, names, n=1, cutoff=threshold)
            if candidates:
                matched_name = db_horses[names.index(candidates[0])][1]
                horse_id = id_map[candidates[0]]
                score = difflib.SequenceMatcher(None, h_lower, candidates[0]).ratio()
            else:
                unmatched.append({"horse_name": row["horse_name"], "race_date": row["race_date"]})
                continue

        in_card = horse_id in active_horse_ids
        dup = conn.execute(
            """SELECT COUNT(*) FROM horse_starts hs
               JOIN entries e ON hs.entry_id = e.entry_id
               JOIN race_cards rc ON e.card_id = rc.card_id
               WHERE hs.horse_id=? AND rc.card_date=?""",
            (horse_id, row["race_date"]),
        ).fetchone()[0] > 0

        entry = {
            "horse_name":   row["horse_name"],
            "matched_name": matched_name,
            "match_score":  round(score, 3),
            "in_card":      in_card,
            "race_date":    row["race_date"],
            "track_code":   row.get("track_code"),
            "finish":       row.get("finish_position"),
            "speed_fig":    row.get("speed_figure"),
        }
        if dup:
            duplicates.append(entry)
        else:
            matched.append(entry)

    return {"matched": matched, "unmatched": unmatched, "duplicates": duplicates}


def ingest_pp_rows(
    conn: sqlite3.Connection,
    parsed_rows: list[ParseResult],
    active_card_id: int,
    threshold: float = 0.72,
) -> dict:
    """Insert parsed PP rows into horse_starts.

    Matches horse names against horses table (exact then fuzzy).
    Finds entry_id via the active card. Skips duplicates.
    Returns summary: n_inserted, n_unmatched, n_skipped, n_duplicate, warnings.
    """
    db_horses = conn.execute("SELECT horse_id, name FROM horses").fetchall()
    id_map  = {r[1].lower(): r[0] for r in db_horses}
    names   = list(id_map.keys())

    n_inserted = n_unmatched = n_skipped = n_duplicate = 0
    warnings: list[str] = []

    for row in parsed_rows:
        h_lower = row["horse_name"].lower()
        horse_id = id_map.get(h_lower)

        if horse_id is None:
            candidates = difflib.get_close_matches(h_lower, names, n=1, cutoff=threshold)
            if candidates:
                horse_id = id_map[candidates[0]]
                sc = difflib.SequenceMatcher(None, h_lower, candidates[0]).ratio()
                warnings.append(
                    f"Fuzzy '{row['horse_name']}' → '{candidates[0]}' (score {sc:.2f})"
                )
            else:
                n_unmatched += 1
                warnings.append(f"No match for '{row['horse_name']}' — skipped")
                continue

        entry_row = conn.execute(
            "SELECT entry_id FROM entries WHERE card_id=? AND horse_id=?",
            (active_card_id, horse_id),
        ).fetchone()
        if not entry_row:
            n_skipped += 1
            warnings.append(
                f"'{row['horse_name']}' not in active race card (card_id={active_card_id}) — skipped"
            )
            continue
        entry_id = entry_row[0]

        dup = conn.execute(
            """SELECT COUNT(*) FROM horse_starts hs
               JOIN entries e ON hs.entry_id = e.entry_id
               JOIN race_cards rc ON e.card_id = rc.card_id
               WHERE hs.horse_id=? AND rc.card_date=?""",
            (horse_id, row["race_date"]),
        ).fetchone()[0]
        if dup:
            n_duplicate += 1
            continue

        conn.execute(
            """INSERT INTO horse_starts
               (entry_id, horse_id, card_id, finish_position,
                lengths_behind, speed_figure, beyer_figure, earned_purse)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id, horse_id, active_card_id,
                row.get("finish_position"),
                row.get("lengths_behind") or 0.0,
                row.get("speed_figure"),
                row.get("beyer_figure"),
                row.get("earned_purse"),
            ),
        )
        n_inserted += 1

    conn.commit()
    return {
        "n_inserted":  n_inserted,
        "n_unmatched": n_unmatched,
        "n_skipped":   n_skipped,
        "n_duplicate": n_duplicate,
        "warnings":    warnings,
    }
