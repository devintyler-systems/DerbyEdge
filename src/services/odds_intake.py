"""Odds intake service for Operator Console.

Two modes:

1. Current-race mode (existing behavior)
   Required: post_position
   Odds:     decimal_odds | american_odds | morning_line
   Optional: book_id, horse_name, is_scratched

2. New-race mode  (detected automatically when race identity columns present)
   Race key: track_code + race_date + race_number
   Required: post_position, horse_name, one odds column
   Optional: book_id, is_scratched, distance, surface, stakes_name
   Creates track / race_card / entries if they don't already exist.

Use has_race_identity(fieldnames) to decide which mode to use.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from src.derbyedge.odds_math import (
    american_to_decimal,
    decimal_to_american,
    morningline_to_decimal,
)

VALID_BOOKS = {
    "fanduel", "draftkings", "twinspires", "churchill",
    "betmgm", "betonline", "caesars", "manual", "morningline",
}

TEMPLATE_COLS = [
    "book_id", "post_position", "horse_name",
    "decimal_odds", "american_odds", "morning_line", "is_scratched",
]


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_template(conn: sqlite3.Connection, card_id: int) -> bytes:
    """Return a blank odds CSV pre-filled with horses from the current card."""
    rows = conn.execute(
        """SELECT e.post_position, h.name AS horse_name, e.morning_line_odds
           FROM entries e
           JOIN horses h ON e.horse_id = h.horse_id
           WHERE e.card_id = ? AND e.scratch_flag = 0
           ORDER BY e.post_position""",
        (card_id,),
    ).fetchall()

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=TEMPLATE_COLS)
    writer.writeheader()
    for r in rows:
        ml = float(r["morning_line_odds"]) if r["morning_line_odds"] else 0
        whole = int(ml)
        ml_str = f"{whole}-1" if ml > 0 else ""
        writer.writerow({
            "book_id": "fanduel",
            "post_position": r["post_position"],
            "horse_name": r["horse_name"],
            "decimal_odds": "",
            "american_odds": "",
            "morning_line": ml_str,
            "is_scratched": 0,
        })
    return buf.getvalue().encode("utf-8")


def _resolve_entry(
    conn: sqlite3.Connection, card_id: int, post_position: int
) -> int | None:
    row = conn.execute(
        "SELECT entry_id FROM entries WHERE card_id=? AND post_position=?",
        (card_id, post_position),
    ).fetchone()
    return row[0] if row else None


def ingest_odds_csv(
    csv_bytes: bytes,
    conn: sqlite3.Connection,
    card_id: int,
    replace: bool = False,
) -> dict:
    """Parse uploaded CSV, write valid rows to live_odds. Returns summary dict.

    replace=True: delete all existing live_odds for card_id before inserting,
    giving a clean current-state snapshot. replace=False (default): append as
    a new historical snapshot batch for drift tracking.
    """
    f = io.StringIO(csv_bytes.decode("utf-8", errors="replace"))
    reader = csv.DictReader(f)
    cols = set(reader.fieldnames or [])

    if "post_position" not in cols:
        raise ValueError("CSV must include a 'post_position' column.")
    if not (cols & {"decimal_odds", "american_odds", "morning_line"}):
        raise ValueError(
            "CSV must include at least one odds column: "
            "decimal_odds, american_odds, or morning_line."
        )

    captured = _now_utc()
    kept, skip_book, skip_odds, skip_entry = [], [], [], []

    for r in reader:
        book = (r.get("book_id") or "manual").strip().lower()
        if book not in VALID_BOOKS:
            skip_book.append({"post": r.get("post_position"), "book": book})
            continue

        dec: float | None = None
        if r.get("decimal_odds"):
            try:
                dec = float(r["decimal_odds"])
            except ValueError:
                pass
        if dec is None and r.get("american_odds"):
            try:
                dec = american_to_decimal(int(r["american_odds"]))
            except (ValueError, TypeError):
                pass
        if dec is None and r.get("morning_line"):
            dec = morningline_to_decimal(r["morning_line"])

        if dec is None:
            skip_odds.append(r.get("post_position"))
            continue

        try:
            pp = int(r["post_position"])
        except (ValueError, TypeError):
            skip_entry.append(r.get("post_position"))
            continue

        entry_id = _resolve_entry(conn, card_id, pp)
        if entry_id is None:
            skip_entry.append(pp)
            continue

        kept.append({
            "captured_at": captured,
            "book_id": book,
            "card_id": card_id,
            "entry_id": entry_id,
            "post_position": pp,
            "decimal_odds": dec,
            "american_odds": decimal_to_american(dec),
            "is_scratched": int(r.get("is_scratched") or 0),
            "is_morning_line": 0,
        })

    if replace:
        conn.execute("DELETE FROM live_odds WHERE card_id=?", (card_id,))

    cur = conn.cursor()
    for rw in kept:
        cur.execute(
            """INSERT INTO live_odds
               (captured_at, book_id, card_id, entry_id, post_position,
                decimal_odds, american_odds, is_scratched, is_morning_line)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rw["captured_at"], rw["book_id"], rw["card_id"],
                rw["entry_id"], rw["post_position"],
                rw["decimal_odds"], rw["american_odds"],
                rw["is_scratched"], rw["is_morning_line"],
            ),
        )
    conn.commit()

    return {
        "n_inserted": len(kept),
        "skip_book": skip_book,
        "skip_odds": skip_odds,
        "skip_entry": skip_entry,
    }


def load_live_odds_by_pp(conn: sqlite3.Connection, card_id: int) -> dict[int, dict]:
    """Return odds from the latest snapshot batch only (keyed by post_position).

    A "snapshot" is all rows sharing the same captured_at from one upload.
    Falls back gracefully if is_morning_line / is_scratched columns don't exist
    in older schema versions so the board never silently loses live-odds coverage.
    """
    # Find latest snapshot timestamp — try progressively simpler filters
    latest_ts = None
    for _q in [
        "SELECT MAX(captured_at) FROM live_odds WHERE card_id=? AND is_scratched=0 AND is_morning_line=0",
        "SELECT MAX(captured_at) FROM live_odds WHERE card_id=? AND is_scratched=0",
        "SELECT MAX(captured_at) FROM live_odds WHERE card_id=?",
    ]:
        try:
            row = conn.execute(_q, (card_id,)).fetchone()
            if row and row[0]:
                latest_ts = row[0]
            break
        except Exception:
            continue

    if not latest_ts:
        return {}

    # Fetch that batch — try progressively simpler WHERE clauses
    rows = []
    for _q in [
        ("SELECT post_position, decimal_odds, book_id, captured_at FROM live_odds "
         "WHERE card_id=? AND is_scratched=0 AND is_morning_line=0 AND captured_at=?"),
        ("SELECT post_position, decimal_odds, book_id, captured_at FROM live_odds "
         "WHERE card_id=? AND is_scratched=0 AND captured_at=?"),
        ("SELECT post_position, decimal_odds, book_id, captured_at FROM live_odds "
         "WHERE card_id=? AND captured_at=?"),
    ]:
        try:
            rows = conn.execute(_q, (card_id, latest_ts)).fetchall()
            break
        except Exception:
            continue

    result: dict[int, dict] = {}
    for r in rows:
        pp = r[0]
        if pp not in result:
            result[pp] = {"decimal_odds": r[1], "book_id": r[2], "captured_at": r[3]}
    return result


def load_latest_snapshot_meta(conn: sqlite3.Connection, card_id: int) -> dict:
    """Summary stats for the snapshot log: total rows, distinct batches, latest ts.

    Falls back to simpler queries if is_morning_line column doesn't exist.
    """
    _empty = {"n_snapshots": 0, "latest_ts": None, "latest_rows": 0, "total_rows": 0}
    for _q in [
        "SELECT COUNT(*),COUNT(DISTINCT captured_at),MAX(captured_at) FROM live_odds WHERE card_id=? AND is_morning_line=0",
        "SELECT COUNT(*),COUNT(DISTINCT captured_at),MAX(captured_at) FROM live_odds WHERE card_id=?",
    ]:
        try:
            row = conn.execute(_q, (card_id,)).fetchone()
            if row and row[2]:
                latest_count = conn.execute(
                    "SELECT COUNT(*) FROM live_odds WHERE card_id=? AND captured_at=?",
                    (card_id, row[2]),
                ).fetchone()[0]
                return {
                    "n_snapshots": int(row[1]),
                    "latest_ts":   row[2],
                    "latest_rows": latest_count,
                    "total_rows":  int(row[0]),
                }
            return _empty
        except Exception:
            continue
    return _empty


def delete_odds_for_race(conn: sqlite3.Connection, card_id: int) -> int:
    """Delete ALL live_odds rows for a race. Scoped to card_id; never deletes other races.

    Returns count of deleted rows.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM live_odds WHERE card_id=?", (card_id,))
    conn.commit()
    return cur.rowcount


# ── New-race mode ─────────────────────────────────────────────────────────────

# Column alias sets for race identity detection
_TRACK_ALIASES   = {"track_code", "track", "trk", "track_abbrev"}
_DATE_ALIASES    = {"race_date", "date", "race_dt"}
_RACENUM_ALIASES = {"race_number", "race_num", "race", "rn"}

# Optional race-meta columns in a new-race CSV
_DIST_ALIASES    = {"distance", "dist", "distance_furlongs"}
_SURF_ALIASES    = {"surface", "surf"}
_SNAME_ALIASES   = {"stakes_name", "race_name"}


def has_race_identity(fieldnames: list[str]) -> bool:
    """Return True if fieldnames contain all three race identity column groups.

    Checks for any alias in each group:
        track_code | track | trk | track_abbrev
        race_date  | date  | race_dt
        race_number| race_num | race | rn

    Call this before deciding which ingest path to use.
    """
    cols = {c.strip().lower() for c in fieldnames}
    return bool(
        cols & _TRACK_ALIASES and
        cols & _DATE_ALIASES and
        cols & _RACENUM_ALIASES
    )


def _find_alias(fieldnames: list[str], aliases: set[str]) -> str | None:
    for c in fieldnames:
        if c.strip().lower() in aliases:
            return c
    return None


def _parse_date_str(s: str) -> str | None:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d-%b-%Y",
                "%d-%b-%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _insert_odds_rows(conn: sqlite3.Connection, kept: list[dict]) -> None:
    cur = conn.cursor()
    for rw in kept:
        cur.execute(
            """INSERT INTO live_odds
               (captured_at, book_id, card_id, entry_id, post_position,
                decimal_odds, american_odds, is_scratched, is_morning_line)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rw["captured_at"], rw["book_id"], rw["card_id"],
                rw["entry_id"], rw["post_position"],
                rw["decimal_odds"], rw["american_odds"],
                rw["is_scratched"], rw["is_morning_line"],
            ),
        )
    conn.commit()


def ingest_new_race_odds_csv(
    csv_bytes: bytes,
    conn: sqlite3.Connection,
) -> dict:
    """New-race mode: parse CSV with race identity columns, create races if needed,
    insert live_odds snapshots.

    The CSV must include: track_code, race_date, race_number, post_position,
    horse_name, and at least one odds column.

    A single upload may contain rows for multiple races; each unique
    (track_code, race_date, race_number) triple is handled independently.

    Returns:
        {
          "races": [
            {
              "card_id": int,
              "created": bool,          # True if race card was just created
              "track_code": str,
              "race_date": str,
              "race_number": int,
              "n_entries": int,         # entries inserted (0 if race existed)
              "n_inserted": int,        # odds rows inserted
              "skip_book": list,
              "skip_odds": list,
              "skip_entry": list,
              "warnings": list[str],
            }
          ],
          "total_inserted": int,
          "total_races": int,
          "errors": list[str],
        }
    """
    from src.services.race_card_builder import (
        find_or_create_race,
        parse_distance_yards,
        norm_surface,
    )

    text = csv_bytes.decode("utf-8", errors="replace")
    first = text.split("\n")[0] if text else ""
    sep = "\t" if "," not in first and "\t" in first else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    fnames = reader.fieldnames or []

    # Locate columns
    c_track  = _find_alias(fnames, _TRACK_ALIASES)
    c_date   = _find_alias(fnames, _DATE_ALIASES)
    c_rnum   = _find_alias(fnames, _RACENUM_ALIASES)
    c_dist   = _find_alias(fnames, _DIST_ALIASES)
    c_surf   = _find_alias(fnames, _SURF_ALIASES)
    c_sname  = _find_alias(fnames, _SNAME_ALIASES)
    cols     = set(fnames)

    errors: list[str] = []
    if not (c_track and c_date and c_rnum):
        return {"races": [], "total_inserted": 0, "total_races": 0,
                "errors": ["Missing required identity columns: track_code, race_date, race_number"]}
    if "post_position" not in cols:
        return {"races": [], "total_inserted": 0, "total_races": 0,
                "errors": ["CSV must include post_position column"]}
    if not (cols & {"decimal_odds", "american_odds", "morning_line"}):
        return {"races": [], "total_inserted": 0, "total_races": 0,
                "errors": ["CSV must include at least one odds column: "
                           "decimal_odds, american_odds, or morning_line"]}

    # Group rows by race key
    all_rows = list(reader)
    race_groups: dict[tuple, list] = defaultdict(list)
    for r in all_rows:
        key = (
            (r.get(c_track) or "").strip().upper(),
            (r.get(c_date)  or "").strip(),
            (r.get(c_rnum)  or "").strip(),
        )
        race_groups[key].append(r)

    captured = _now_utc()
    results: list[dict] = []
    total_inserted = 0

    for (raw_track, raw_date, raw_rnum), rows in race_groups.items():
        if not raw_track:
            errors.append("Skipped group: blank track_code")
            continue
        if not raw_date:
            errors.append(f"Skipped {raw_track}: blank race_date")
            continue

        race_date = _parse_date_str(raw_date)
        if not race_date:
            errors.append(f"Skipped {raw_track}: unparseable date {raw_date!r}")
            continue

        try:
            race_num = int(raw_rnum)
        except (ValueError, TypeError):
            errors.append(f"Skipped {raw_track}: unparseable race_number {raw_rnum!r}")
            continue

        # Race metadata from the first row
        first_row = rows[0]
        dist_text  = (first_row.get(c_dist)  or "") if c_dist else ""
        surf_text  = (first_row.get(c_surf)  or "") if c_surf else ""
        stakes     = (first_row.get(c_sname) or "").strip() if c_sname else None

        # Build runner list for race/entry creation
        runners_for_build = []
        for r in rows:
            name = (r.get("horse_name") or "").strip()
            pp_raw = (r.get("post_position") or "").strip()
            if name:
                runners_for_build.append({
                    "horse_name":   name,
                    "post_position": int(pp_raw) if pp_raw.isdigit() else None,
                    "morning_line":  r.get("morning_line", ""),
                })

        card_id, created, n_entries, build_warnings = find_or_create_race(
            conn, raw_track, race_date, race_num,
            runners_for_build,
            distance_yards=parse_distance_yards(dist_text),
            surface=norm_surface(surf_text),
            stakes_name=stakes or None,
        )

        # Build odds rows for this race
        kept, skip_book, skip_odds, skip_entry = [], [], [], []
        for r in rows:
            book = (r.get("book_id") or "manual").strip().lower()
            if book not in VALID_BOOKS:
                skip_book.append({"post": r.get("post_position"), "book": book})
                continue

            dec: float | None = None
            if r.get("decimal_odds"):
                try:
                    dec = float(r["decimal_odds"])
                except ValueError:
                    pass
            if dec is None and r.get("american_odds"):
                try:
                    dec = american_to_decimal(int(r["american_odds"]))
                except (ValueError, TypeError):
                    pass
            if dec is None and r.get("morning_line"):
                dec = morningline_to_decimal(r["morning_line"])

            if dec is None:
                skip_odds.append(r.get("post_position"))
                continue

            try:
                pp = int(r["post_position"])
            except (ValueError, TypeError):
                skip_entry.append(r.get("post_position"))
                continue

            entry_id = _resolve_entry(conn, card_id, pp)
            if entry_id is None:
                skip_entry.append(pp)
                continue

            kept.append({
                "captured_at": captured,
                "book_id":     book,
                "card_id":     card_id,
                "entry_id":    entry_id,
                "post_position": pp,
                "decimal_odds":  dec,
                "american_odds": decimal_to_american(dec),
                "is_scratched":  int(r.get("is_scratched") or 0),
                "is_morning_line": 0,
            })

        _insert_odds_rows(conn, kept)
        n_ins = len(kept)
        total_inserted += n_ins

        results.append({
            "card_id":    card_id,
            "created":    created,
            "track_code": raw_track,
            "race_date":  race_date,
            "race_number":race_num,
            "n_entries":  n_entries,
            "n_inserted": n_ins,
            "skip_book":  skip_book,
            "skip_odds":  skip_odds,
            "skip_entry": skip_entry,
            "warnings":   build_warnings,
        })

    return {
        "races":          results,
        "total_inserted": total_inserted,
        "total_races":    len(results),
        "errors":         errors,
    }
