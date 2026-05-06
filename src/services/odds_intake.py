"""Odds intake service for Operator Console.

Reads a CSV keyed by post_position, resolves entry_ids from the current
race card, and writes to the live_odds table.

CSV columns (required): post_position
CSV odds  (one of):     decimal_odds | american_odds | morning_line
CSV optional:           book_id, horse_name, is_scratched
"""
from __future__ import annotations

import csv
import io
import sqlite3
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
) -> dict:
    """Parse uploaded CSV, write valid rows to live_odds. Returns summary dict."""
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
    """Return best (highest decimal) live odds per post_position for this card."""
    try:
        rows = conn.execute(
            """SELECT post_position, decimal_odds, book_id, captured_at
               FROM live_odds
               WHERE card_id=? AND is_scratched=0 AND is_morning_line=0
               ORDER BY captured_at DESC""",
            (card_id,),
        ).fetchall()
    except Exception:
        return {}

    best: dict[int, dict] = {}
    for r in rows:
        pp = r[0]
        if pp not in best or r[1] > best[pp]["decimal_odds"]:
            best[pp] = {"decimal_odds": r[1], "book_id": r[2], "captured_at": r[3]}
    return best
