"""
training/import_race_eval_log.py

Idempotent importer for race-level top-pick evaluation CSV files.

Each CSV row records one race: the original and effective top picks, finish
positions, win/loss outcome, winner, chaos flag, and data tier.  Rows are
stored in race_eval_log and matched to race_cards by date + track + race number.
This table feeds operator/reporting queries only — it is NOT used for
starter-level ML training labels.

Usage
-----
    python -m training.import_race_eval_log --csv path/to/38Races.csv
    python -m training.import_race_eval_log --csv path/to/38Races.csv --replace-source
    python -m training.import_race_eval_log --csv path/to/38Races.csv --strict-match
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from src.utils.db import DB_PATH, ensure_race_eval_log, get_connection


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Lowercase, alnum+spaces only, collapse whitespace.

    Strips warning glyphs (⚠ ✓ ✗), apostrophes, and all punctuation.
    Used for fuzzy-safe matching and storage in *_norm columns.
    """
    if not name:
        return ""
    s = str(name).strip().lower()
    s = s.replace("⚠", "").replace("✓", "").replace("✗", "")
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_horse_display(name: str) -> str:
    """Remove warning markers (⚠ ✓ ✗) and trim; preserve punctuation for display."""
    if not name:
        return ""
    for marker in ("⚠", "✓", "✗"):
        name = name.replace(marker, "")
    return name.strip()


# ---------------------------------------------------------------------------
# Field parsers
# ---------------------------------------------------------------------------

def parse_checkmark(val) -> int:
    """Return 1 if value contains ✓, else 0."""
    return 1 if val and "✓" in str(val) else 0


def parse_distance_to_furlongs(dist_text) -> Optional[float]:
    """'7.0f' → 7.0 · '8.5f' → 8.5 · blank → None."""
    if not dist_text or str(dist_text).strip() == "":
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)f$", str(dist_text).strip(), re.I)
    return float(m.group(1)) if m else None


def parse_finish_pos(val) -> tuple[Optional[int], str]:
    """Return (int_position, original_text) or (None, text) for non-integer / SCR."""
    s = str(val).strip() if val is not None else ""
    if s.upper() == "SCR":
        return None, "SCR"
    try:
        return int(s), s
    except (ValueError, TypeError):
        return None, s


def parse_chaos_active(val) -> int:
    """1 if val is a non-blank value not in the falsy set; 0 otherwise."""
    if val is None:
        return 0
    s = str(val).strip().lower()
    if s == "" or s in ("0", "off", "false", "no"):
        return 0
    return 1


# ---------------------------------------------------------------------------
# Race matching
# ---------------------------------------------------------------------------

def find_race_id(
    conn: sqlite3.Connection,
    race_date: str,
    track_code: str,
    race_number: int,
) -> tuple[Optional[int], str, Optional[str]]:
    """Return (race_id, match_status, match_notes).

    Queries race_cards JOIN tracks to resolve the internal card_id.
    match_status: 'MATCHED' | 'UNMATCHED' | 'AMBIGUOUS'
    """
    rows = conn.execute(
        """
        SELECT rc.card_id
        FROM   race_cards rc
        JOIN   tracks t ON t.track_id = rc.track_id
        WHERE  rc.card_date  = ?
          AND  upper(t.abbrev) = upper(?)
          AND  rc.race_number  = ?
        """,
        (race_date, track_code, race_number),
    ).fetchall()

    if len(rows) == 0:
        return None, "UNMATCHED", "no race match"
    if len(rows) == 1:
        return rows[0][0], "MATCHED", None
    return None, "AMBIGUOUS", f"multiple race matches ({len(rows)})"


# ---------------------------------------------------------------------------
# Core import
# ---------------------------------------------------------------------------

def import_eval_csv(
    csv_path: Path,
    conn: sqlite3.Connection,
    strict_match: bool = False,
    replace_source: bool = False,
) -> dict:
    """Read *csv_path* and upsert rows into race_eval_log.

    Returns a summary dict:
        rows_read, inserted_or_updated, matched, unmatched, ambiguous,
        strict_match_failed
    """
    source_file = csv_path.name
    batch_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str).fillna("")
    df.columns = [c.strip() for c in df.columns]

    if replace_source:
        conn.execute(
            "DELETE FROM race_eval_log WHERE source_file = ?", (source_file,)
        )
        conn.commit()

    n_inserted = 0
    n_matched = 0
    n_unmatched = 0
    n_ambiguous = 0
    strict_failed = False

    # Use to_dict("records") so original column names (including "R#", "Eff TP")
    # are preserved — itertuples renames columns with special characters.
    for row_idx, raw in enumerate(df.to_dict("records"), start=1):
        race_date    = str(raw.get("Date", "")).strip()
        track_code   = str(raw.get("Track", "")).strip().upper()
        rn_s         = str(raw.get("R#", "")).strip()
        race_number  = int(rn_s) if rn_s.isdigit() else 0
        surface      = str(raw.get("Srf", "")).strip() or None
        dist_text    = str(raw.get("Dist", "")).strip() or None
        dist_f       = parse_distance_to_furlongs(dist_text)
        field_size_s = str(raw.get("Field", "")).strip()
        field_size   = int(field_size_s) if field_size_s.isdigit() else None

        orig_tp_raw  = str(raw.get("Orig TP", "")).strip() or None
        orig_tp_name = clean_horse_display(orig_tp_raw) if orig_tp_raw else None
        orig_tp_norm = normalize_name(orig_tp_raw) if orig_tp_raw else None
        orig_tp_scr  = parse_checkmark(raw.get("SCR", ""))

        eff_tp_raw   = str(raw.get("Eff TP", "")).strip() or None
        eff_tp_name  = clean_horse_display(eff_tp_raw) if eff_tp_raw else None
        eff_tp_norm  = normalize_name(eff_tp_raw) if eff_tp_raw else None
        tp_fin_raw   = str(raw.get("TP Fin", "")).strip() or None
        eff_tp_fin_pos, eff_tp_fin_text = parse_finish_pos(tp_fin_raw)
        eff_tp_won   = parse_checkmark(raw.get("TP Won", ""))

        winner_raw   = str(raw.get("Winner", "")).strip() or None
        winner_name  = clean_horse_display(winner_raw) if winner_raw else None
        winner_norm  = normalize_name(winner_raw) if winner_raw else None

        chaos_raw    = str(raw.get("Chaos", "")).strip() or None
        chaos_active = parse_chaos_active(chaos_raw)
        tier_name    = str(raw.get("Tier", "")).strip() or None

        # Race matching
        race_id, match_status, match_notes = find_race_id(
            conn, race_date, track_code, race_number
        )

        if match_status == "MATCHED":
            n_matched += 1
        elif match_status == "UNMATCHED":
            n_unmatched += 1
            if strict_match:
                strict_failed = True
        else:
            n_ambiguous += 1
            if strict_match:
                strict_failed = True

        conn.execute(
            """
            INSERT OR REPLACE INTO race_eval_log (
                source_file, source_row_num, import_batch_ts,
                race_id, race_date, track_code, race_number,
                surface, distance_text, distance_f, field_size,
                orig_tp_raw, orig_tp_name, orig_tp_norm, orig_tp_scratched,
                eff_tp_raw, eff_tp_name, eff_tp_norm,
                eff_tp_finish_text, eff_tp_finish_pos, eff_tp_won,
                winner_raw, winner_name, winner_norm,
                chaos_raw, chaos_active, tier_name,
                match_status, match_notes
            ) VALUES (
                ?,?,?,  ?,?,?,?,  ?,?,?,?,
                ?,?,?,?,  ?,?,?,  ?,?,?,
                ?,?,?,  ?,?,?,  ?,?
            )
            """,
            (
                source_file, row_idx, batch_ts,
                race_id, race_date, track_code, race_number,
                surface, dist_text, dist_f, field_size,
                orig_tp_raw, orig_tp_name, orig_tp_norm, orig_tp_scr,
                eff_tp_raw, eff_tp_name, eff_tp_norm,
                eff_tp_fin_text, eff_tp_fin_pos, eff_tp_won,
                winner_raw, winner_name, winner_norm,
                chaos_raw, chaos_active, tier_name,
                match_status, match_notes,
            ),
        )
        n_inserted += 1

    conn.commit()

    return {
        "rows_read":           len(df),
        "inserted_or_updated": n_inserted,
        "matched":             n_matched,
        "unmatched":           n_unmatched,
        "ambiguous":           n_ambiguous,
        "strict_match_failed": strict_failed,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Import a race-level eval CSV into race_eval_log."
    )
    ap.add_argument("--csv", required=True, metavar="PATH",
                    help="Path to the eval CSV file")
    ap.add_argument("--strict-match", action="store_true",
                    help="Exit nonzero if any row cannot be matched to a race")
    ap.add_argument("--replace-source", action="store_true",
                    help="Delete all existing rows for this source_file before import")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"[import_race_eval_log] ERROR: file not found: {csv_path}")
        sys.exit(1)

    conn = get_connection()
    ensure_race_eval_log(conn)

    summary = import_eval_csv(
        csv_path,
        conn,
        strict_match=args.strict_match,
        replace_source=args.replace_source,
    )
    conn.close()

    print("\n=== Race Eval Log Import Summary ===")
    print(f"  Source file        : {csv_path.name}")
    print(f"  Rows read          : {summary['rows_read']}")
    print(f"  Inserted/updated   : {summary['inserted_or_updated']}")
    print(f"  Matched to race_id : {summary['matched']}")
    print(f"  Unmatched          : {summary['unmatched']}")
    print(f"  Ambiguous          : {summary['ambiguous']}")
    print(f"  Strict match failed: {summary['strict_match_failed']}")

    if summary["strict_match_failed"]:
        print("\n[import_race_eval_log] --strict-match: one or more rows unresolved.")
        sys.exit(2)


if __name__ == "__main__":
    main()
