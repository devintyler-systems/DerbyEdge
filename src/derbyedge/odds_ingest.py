"""Odds ingestion adapters.

Design: each adapter is a callable that takes (race_id, race_date) and returns
a list of normalized OddsRecord dicts ready to insert into odds_snapshots.

Adapters provided:
    - manual_csv:    Paste-in CSV. Always works. Use this for race-day operation.
    - morningline:   Reads MLs already encoded in the SIMD entries (placeholder
                     for now — Equibase free feed doesn't carry ML in the entry
                     spec we ingested; a separate ML feed or manual paste is used).
    - fanduel_http:  HTTP skeleton. Endpoint + headers documented; live-fetch
                     blocked by their geo/anti-bot. Run from Windows box with
                     real session cookies if you want to use it.
    - draftkings_http: Similar.
    - twinspires_http: Pari-mutuel pool feed (Churchill operator).

Run-from-CSV is the canonical race-day path. HTTP adapters are scaffolding.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Callable

from .odds_math import (
    american_to_decimal,
    decimal_to_american,
    morningline_to_decimal,
)


# ---------------------------------------------------------------------------

@dataclass
class OddsRecord:
    captured_at: str             # ISO UTC
    book_id: str
    race_id: str
    program_number: str
    entry_id: str | None
    decimal_odds: float | None
    american_odds: int | None
    is_scratched: int = 0
    is_morning_line: int = 0
    raw_payload: str | None = None


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _resolve_entry_id(conn: sqlite3.Connection, race_id: str,
                      program_number: str) -> str | None:
    cur = conn.execute(
        "SELECT entry_id FROM entries WHERE race_id=? AND program_number=?",
        (race_id, program_number),
    )
    row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Manual CSV adapter — the workhorse

CSV_REQUIRED_COLS = {'book_id', 'race_id', 'program_number'}
CSV_ODDS_COLS = ('decimal_odds', 'american_odds', 'morning_line', 'fractional')


def adapter_manual_csv(csv_path: str | Path,
                       conn: sqlite3.Connection | None = None) -> list[OddsRecord]:
    """Read odds from a CSV file. Most flexible input path.

    Required columns: book_id, race_id, program_number
    Odds columns (any one of):
        decimal_odds       e.g. 5.0
        american_odds      e.g. +400 or -110
        morning_line       e.g. '4-1'
        fractional         e.g. '4-1' or '5/2'
    Optional columns: captured_at (ISO UTC), is_scratched (0/1), is_morning_line (0/1)
    """
    p = Path(csv_path)
    rows: list[OddsRecord] = []
    with p.open(newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        missing = CSV_REQUIRED_COLS - cols
        if missing:
            raise ValueError(f"CSV missing required cols: {missing}")
        if not (cols & set(CSV_ODDS_COLS)):
            raise ValueError(f"CSV must include one of {CSV_ODDS_COLS}")

        captured = _now_utc()
        for r in reader:
            dec = None
            if r.get('decimal_odds'):
                try:
                    dec = float(r['decimal_odds'])
                except ValueError:
                    dec = None
            elif r.get('american_odds'):
                try:
                    dec = american_to_decimal(int(r['american_odds']))
                except ValueError:
                    dec = None
            elif r.get('morning_line'):
                dec = morningline_to_decimal(r['morning_line'])
            elif r.get('fractional'):
                dec = morningline_to_decimal(r['fractional'])

            am = decimal_to_american(dec) if dec is not None else None
            entry_id = None
            if conn is not None:
                entry_id = _resolve_entry_id(conn, r['race_id'], r['program_number'])

            rows.append(OddsRecord(
                captured_at=r.get('captured_at') or captured,
                book_id=r['book_id'].strip().lower(),
                race_id=r['race_id'].strip(),
                program_number=str(r['program_number']).strip(),
                entry_id=entry_id,
                decimal_odds=dec,
                american_odds=am,
                is_scratched=int(r.get('is_scratched') or 0),
                is_morning_line=int(r.get('is_morning_line') or 0),
                raw_payload=None,
            ))
    return rows


# ---------------------------------------------------------------------------
# Record filter — call between adapter_manual_csv and write_snapshots

def filter_records(
    records: list[OddsRecord],
    valid_books: set[str] | None = None,
) -> tuple[list[OddsRecord], list[OddsRecord], list[OddsRecord]]:
    """Partition records into (kept, bad_book, null_odds).

    Rules:
      unknown book_id   → skip: phantom book in devig_proportional group corrupts
                          market_prob for all runners in that race/book.
      null decimal_odds → skip: one null makes devig_proportional return all-NaN
                          for the whole (race_id, book_id) group.
      null entry_id     → keep: falls off the entries-spine merge in
                          build_odds_features; safe to store, may resolve later.

    If valid_books is None the book_id check is skipped entirely.
    """
    kept:      list[OddsRecord] = []
    bad_book:  list[OddsRecord] = []
    null_odds: list[OddsRecord] = []
    for r in records:
        if valid_books is not None and r.book_id not in valid_books:
            bad_book.append(r)
        elif r.decimal_odds is None:
            null_odds.append(r)
        else:
            kept.append(r)
    return kept, bad_book, null_odds


# ---------------------------------------------------------------------------
# HTTP adapter scaffolding — DOES NOT FETCH IN CLOUD ENVIRONMENT.
# Endpoints are stable enough to document but require a real session.

HTTP_ENDPOINT_NOTES = """
FanDuel:
    Race odds appear under their racing app:
        https://sportsbook.fanduel.com/horse-racing
    JSON used by the page (subject to change, run with browser-mimicking headers
    from a US IP):
        https://api.<region>.fanduel.com/...  (path differs by event)
    Easier path: hit the public race card page and parse the embedded JSON
    'INITIAL_STATE' blob. Cookies + 'x-fdr-token' header required.

DraftKings:
    Horse racing lives at https://sportsbook.draftkings.com/leagues/horse-racing/...
    Public endpoint (signed):
        https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/<id>?format=json
    Find the eventgroup id from the page source.

TwinSpires (CDI):
    https://www.twinspires.com/adw/todays-tracks
    Pool data via:
        https://api-twinspires.cdiops.cloud/v1/...
    Auth required. CDI also runs Churchill — odds tend to track tight.

BetMGM, Caesars, etc.: similar shape; each ships a JSON event endpoint behind
their CDN. Headers vary; geofencing applies.
"""


def adapter_fanduel_http(*args, **kwargs):
    raise NotImplementedError(
        "FanDuel live fetch requires authenticated session + US IP. "
        "Run from your Windows box with real cookies, or use adapter_manual_csv. "
        "See HTTP_ENDPOINT_NOTES."
    )


def adapter_draftkings_http(*args, **kwargs):
    raise NotImplementedError(
        "DraftKings live fetch requires US IP + their event-group id discovery. "
        "See HTTP_ENDPOINT_NOTES."
    )


def adapter_twinspires_http(*args, **kwargs):
    raise NotImplementedError(
        "TwinSpires requires authentication. See HTTP_ENDPOINT_NOTES."
    )


# ---------------------------------------------------------------------------
# Loader: write OddsRecords to DB

def write_snapshots(conn: sqlite3.Connection, records: Iterable[OddsRecord]) -> int:
    """Insert snapshots. Returns number of rows written.

    No deduplication — every call writes new rows. Drift is computed by
    comparing snapshots over time.
    """
    cur = conn.cursor()
    n = 0
    for r in records:
        cur.execute(
            """INSERT INTO odds_snapshots
               (captured_at, book_id, race_id, program_number, entry_id,
                decimal_odds, american_odds, is_scratched, is_morning_line, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.captured_at, r.book_id, r.race_id, r.program_number, r.entry_id,
             r.decimal_odds, r.american_odds, r.is_scratched, r.is_morning_line,
             r.raw_payload),
        )
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# CLI helper

def main(argv: list[str] | None = None) -> int:
    """Usage:
        python -m derbyedge.odds_ingest <db_path> <csv_path>
    """
    import sys
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print(main.__doc__)
        return 2
    db_path, csv_path = args
    conn = sqlite3.connect(db_path)
    try:
        recs = adapter_manual_csv(csv_path, conn=conn)
        try:
            cur = conn.execute("SELECT book_id FROM markets")
            valid_books: set[str] | None = {row[0] for row in cur.fetchall()}
        except Exception:
            valid_books = None
        kept, bad_book, null_odds = filter_records(recs, valid_books)
        if bad_book:
            print(
                f"Skipped {len(bad_book)} rows — unknown book_id(s): "
                f"{', '.join(sorted({r.book_id for r in bad_book}))}"
            )
        if null_odds:
            print(f"Skipped {len(null_odds)} rows — no readable odds value")
        n = write_snapshots(conn, kept)
        print(f"Wrote {n} snapshot rows from {csv_path}")
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
