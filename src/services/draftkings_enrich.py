"""DraftKings Horse enrichment and canonical ingestion service.

Responsibilities:
1. Append-only staging tables with full provenance:
     dk_staging_documents
     dk_staging_races
     dk_staging_entries
     dk_staging_starts
     dk_staging_workouts
     dk_staging_scratches
     dk_staging_odds
     dk_staging_annotations
2. Authoritative canonical persistence into DerbyEdge standard tables:
     tracks, race_cards, horses, entries, horse_starts, workouts
   - Provisional horse resolution via draftkings:{name}:{sex}:{foal_yr}:{state}
   - Idempotent: re-ingesting the same document creates 0 duplicate canonical rows
   - Stamped with source_document_id and source_row_id
3. Pre-race feature generation:
   - As-of timestamp contract with conservative target-date exclusion:
     record_date < target_race_date strictly enforced (0 leakage from target race)
   - Conditioned and smoothed features:
     days_since_last_start, starts_last_90d, recent_finish_percentile_w,
     surface_distance_start_count, surface_distance_finish_percentile_w,
     class_delta_last_to_today, days_since_last_workout, workout_cadence_30d,
     workout_velocity_z, prior_publicness, historical_scratch_rate
   - Diagnostic career totals retained for monitoring
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import math
import sqlite3
from typing import Any, Optional

import pandas as pd

from src.utils.db import ensure_feature_store_columns
from src.ingest.draftkings_pdf import (
    DraftKingsEntryRecord,
    DraftKingsParsedRace,
    DraftKingsStartRecord,
    DraftKingsWorkoutRecord,
)
from src.utils.horse_norm import horse_key


# ── Staging DDL ────────────────────────────────────────────────────────────────

_STAGING_DDL = """\
CREATE TABLE IF NOT EXISTS dk_staging_documents (
    doc_id                TEXT PRIMARY KEY,
    file_sha256           TEXT NOT NULL UNIQUE,
    filename              TEXT NOT NULL,
    track_code            TEXT NOT NULL,
    race_date             TEXT NOT NULL,
    race_number           INTEGER NOT NULL,
    captured_at           TEXT,
    is_post_race          INTEGER NOT NULL DEFAULT 0,
    production_eligible   INTEGER NOT NULL DEFAULT 0,
    eligibility_reason    TEXT NOT NULL,
    status                TEXT NOT NULL,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dk_staging_races (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                TEXT NOT NULL REFERENCES dk_staging_documents(doc_id),
    track_code            TEXT NOT NULL,
    race_date             TEXT NOT NULL,
    race_number           INTEGER NOT NULL,
    stakes_name           TEXT,
    race_class            TEXT,
    purse                 INTEGER,
    distance_text         TEXT,
    distance_furlongs     REAL,
    surface               TEXT,
    conditions            TEXT,
    field_size_declared   INTEGER,
    parse_confidence      REAL NOT NULL DEFAULT 1.0,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dk_staging_entries (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                TEXT NOT NULL REFERENCES dk_staging_documents(doc_id),
    source_page_number    INTEGER NOT NULL,
    source_row_id         TEXT NOT NULL,
    post_position         INTEGER NOT NULL,
    program_number        INTEGER NOT NULL,
    horse_name            TEXT NOT NULL,
    horse_source_key      TEXT NOT NULL,
    morning_line_raw      TEXT,
    morning_line_decimal  REAL,
    other_odds_raw        TEXT,
    odds_type             TEXT NOT NULL,
    sex                   TEXT,
    age                   INTEGER,
    foaling_year          INTEGER,
    color                 TEXT,
    state_bred            TEXT,
    lasix                 INTEGER NOT NULL DEFAULT 0,
    angles_json           TEXT,
    raw_text              TEXT,
    parse_confidence      REAL NOT NULL DEFAULT 1.0,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dk_staging_starts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                TEXT NOT NULL REFERENCES dk_staging_documents(doc_id),
    source_page_number    INTEGER NOT NULL,
    source_row_id         TEXT NOT NULL,
    horse_name            TEXT NOT NULL,
    horse_source_key      TEXT NOT NULL,
    start_date            TEXT NOT NULL,
    is_target_race        INTEGER NOT NULL DEFAULT 0,
    track_code            TEXT NOT NULL,
    track_name            TEXT,
    race_class            TEXT,
    distance_text         TEXT,
    distance_furlongs     REAL,
    surface               TEXT,
    surface_condition     TEXT,
    program_post          TEXT,
    odds_raw              TEXT,
    finish_position       INTEGER,
    is_scratch            INTEGER NOT NULL DEFAULT 0,
    raw_text              TEXT,
    parse_confidence      REAL NOT NULL DEFAULT 1.0,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dk_staging_workouts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                TEXT NOT NULL REFERENCES dk_staging_documents(doc_id),
    source_page_number    INTEGER NOT NULL,
    source_row_id         TEXT NOT NULL,
    horse_name            TEXT NOT NULL,
    horse_source_key      TEXT NOT NULL,
    workout_date          TEXT NOT NULL,
    is_target_race        INTEGER NOT NULL DEFAULT 0,
    track_code            TEXT NOT NULL,
    track_name            TEXT,
    distance_text         TEXT,
    distance_furlongs     REAL,
    surface               TEXT,
    surface_condition     TEXT,
    time_seconds          REAL,
    time_text             TEXT,
    work_grade            TEXT NOT NULL DEFAULT 'N',
    rank                  INTEGER,
    raw_text              TEXT,
    parse_confidence      REAL NOT NULL DEFAULT 1.0,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dk_staging_scratches (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                TEXT NOT NULL REFERENCES dk_staging_documents(doc_id),
    source_page_number    INTEGER NOT NULL,
    source_row_id         TEXT NOT NULL,
    horse_name            TEXT NOT NULL,
    scratch_type          TEXT NOT NULL,
    scratch_date          TEXT NOT NULL,
    track_code            TEXT,
    race_class            TEXT,
    raw_text              TEXT,
    parse_confidence      REAL NOT NULL DEFAULT 1.0,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dk_staging_odds (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                 TEXT NOT NULL REFERENCES dk_staging_documents(doc_id),
    source_page_number     INTEGER NOT NULL,
    source_row_id          TEXT NOT NULL,
    horse_name             TEXT NOT NULL,
    odds_value_raw         TEXT NOT NULL,
    odds_type              TEXT NOT NULL,
    odds_capture_timestamp TEXT,
    odds_source_label_raw  TEXT NOT NULL,
    is_market_eligible     INTEGER NOT NULL DEFAULT 0,
    raw_text               TEXT,
    parse_confidence       REAL NOT NULL DEFAULT 1.0,
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dk_staging_annotations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                TEXT NOT NULL REFERENCES dk_staging_documents(doc_id),
    source_page_number    INTEGER NOT NULL,
    source_row_id         TEXT NOT NULL,
    horse_name            TEXT NOT NULL,
    angle_name            TEXT NOT NULL,
    angle_category        TEXT NOT NULL,
    raw_text              TEXT,
    parse_confidence      REAL NOT NULL DEFAULT 1.0,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS horse_source_identities (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id              INTEGER NOT NULL REFERENCES horses(horse_id),
    source_key            TEXT NOT NULL UNIQUE,
    provider              TEXT NOT NULL,
    horse_name_raw        TEXT NOT NULL,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def ensure_draftkings_staging_tables(conn: sqlite3.Connection) -> None:
    """Create append-only staging tables and provenance columns if absent."""
    conn.executescript(_STAGING_DDL)
    # Ensure source_document_id and source_row_id exist on canonical horse_starts & workouts
    for tbl in ("horse_starts", "workouts", "entries"):
        cur = conn.execute(f"PRAGMA table_info({tbl})")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "source_document_id" not in existing_cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN source_document_id TEXT")
        if "source_row_id" not in existing_cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN source_row_id TEXT")
    conn.commit()


# ── Canonical Horse Resolution ────────────────────────────────────────────────

def resolve_or_create_horse_from_dk(
    conn: sqlite3.Connection,
    provisional_key: str,
    horse_name: str,
    foaling_year: int | None = None,
    state_bred: str | None = None,
) -> int:
    """Resolve horse_id using composite provisional identity or canonical horses table."""
    row = conn.execute(
        "SELECT horse_id FROM horse_source_identities WHERE source_key = ?",
        (provisional_key,),
    ).fetchone()
    if row:
        return row[0]

    # Check canonical horses table by name (case-insensitive)
    row_h = conn.execute(
        "SELECT horse_id, year_foaled FROM horses WHERE name = ? COLLATE NOCASE",
        (horse_name.strip(),),
    ).fetchone()
    if row_h:
        horse_id = row_h[0]
        # Link source identity
        conn.execute(
            """INSERT OR IGNORE INTO horse_source_identities
               (horse_id, source_key, provider, horse_name_raw)
               VALUES (?, ?, 'draftkings', ?)""",
            (horse_id, provisional_key, horse_name.strip()),
        )
        conn.commit()
        return horse_id

    # Create new horse in canonical table
    country = "USA"
    conn.execute(
        """INSERT INTO horses (name, year_foaled, country_bred)
           VALUES (?, ?, ?)""",
        (horse_name.strip(), foaling_year, country),
    )
    conn.commit()
    new_id = conn.execute(
        "SELECT horse_id FROM horses WHERE name = ? COLLATE NOCASE",
        (horse_name.strip(),),
    ).fetchone()[0]

    conn.execute(
        """INSERT OR IGNORE INTO horse_source_identities
           (horse_id, source_key, provider, horse_name_raw)
           VALUES (?, ?, 'draftkings', ?)""",
        (new_id, provisional_key, horse_name.strip()),
    )
    conn.commit()
    return new_id


# ── Canonical Ingestion ───────────────────────────────────────────────────────

def ingest_draftkings_to_canonical(
    conn: sqlite3.Connection,
    parsed: DraftKingsParsedRace,
) -> tuple[int, bool]:
    """Persist DraftKings staging records and populate authoritative canonical tables.

    Returns (card_id, is_new_document: bool).
    Strictly idempotent: re-ingesting the same file SHA creates 0 duplicate canonical rows.
    """
    ensure_draftkings_staging_tables(conn)
    ensure_feature_store_columns(conn)

    # 1. Check if document already exists
    row_doc = conn.execute(
        "SELECT doc_id FROM dk_staging_documents WHERE file_sha256 = ?",
        (parsed.file_sha256,),
    ).fetchone()
    is_new = row_doc is None

    # 2. Append to staging documents if new
    if is_new:
        conn.execute(
            """INSERT INTO dk_staging_documents
               (doc_id, file_sha256, filename, track_code, race_date, race_number,
                captured_at, is_post_race, production_eligible, eligibility_reason, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                parsed.source_document_id,
                parsed.file_sha256,
                parsed.manifest.get("source_url_or_reference") or "unknown.pdf",
                parsed.header_track_code or parsed.filename_track_code or "SAR",
                parsed.target_race_date.isoformat(),
                parsed.header_race_number or parsed.filename_race_number or 1,
                parsed.captured_at,
                1 if parsed.is_post_race else 0,
                1 if parsed.production_eligible else 0,
                parsed.eligibility_reason,
                parsed.status,
            ),
        )

        # Append staging races
        conn.execute(
            """INSERT INTO dk_staging_races
               (doc_id, track_code, race_date, race_number, stakes_name, race_class,
                purse, distance_text, distance_furlongs, surface, conditions, field_size_declared)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                parsed.source_document_id,
                parsed.header_track_code or parsed.filename_track_code or "SAR",
                parsed.target_race_date.isoformat(),
                parsed.header_race_number or parsed.filename_race_number or 1,
                parsed.stakes_name,
                parsed.race_class,
                parsed.purse,
                parsed.distance_text,
                parsed.distance_furlongs,
                parsed.surface,
                parsed.conditions,
                parsed.field_size_declared,
            ),
        )

        # Append staging entries
        import json
        for e in parsed.entries:
            conn.execute(
                """INSERT INTO dk_staging_entries
                   (doc_id, source_page_number, source_row_id, post_position, program_number,
                    horse_name, horse_source_key, morning_line_raw, morning_line_decimal,
                    other_odds_raw, odds_type, sex, age, foaling_year, color, state_bred,
                    lasix, angles_json, raw_text, parse_confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parsed.source_document_id,
                    e.source_page_number,
                    e.source_row_id,
                    e.post_position,
                    e.program_number,
                    e.horse_name,
                    e.horse_source_key,
                    e.morning_line_raw,
                    e.morning_line_decimal,
                    e.other_odds_raw,
                    e.odds_type,
                    e.sex,
                    e.age,
                    e.foaling_year,
                    e.color,
                    e.state_bred,
                    1 if e.lasix else 0,
                    json.dumps(e.angles),
                    e.raw_text,
                    e.parse_confidence,
                ),
            )

        # Append staging starts
        for s in parsed.starts:
            conn.execute(
                """INSERT INTO dk_staging_starts
                   (doc_id, source_page_number, source_row_id, horse_name, horse_source_key,
                    start_date, is_target_race, track_code, track_name, race_class,
                    distance_text, distance_furlongs, surface, surface_condition,
                    program_post, odds_raw, finish_position, is_scratch, raw_text, parse_confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parsed.source_document_id,
                    s.source_page_number,
                    s.source_row_id,
                    s.horse_name,
                    s.horse_source_key,
                    s.start_date.isoformat(),
                    1 if s.is_target_race else 0,
                    s.track_code,
                    s.track_name,
                    s.race_class,
                    s.distance_text,
                    s.distance_furlongs,
                    s.surface,
                    s.surface_condition,
                    s.program_post,
                    s.odds_raw,
                    s.finish_position,
                    1 if s.is_scratch else 0,
                    s.raw_text,
                    s.parse_confidence,
                ),
            )

        # Append staging workouts
        for w in parsed.workouts:
            conn.execute(
                """INSERT INTO dk_staging_workouts
                   (doc_id, source_page_number, source_row_id, horse_name, horse_source_key,
                    workout_date, is_target_race, track_code, track_name, distance_text,
                    distance_furlongs, surface, surface_condition, time_seconds, time_text,
                    work_grade, rank, raw_text, parse_confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parsed.source_document_id,
                    w.source_page_number,
                    w.source_row_id,
                    w.horse_name,
                    w.horse_source_key,
                    w.workout_date.isoformat(),
                    1 if w.is_target_race else 0,
                    w.track_code,
                    w.track_name,
                    w.distance_text,
                    w.distance_furlongs,
                    w.surface,
                    w.surface_condition,
                    w.time_seconds,
                    w.time_text,
                    w.work_grade,
                    w.rank,
                    w.raw_text,
                    w.parse_confidence,
                ),
            )

        # Append staging scratches
        for scr in parsed.scratches:
            conn.execute(
                """INSERT INTO dk_staging_scratches
                   (doc_id, source_page_number, source_row_id, horse_name, scratch_type,
                    scratch_date, track_code, race_class, raw_text, parse_confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parsed.source_document_id,
                    scr.source_page_number,
                    scr.source_row_id,
                    scr.horse_name,
                    scr.scratch_type,
                    scr.scratch_date.isoformat(),
                    scr.track_code,
                    scr.race_class,
                    scr.raw_text,
                    scr.parse_confidence,
                ),
            )

        # Append staging odds
        for od in parsed.odds_records:
            conn.execute(
                """INSERT INTO dk_staging_odds
                   (doc_id, source_page_number, source_row_id, horse_name, odds_value_raw,
                    odds_type, odds_capture_timestamp, odds_source_label_raw, is_market_eligible,
                    raw_text, parse_confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parsed.source_document_id,
                    od.source_page_number,
                    od.source_row_id,
                    od.horse_name,
                    od.odds_value_raw,
                    od.odds_type,
                    od.odds_capture_timestamp,
                    od.odds_source_label_raw,
                    1 if od.is_market_eligible else 0,
                    od.raw_text,
                    od.parse_confidence,
                ),
            )

        # Append staging annotations
        for an in parsed.annotations:
            conn.execute(
                """INSERT INTO dk_staging_annotations
                   (doc_id, source_page_number, source_row_id, horse_name, angle_name,
                    angle_category, raw_text, parse_confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parsed.source_document_id,
                    an.source_page_number,
                    an.source_row_id,
                    an.horse_name,
                    an.angle_name,
                    an.angle_category,
                    an.raw_text,
                    an.parse_confidence,
                ),
            )
        conn.commit()

    # 3. Canonical Table Persistence (Authoritative One Cross-Source Truth)
    # Track
    track_code = parsed.header_track_code or parsed.filename_track_code or "SAR"
    row_t = conn.execute("SELECT track_id FROM tracks WHERE abbrev = ?", (track_code,)).fetchone()
    if row_t:
        track_id = row_t[0]
    else:
        conn.execute(
            "INSERT OR IGNORE INTO tracks (name, abbrev, country) VALUES (?, ?, 'USA')",
            (parsed.track_name, track_code),
        )
        conn.commit()
        track_id = conn.execute("SELECT track_id FROM tracks WHERE abbrev = ?", (track_code,)).fetchone()[0]

    # Race Card
    race_date_str = parsed.target_race_date.isoformat()
    race_num = parsed.header_race_number or parsed.filename_race_number or 1
    dist_yards = int((parsed.distance_furlongs or 8.5) * 220)

    row_c = conn.execute(
        """SELECT card_id FROM race_cards
           WHERE track_id = ? AND card_date = ? AND race_number = ?""",
        (track_id, race_date_str, race_num),
    ).fetchone()

    if row_c:
        card_id = row_c[0]
    else:
        conn.execute(
            """INSERT INTO race_cards
               (track_id, card_date, race_number, stakes_name, race_class, purse,
                distance_yards, surface, conditions, field_size)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                track_id,
                race_date_str,
                race_num,
                parsed.stakes_name,
                parsed.race_class,
                parsed.purse,
                dist_yards,
                parsed.surface or "turf",
                parsed.conditions,
                parsed.field_size_declared or len(parsed.entries),
            ),
        )
        conn.commit()
        card_id = conn.execute(
            """SELECT card_id FROM race_cards
               WHERE track_id = ? AND card_date = ? AND race_number = ?""",
            (track_id, race_date_str, race_num),
        ).fetchone()[0]

    # Canonical Entries
    horse_id_by_name: dict[str, int] = {}
    entry_id_by_name: dict[str, int] = {}

    for e in parsed.entries:
        hid = resolve_or_create_horse_from_dk(
            conn,
            e.horse_source_key,
            e.horse_name,
            e.foaling_year,
            e.state_bred,
        )
        horse_id_by_name[e.horse_name] = hid

        # Check existing entry for card + horse
        row_e = conn.execute(
            "SELECT entry_id FROM entries WHERE card_id = ? AND horse_id = ?",
            (card_id, hid),
        ).fetchone()

        ml_val = e.morning_line_decimal or 10.0

        if row_e:
            eid = row_e[0]
        else:
            conn.execute(
                """INSERT INTO entries
                   (card_id, horse_id, post_position, morning_line_odds,
                    scratch_flag, source_document_id, source_row_id)
                   VALUES (?, ?, ?, ?, 0, ?, ?)""",
                (card_id, hid, e.post_position, ml_val, parsed.source_document_id, e.source_row_id),
            )
            conn.commit()
            eid = conn.execute(
                "SELECT entry_id FROM entries WHERE card_id = ? AND horse_id = ?",
                (card_id, hid),
            ).fetchone()[0]

        entry_id_by_name[e.horse_name] = eid

    # Pre-race canonical horse_starts and workouts:
    # Strictly where record_date < target_race_date (conservative anti-leakage contract)
    for s in parsed.starts:
        if s.start_date >= parsed.target_race_date or s.is_target_race or s.is_scratch:
            continue
        hid = horse_id_by_name.get(s.horse_name)
        eid = entry_id_by_name.get(s.horse_name)
        if not hid or not eid:
            continue

        # Idempotency check: don't insert duplicate source_row_id
        row_dup = conn.execute(
            "SELECT start_id FROM horse_starts WHERE source_row_id = ?",
            (s.source_row_id,),
        ).fetchone()
        if not row_dup:
            conn.execute(
                """INSERT INTO horse_starts
                   (entry_id, horse_id, card_id, finish_position, lengths_behind,
                    speed_figure, field_size_last, source_document_id, source_row_id)
                   VALUES (?, ?, ?, ?, 0.0, NULL, 10, ?, ?)""",
                (eid, hid, card_id, s.finish_position, parsed.source_document_id, s.source_row_id),
            )

    for w in parsed.workouts:
        if w.workout_date >= parsed.target_race_date or w.is_target_race:
            continue
        hid = horse_id_by_name.get(w.horse_name)
        if not hid:
            continue

        row_dup_w = conn.execute(
            "SELECT workout_id FROM workouts WHERE source_row_id = ?",
            (w.source_row_id,),
        ).fetchone()
        if not row_dup_w:
            conn.execute(
                """INSERT INTO workouts
                   (horse_id, workout_date, distance_furlongs, time_seconds,
                    work_grade, surface, source_document_id, source_row_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    hid,
                    w.workout_date.isoformat(),
                    w.distance_furlongs or 4.0,
                    w.time_seconds or 48.0,
                    w.work_grade[:1] if w.work_grade in ('B', 'F', 'G', 'N') else 'N',
                    w.surface or 'dirt',
                    parsed.source_document_id,
                    w.source_row_id,
                ),
            )

    conn.commit()
    return card_id, is_new


# ── Conditioned & Smoothed Pre-Race Feature Generator ─────────────────────────

_CLASS_RANKS: dict[str, int] = {
    "G1": 100, "STAKES": 85, "AOC": 70, "ALW": 65, "SOC": 60, "STR": 55,
    "CLM40000": 55, "CLM35000": 50, "CLM30000": 48, "CLM20000": 42,
    "CLM": 45, "MSW": 50, "MOC": 45, "MCL50000": 38, "MCL40000": 34,
    "MCL35000": 32, "MCL20000": 28, "MCL": 30,
}


def _class_score(cls_text: str | None) -> int:
    if not cls_text:
        return 45
    c_up = cls_text.upper().replace(" ", "")
    for k, score in _CLASS_RANKS.items():
        if k in c_up:
            return score
    return 45


def generate_dk_pre_race_features(
    conn: sqlite3.Connection,
    card_id: int,
    parsed: DraftKingsParsedRace,
    scoring_as_of: str | None = None,
) -> pd.DataFrame:
    """Generate normalized, smoothed, and conditioned pre-race features.

    Anti-leakage contract:
    - Filters starts and workouts: record_date < target_race_date strictly.
    - Zero records with is_target_race or record_date == target_race_date enter features.
    """
    target_date = parsed.target_race_date
    today_surface = (parsed.surface or "turf").lower()
    today_is_route = (parsed.distance_furlongs or 8.5) >= 8.0
    today_class_score = _class_score(parsed.race_class)

    rows = []

    for entry in parsed.entries:
        # 1. Starts filtering: strictly pre-race
        entry_starts = [
            s for s in parsed.starts
            if s.horse_name == entry.horse_name
            and s.start_date < target_date
            and not s.is_target_race
        ]

        # 2. Workouts filtering: strictly pre-race
        entry_workouts = [
            w for w in parsed.workouts
            if w.horse_name == entry.horse_name
            and w.workout_date < target_date
            and not w.is_target_race
        ]

        # Non-scratch valid starts
        valid_starts = [s for s in entry_starts if not s.is_scratch]
        scratch_count = sum(1 for s in entry_starts if s.is_scratch)
        total_attempts = len(entry_starts)

        # Feature: days_since_last_start
        if valid_starts:
            most_recent_start = max(s.start_date for s in valid_starts)
            days_since_last_start = (target_date - most_recent_start).days
            last_start_record = next(s for s in valid_starts if s.start_date == most_recent_start)
            last_finish = last_start_record.finish_position or 5
            last_class_score = _class_score(last_start_record.race_class)
            class_delta_last_to_today = last_class_score - today_class_score
        else:
            days_since_last_start = None
            last_finish = None
            class_delta_last_to_today = 0

        # Feature: starts_last_90d
        starts_last_90d = sum(
            1 for s in valid_starts
            if 0 <= (target_date - s.start_date).days <= 90
        )

        # Feature: recent_finish_percentile_w
        # Exponential decay w_i = exp(-0.01 * delta_days), finish percentile p_i = (N - f) / (N - 1)
        w_sum = 0.0
        p_sum = 0.0
        for s in valid_starts:
            delta_days = (target_date - s.start_date).days
            w_i = math.exp(-0.01 * max(delta_days, 0))
            f_i = s.finish_position or 5
            n_i = 10  # default typical field size
            p_i = max(0.0, min(1.0, (n_i - f_i) / max(n_i - 1, 1)))
            w_sum += w_i
            p_sum += w_i * p_i

        recent_finish_percentile_w = round(p_sum / w_sum, 4) if w_sum > 0 else 0.50

        # Feature: surface_distance_start_count & surface_distance_finish_percentile_w
        # Empirical Bayes smoothing with pseudo-count M = 2.0 toward prior 0.50
        sd_starts = []
        for s in valid_starts:
            surf_match = today_surface in s.surface.lower()
            dist_f = s.distance_furlongs or 8.0
            route_match = (dist_f >= 8.0) == today_is_route
            if surf_match and route_match:
                sd_starts.append(s)

        surface_distance_start_count = len(sd_starts)
        sd_w_sum = 0.0
        sd_p_sum = 0.0
        for s in sd_starts:
            delta_days = (target_date - s.start_date).days
            w_i = math.exp(-0.01 * max(delta_days, 0))
            f_i = s.finish_position or 5
            n_i = 10
            p_i = max(0.0, min(1.0, (n_i - f_i) / max(n_i - 1, 1)))
            sd_w_sum += w_i
            sd_p_sum += w_i * p_i

        # EB smoothed toward 0.50 with weight M = 2.0
        M = 2.0
        surface_distance_finish_percentile_w = round(
            (sd_p_sum + M * 0.50) / (sd_w_sum + M), 4
        )

        # Workouts features: days_since_last_workout, workout_cadence_30d
        if entry_workouts:
            most_recent_wo = max(w.workout_date for w in entry_workouts)
            days_since_last_workout = (target_date - most_recent_wo).days
        else:
            days_since_last_workout = None

        workout_cadence_30d = sum(
            1 for w in entry_workouts
            if 0 <= (target_date - w.workout_date).days <= 30
        )

        # workout_velocity_z: normalized if peer reference available
        workout_velocity_z = None

        # Feature: prior_publicness (recency-weighted implied probability shrunk toward 0.10)
        pub_w_sum = 0.0
        pub_q_sum = 0.0
        for s in valid_starts:
            if s.odds_raw:
                try:
                    if "/" in s.odds_raw:
                        num, den = s.odds_raw.split("/")
                        dec_odds = float(num) / float(den) + 1.0
                    else:
                        dec_odds = float(s.odds_raw) + 1.0
                    q_i = 1.0 / max(dec_odds, 1.01)
                    delta_days = (target_date - s.start_date).days
                    w_i = math.exp(-0.01 * max(delta_days, 0))
                    pub_w_sum += w_i
                    pub_q_sum += w_i * q_i
                except ValueError:
                    pass

        prior_publicness = round(
            (pub_q_sum + 1.0 * 0.10) / (pub_w_sum + 1.0), 4
        )

        # Feature: historical_scratch_rate
        historical_scratch_rate = (
            round(scratch_count / total_attempts, 4) if total_attempts > 0 else 0.0
        )

        # Diagnostics: raw career totals
        career_starts = len(valid_starts)
        career_wins = sum(1 for s in valid_starts if s.finish_position == 1)
        career_places = sum(1 for s in valid_starts if s.finish_position == 2)
        career_shows = sum(1 for s in valid_starts if s.finish_position == 3)
        career_win_pct = round(career_wins / career_starts, 4) if career_starts > 0 else 0.0
        career_itm_pct = (
            round((career_wins + career_places + career_shows) / career_starts, 4)
            if career_starts > 0 else 0.0
        )

        # ML implied prob (prior only, never wagering market price)
        ml_odds = entry.morning_line_decimal or 10.0
        market_implied_prob = round(1.0 / max(ml_odds, 1.01), 4)

        # Look up canonical entry_id and horse_id
        row_e = conn.execute(
            """SELECT e.entry_id, e.horse_id FROM entries e
               JOIN horses h ON e.horse_id = h.horse_id
               WHERE e.card_id = ? AND h.name = ? COLLATE NOCASE""",
            (card_id, entry.horse_name),
        ).fetchone()

        entry_id = row_e[0] if row_e else None
        horse_id = row_e[1] if row_e else None

        rows.append({
            "card_id": card_id,
            "entry_id": entry_id,
            "horse_id": horse_id,
            "horse_name": entry.horse_name,
            "post_position": entry.post_position,
            "morning_line_odds": ml_odds,
            "market_implied_prob": market_implied_prob,
            # Model features
            "days_since_last_start": days_since_last_start,
            "starts_last_90d": starts_last_90d,
            "recent_finish_percentile_w": recent_finish_percentile_w,
            "surface_distance_start_count": surface_distance_start_count,
            "surface_distance_finish_percentile_w": surface_distance_finish_percentile_w,
            "class_delta_last_to_today": class_delta_last_to_today,
            "days_since_last_workout": days_since_last_workout,
            "workout_cadence_30d": workout_cadence_30d,
            "workout_velocity_z": workout_velocity_z,
            "prior_publicness": prior_publicness,
            "historical_scratch_rate": historical_scratch_rate,
            # Diagnostic totals
            "career_starts": career_starts,
            "career_wins": career_wins,
            "career_places": career_places,
            "career_shows": career_shows,
            "career_win_pct": career_win_pct,
            "career_itm_pct": career_itm_pct,
            # Provenance & anti-leakage audit
            "pre_race_starts_count": len(valid_starts),
            "pre_race_workouts_count": len(entry_workouts),
            "target_race_records_excluded": sum(1 for s in parsed.starts if s.horse_name == entry.horse_name and s.is_target_race),
            "scoring_as_of_timestamp": scoring_as_of or parsed.captured_at,
        })

    feat_df = pd.DataFrame(rows)
    return feat_df
