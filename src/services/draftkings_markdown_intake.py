"""Canonical persistence for validated DraftKings Markdown raw-card imports.

The Markdown parser owns parsing and fail-closed validation.  This module owns
only the subsequent canonical upsert into the repository's existing race-card,
entry, horse-start, workout, and people tables.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.derbyedge.tracks import resolve_track
from src.ingest.draftkings_markdown import (
    DraftKingsMarkdownCard,
    ValidationResult,
    parse_draftkings_markdown,
    validate_draftkings_markdown_card,
)
from src.services.race_card_builder import find_race_card, parse_morning_line
from src.utils.distance_parser import parse_furlongs


_PROVENANCE_DDL = """
CREATE TABLE IF NOT EXISTS dk_markdown_imports (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_sha256 TEXT NOT NULL UNIQUE,
    source_filename TEXT NOT NULL,
    source_path TEXT,
    source_format TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    scoring_as_of TEXT NOT NULL,
    card_id INTEGER REFERENCES race_cards(card_id),
    parsed_runner_count INTEGER NOT NULL,
    past_performance_count INTEGER NOT NULL,
    workout_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dk_markdown_import_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    source_path TEXT,
    source_format TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    scoring_as_of TEXT NOT NULL,
    card_id INTEGER REFERENCES race_cards(card_id),
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(file_sha256, parser_version)
);

CREATE TABLE IF NOT EXISTS dk_horse_profile_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id INTEGER NOT NULL REFERENCES horses(horse_id),
    card_id INTEGER NOT NULL REFERENCES race_cards(card_id),
    file_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    source_as_of TEXT NOT NULL,
    owner_name TEXT,
    breeder_name TEXT,
    age INTEGER,
    sex_raw TEXT,
    color TEXT,
    sire TEXT,
    dam TEXT,
    dam_sire TEXT,
    raw_profile TEXT,
    record_splits_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(horse_id, card_id, file_sha256, parser_version)
);
"""


@dataclasses.dataclass(frozen=True)
class MarkdownPersistenceResult:
    card_id: int
    already_imported: bool
    repaired_existing_card: bool
    materially_different_from_existing: bool
    persisted_runner_count: int
    persisted_past_performance_count: int
    persisted_workout_count: int
    import_id: int


@dataclasses.dataclass(frozen=True)
class ScoreReadinessBlocker:
    code: str
    message: str


@dataclasses.dataclass(frozen=True)
class MarkdownCardScoreReadiness:
    score_eligible: bool
    blockers: tuple[ScoreReadinessBlocker, ...]
    validated_runner_count: int
    persisted_runner_count: int
    active_runner_count: int


def ensure_draftkings_markdown_intake_tables(conn: sqlite3.Connection) -> None:
    """Install additive provenance/entry fields for the Markdown intake path."""
    conn.executescript(_PROVENANCE_DDL)
    import_columns = {row[1] for row in conn.execute("PRAGMA table_info(dk_markdown_imports)").fetchall()}
    if "source_artifact_id" not in import_columns:
        has_artifacts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_artifacts'").fetchone()
        definition = "INTEGER REFERENCES source_artifacts(artifact_id)" if has_artifacts else "INTEGER"
        conn.execute(f"ALTER TABLE dk_markdown_imports ADD COLUMN source_artifact_id {definition}")
    entry_columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)").fetchall()}
    if "program_number" not in entry_columns:
        conn.execute("ALTER TABLE entries ADD COLUMN program_number TEXT")
    if "medication_weight_equipment" not in entry_columns:
        conn.execute("ALTER TABLE entries ADD COLUMN medication_weight_equipment TEXT")
    horse_columns = {row[1] for row in conn.execute("PRAGMA table_info(horses)").fetchall()}
    if "dam_sire" not in horse_columns:
        conn.execute("ALTER TABLE horses ADD COLUMN dam_sire TEXT")
    for table, additions in {
        "horse_starts": {
            "surface_condition_raw": "TEXT", "program_or_post": "TEXT",
            "historical_jockey": "TEXT", "trip_comment": "TEXT",
        },
        "workouts": {
            "surface_condition_raw": "TEXT", "raw_time": "TEXT",
            "workout_designation": "TEXT", "rank_denominator": "INTEGER",
        },
    }.items():
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, kind in additions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()


def _filename_declared_as_of(source_filename: str) -> str | None:
    """Return only a filename-derived date; it is never pre-race proof."""
    match = re.search(r"_R\d+_(\d{1,2}-\d{1,2}-(?:\d{2}|\d{4}))", Path(source_filename).name, re.I)
    if not match:
        return None
    for fmt in ("%m-%d-%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(match.group(1), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _persist_dk_source_artifact(conn: sqlite3.Connection, card: DraftKingsMarkdownCard, *, source_filename: str, source_path: str | None, card_id: int) -> int | None:
    """Append the exact raw DK document as an unproven source artifact."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_artifacts'").fetchone() is None:
        # Older isolated schemas can retain canonical DK persistence but cannot
        # claim an artifact that their schema does not provide.
        return None
    raw_bytes = bytes(card.raw_bytes or b"")
    if not raw_bytes or hashlib.sha256(raw_bytes).hexdigest() != card.source_sha256:
        raise ValueError("DK raw bytes are unavailable or do not match the parsed source SHA-256")
    declared = _filename_declared_as_of(source_filename)
    conn.execute(
        """INSERT INTO source_artifacts (source_provider,source_filename,source_path,raw_bytes,sha256,parser_version,
           ingestion_timestamp,declared_as_of_timestamp,as_of_status,validation_status,validation_errors_json,
           normalized_record_count,normalized_records_json,card_id)
           VALUES ('draftkings_markdown',?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_provider,sha256,parser_version,card_id) DO NOTHING""",
        (source_filename, source_path, raw_bytes, card.source_sha256, card.parser_version, datetime.now(timezone.utc).isoformat(),
         declared, "UNPROVEN", "UNPROVEN", json.dumps({"as_of_provenance": "FILENAME_DERIVED" if declared else "UNAVAILABLE"}, sort_keys=True),
         len(card.entries), "[]", card_id),
    )
    row = conn.execute("SELECT artifact_id FROM source_artifacts WHERE source_provider='draftkings_markdown' AND sha256=? AND parser_version=? AND card_id=?", (card.source_sha256, card.parser_version, card_id)).fetchone()
    if row is None:
        raise RuntimeError("DK source artifact was not persisted")
    artifact_id = int(row[0])
    conn.execute("UPDATE dk_markdown_imports SET source_artifact_id=? WHERE file_sha256=?", (artifact_id, card.source_sha256))
    return artifact_id


def _track_id(conn: sqlite3.Connection, track_code: str, track_name: str) -> int:
    row = conn.execute("SELECT track_id FROM tracks WHERE abbrev=?", (track_code,)).fetchone()
    if row:
        return int(row[0])
    conn.execute(
        "INSERT INTO tracks (name, abbrev, country) VALUES (?, ?, 'USA')",
        (track_name or track_code, track_code),
    )
    return int(conn.execute("SELECT track_id FROM tracks WHERE abbrev=?", (track_code,)).fetchone()[0])


def _person_id(conn: sqlite3.Connection, name: str | None, role: str) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    conn.execute("INSERT OR IGNORE INTO people (full_name, role) VALUES (?, ?)", (name, role))
    row = conn.execute("SELECT person_id FROM people WHERE full_name=? AND role=?", (name, role)).fetchone()
    return int(row[0]) if row else None


def _sex_code(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return {
        "colt": "C", "filly": "F", "horse": "H", "gelding": "G", "mare": "M", "ridgling": "R",
    }.get(normalized)


def _horse_id(conn: sqlite3.Connection, entry: Any) -> int:
    name = (entry.horse_name or "").strip()
    profile = entry.horse_profile
    conn.execute("INSERT OR IGNORE INTO horses (name) VALUES (?)", (name,))
    row = conn.execute("SELECT horse_id FROM horses WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    horse_id = int(row[0])
    # The validated raw source may fill absent profile data, but does not erase
    # pre-existing profile facts with nulls.
    conn.execute(
        """UPDATE horses SET sire=COALESCE(?, sire), dam=COALESCE(?, dam),
               dam_sire=COALESCE(?, dam_sire), sex=COALESCE(?, sex), color=COALESCE(?, color)
           WHERE horse_id=?""",
        (profile.sire, profile.dam, profile.dam_sire, _sex_code(profile.sex), profile.color, horse_id),
    )
    return horse_id


def _surface(value: str | None) -> str:
    text = (value or "").lower()
    if text.startswith("turf") or text.startswith("i-"):
        return "turf"
    if text.startswith(("aw", "synthetic")):
        return "all_weather"
    return "dirt"


def _lengths(value: str | None) -> float:
    text = (value or "").strip()
    try:
        return float(text)
    except ValueError:
        match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", text)
        if match and int(match.group(2)):
            return int(match.group(1)) / int(match.group(2))
    return 0.0


def _seconds(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    # DK rendered cells carry a timing designation (for example ``50.23 B``).
    # Preserve that raw cell elsewhere, but normalize only the leading clock.
    clock = re.match(r"(\d+(?::\d+(?:\.\d+)?)?|\d+\.\d+)", text)
    if clock:
        text = clock.group(1)
    try:
        if ":" in text:
            minute, second = text.split(":", 1)
            return int(minute) * 60 + float(second)
        return float(text)
    except ValueError:
        return None


def _source_row_id(prefix: str, horse_name: str, raw_text: str | None) -> str:
    digest = hashlib.sha256(f"{horse_name}\n{raw_text or ''}".encode("utf-8")).hexdigest()[:24]
    return f"dk_markdown:{prefix}:{digest}"


def _repair_missing_workout_rank_denominators(
    conn: sqlite3.Connection, card: DraftKingsMarkdownCard,
) -> None:
    """Backfill only missing denominator values for idempotent DK workout rows."""
    for workout in card.workouts:
        if workout.rank_denominator is None:
            continue
        source_row_id = _source_row_id("workout", workout.horse_name, workout.raw_text)
        conn.execute(
            "UPDATE workouts SET rank_denominator=? "
            "WHERE source_row_id=? AND rank_denominator IS NULL",
            (workout.rank_denominator, source_row_id),
        )


def _entry_id_by_name(conn: sqlite3.Connection, card_id: int) -> dict[str, tuple[int, int]]:
    rows = conn.execute(
        """SELECT e.entry_id, e.horse_id, h.name FROM entries e
           JOIN horses h ON h.horse_id=e.horse_id WHERE e.card_id=?""", (card_id,)
    ).fetchall()
    return {str(row[2]).casefold(): (int(row[0]), int(row[1])) for row in rows}


def _materially_differs_from_existing_card(
    conn: sqlite3.Connection, card_id: int, imported_entries: list[Any]
) -> bool:
    """Compare source-owned core entry fields before revising a matched card."""
    rows = conn.execute(
        """SELECT h.name, e.post_position, e.program_number, e.weight,
                  e.morning_line_odds, tr.full_name, jo.full_name
           FROM entries e JOIN horses h ON h.horse_id=e.horse_id
           LEFT JOIN people tr ON tr.person_id=e.trainer_id
           LEFT JOIN people jo ON jo.person_id=e.jockey_id
           WHERE e.card_id=? AND e.scratch_flag=0""",
        (card_id,),
    ).fetchall()
    if len(rows) != len(imported_entries):
        return True
    existing = {str(row[0]).casefold(): row for row in rows}
    if len(existing) != len(imported_entries):
        return True
    for entry in imported_entries:
        row = existing.get((entry.horse_name or "").casefold())
        if row is None:
            return True
        parsed_ml = parse_morning_line(entry.morning_line)
        comparable = (
            (row[1], entry.post_position),
            ((row[2] or "").strip(), (entry.program_number or "").strip()),
            (row[3], entry.weight),
            ((row[5] or "").strip(), (entry.trainer or "").strip()),
            ((row[6] or "").strip(), (entry.jockey or "").strip()),
        )
        if any(left != right for left, right in comparable):
            return True
        if parsed_ml is None or row[4] is None or abs(float(row[4]) - float(parsed_ml)) > 1e-9:
            return True
    return False


def persist_validated_draftkings_markdown(
    conn: sqlite3.Connection,
    card: DraftKingsMarkdownCard,
    validation: ValidationResult,
    *,
    source_filename: str,
    source_path: str | None = None,
) -> MarkdownPersistenceResult:
    """Persist a PASS card to canonical tables and append immutable provenance.

    Same-SHA imports are idempotent.  A changed SHA is a separately auditable
    revision; canonical core entry fields are upserted from the new validated
    source without deleting historical rows from older imports.
    """
    if not validation.passed:
        raise ValueError("Cannot persist DraftKings Markdown: card validation did not pass.")
    ensure_draftkings_markdown_intake_tables(conn)
    existing_import = conn.execute(
        """SELECT revision_id, card_id FROM dk_markdown_import_revisions
           WHERE file_sha256=? AND parser_version=?""",
        (card.source_sha256, card.parser_version),
    ).fetchone()
    if existing_import:
        card_id = int(existing_import[1])
        _repair_missing_workout_rank_denominators(conn, card)
        _persist_dk_source_artifact(conn, card, source_filename=source_filename, source_path=source_path, card_id=card_id)
        conn.commit()
        return MarkdownPersistenceResult(
            card_id=card_id, already_imported=True, repaired_existing_card=False,
            materially_different_from_existing=False,
            persisted_runner_count=_active_entry_count(conn, card_id),
            persisted_past_performance_count=_history_count(conn, card_id),
            persisted_workout_count=_workout_count(conn, card_id), import_id=int(existing_import[0]),
        )

    race = card.race
    track_resolution = resolve_track(track_name=race.track or "")
    track_code = track_resolution.get("track_code") or (race.track or "UNK")[:6].upper()
    race_date = race.race_date.isoformat() if race.race_date else None
    if not race_date or race.race_number is None:
        raise ValueError("Validated card lacks canonical race identity.")
    existing_card_id = find_race_card(conn, track_code, race_date, int(race.race_number))
    materially_different = (
        _materially_differs_from_existing_card(conn, int(existing_card_id), card.entries)
        if existing_card_id is not None else False
    )
    track_id = _track_id(conn, track_code, race.track or track_code)
    distance_yards = int(round((race.normalized_distance_furlongs or 0.0) * 220))
    if distance_yards <= 0:
        raise ValueError("Validated card has no usable canonical distance.")
    if existing_card_id is None:
        conn.execute(
            """INSERT INTO race_cards
               (track_id, card_date, race_number, purse, distance_yards, surface, race_class,
                age_restriction, conditions, field_size, scheduled_post_time_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (track_id, race_date, int(race.race_number), race.purse, distance_yards, _surface(race.surface),
             race.class_code, race.age_restriction, race.surface_condition, len(card.entries),
             race.scheduled_post_time),
        )
        card_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    else:
        card_id = int(existing_card_id)
        conn.execute(
            """UPDATE race_cards SET track_id=?, purse=COALESCE(?, purse),
               distance_yards=?, surface=?, race_class=COALESCE(?, race_class),
               age_restriction=COALESCE(?, age_restriction), conditions=COALESCE(?, conditions),
               field_size=?, scheduled_post_time_utc=COALESCE(?, scheduled_post_time_utc)
               WHERE card_id=?""",
            (track_id, race.purse, distance_yards, _surface(race.surface), race.class_code,
             race.age_restriction, race.surface_condition, len(card.entries), race.scheduled_post_time, card_id),
        )

    for entry in card.entries:
        horse_id = _horse_id(conn, entry)
        trainer_id = _person_id(conn, entry.trainer, "trainer")
        jockey_id = _person_id(conn, entry.jockey, "jockey")
        owner_id = _person_id(conn, entry.owner, "owner")
        life = entry.record_splits.get("life")
        dirt = entry.record_splits.get("dirt")
        distance = entry.record_splits.get("distance")
        ml = parse_morning_line(entry.morning_line)
        if not ml or ml <= 0:
            raise ValueError(f"Validated card has unusable morning line for {entry.horse_name!r}")
        current = conn.execute(
            "SELECT entry_id FROM entries WHERE card_id=? AND horse_id=?", (card_id, horse_id)
        ).fetchone()
        if current is None:
            current = conn.execute(
                "SELECT entry_id FROM entries WHERE card_id=? AND post_position=?", (card_id, entry.post_position)
            ).fetchone()
        if current is None:
            conn.execute(
                """INSERT INTO entries
                   (card_id, horse_id, trainer_id, jockey_id, owner_id, post_position, weight,
                    morning_line_odds, scratch_flag, program_number, medication_weight_equipment,
                    career_starts, career_wins, career_places, career_shows, career_earnings,
                    dirt_starts, dirt_wins, dist_starts, dist_wins)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (card_id, horse_id, trainer_id, jockey_id, owner_id, entry.post_position, entry.weight,
                 float(ml), 0, entry.program_number, entry.medication_weight_equipment,
                 life.starts if life else None, life.wins if life else None, life.places if life else None,
                 life.shows if life else None, life.earnings if life else None,
                 dirt.starts if dirt else None, dirt.wins if dirt else None,
                 distance.starts if distance else None, distance.wins if distance else None),
            )
        else:
            conn.execute(
                """UPDATE entries SET horse_id=?, trainer_id=?, jockey_id=?, owner_id=?, post_position=?, weight=?,
                   morning_line_odds=?, scratch_flag=0, program_number=?, medication_weight_equipment=?,
                   career_starts=COALESCE(?, career_starts), career_wins=COALESCE(?, career_wins),
                   career_places=COALESCE(?, career_places), career_shows=COALESCE(?, career_shows),
                   career_earnings=COALESCE(?, career_earnings), dirt_starts=COALESCE(?, dirt_starts),
                   dirt_wins=COALESCE(?, dirt_wins), dist_starts=COALESCE(?, dist_starts),
                   dist_wins=COALESCE(?, dist_wins)
                   WHERE entry_id=?""",
                (horse_id, trainer_id, jockey_id, owner_id, entry.post_position, entry.weight, float(ml),
                 entry.program_number, entry.medication_weight_equipment,
                 life.starts if life else None, life.wins if life else None, life.places if life else None,
                 life.shows if life else None, life.earnings if life else None,
                 dirt.starts if dirt else None, dirt.wins if dirt else None,
                 distance.starts if distance else None, distance.wins if distance else None, int(current[0])),
            )
        conn.execute(
            """INSERT OR REPLACE INTO dk_horse_profile_snapshots
               (horse_id, card_id, file_sha256, parser_version, source_as_of, owner_name, breeder_name,
                age, sex_raw, color, sire, dam, dam_sire, raw_profile, record_splits_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (horse_id, card_id, card.source_sha256, card.parser_version, card.as_of.isoformat(),
             entry.owner, entry.breeder or entry.horse_profile.breeder, entry.horse_profile.age,
             entry.horse_profile.sex, entry.horse_profile.color, entry.horse_profile.sire,
             entry.horse_profile.dam, entry.horse_profile.dam_sire, entry.horse_profile.raw_profile,
             json.dumps({key: dataclasses.asdict(value) for key, value in entry.record_splits.items()}, sort_keys=True)),
        )

    entry_by_name = _entry_id_by_name(conn, card_id)
    source_document_id = f"dk_markdown:{card.source_sha256}"
    for pp in card.past_performances:
        pair = entry_by_name.get((pp.horse_name or "").casefold())
        if not pair:
            continue
        entry_id, horse_id = pair
        source_row_id = _source_row_id("pp", pp.horse_name, pp.raw_text)
        duplicate = conn.execute("SELECT 1 FROM horse_starts WHERE source_row_id=?", (source_row_id,)).fetchone()
        finish = pp.finish_position if isinstance(pp.finish_position, int) else None
        if not duplicate:
            conn.execute(
                """INSERT INTO horse_starts
                   (entry_id, horse_id, card_id, finish_position, lengths_behind, start_date,
                    track_code, race_class_raw, distance_furlongs, surface, historical_odds_raw,
                    historical_odds_type, is_scratch, source_provider, source_document_id, source_row_id,
                    surface_condition_raw, program_or_post, historical_jockey, trip_comment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'off_odds', ?, 'draftkings_markdown', ?, ?, ?, ?, ?, ?)""",
                (entry_id, horse_id, card_id, finish, _lengths(pp.beaten_lengths),
                 pp.start_date.isoformat() if pp.start_date else None,
                 resolve_track(track_name=pp.track or "").get("track_code") or pp.track,
                 pp.race_class, parse_furlongs(pp.distance), _surface(pp.surface_condition), pp.odds,
                 1 if pp.finish_position == "SCR" else 0, source_document_id, source_row_id,
                 pp.raw_surface_condition, pp.program_or_post, pp.jockey, pp.comment),
            )
    for workout in card.workouts:
        pair = entry_by_name.get((workout.horse_name or "").casefold())
        if not pair:
            continue
        _, horse_id = pair
        source_row_id = _source_row_id("workout", workout.horse_name, workout.raw_text)
        existing_workout = conn.execute(
            "SELECT workout_id, rank_denominator FROM workouts WHERE source_row_id=?",
            (source_row_id,),
        ).fetchone()
        if existing_workout:
            # ``source_row_id`` is the canonical idempotency key for imported
            # workout rows.  Earlier extraction revisions created these rows
            # without the rank denominator; repair only that missing value and
            # never overwrite a populated/manual value on reimport.
            if existing_workout[1] is None and workout.rank_denominator is not None:
                conn.execute(
                    "UPDATE workouts SET rank_denominator=? "
                    "WHERE workout_id=? AND rank_denominator IS NULL",
                    (workout.rank_denominator, existing_workout[0]),
                )
            continue
        track_code_for_work = resolve_track(track_name=workout.track or "").get("track_code")
        workout_track_id = _track_id(conn, track_code_for_work, workout.track or track_code_for_work) if track_code_for_work else None
        conn.execute(
            """INSERT INTO workouts
               (horse_id, workout_date, track_id, distance_furlongs, time_seconds, work_grade,
                surface, location_label, source_rank, source_provider, source_document_id, source_row_id,
                surface_condition_raw, raw_time, workout_designation, rank_denominator)
               VALUES (?, ?, ?, ?, ?, 'N', ?, ?, ?, 'draftkings_markdown', ?, ?, ?, ?, ?, ?)""",
            (horse_id, workout.work_date.isoformat() if workout.work_date else None, workout_track_id,
             parse_furlongs(workout.distance), _seconds(workout.time), _surface(workout.surface_condition),
             workout.track, workout.rank_numerator or _rank(workout.rank), source_document_id, source_row_id,
             workout.raw_surface_condition, workout.raw_time, workout.designation, workout.rank_denominator),
        )

    old_import = conn.execute(
        "SELECT import_id FROM dk_markdown_imports WHERE file_sha256=?", (card.source_sha256,)
    ).fetchone()
    if not old_import:
        conn.execute(
        """INSERT INTO dk_markdown_imports
           (file_sha256, source_filename, source_path, source_format, parser_version, validation_status,
            validation_json, scoring_as_of, card_id, parsed_runner_count, past_performance_count, workout_count)
           VALUES (?, ?, ?, ?, ?, 'PASS', ?, ?, ?, ?, ?, ?)""",
        (card.source_sha256, source_filename, source_path, card.source_format, card.parser_version,
         json.dumps(validation.to_dict(), sort_keys=True), card.as_of.isoformat(), card_id,
         validation.parsed_unique_runner_count, validation.past_performance_row_count, validation.workout_row_count),
        )
    _persist_dk_source_artifact(conn, card, source_filename=source_filename, source_path=source_path, card_id=card_id)
    conn.execute(
        """INSERT INTO dk_markdown_import_revisions
           (file_sha256, parser_version, source_filename, source_path, source_format, validation_status,
            validation_json, scoring_as_of, card_id)
           VALUES (?, ?, ?, ?, ?, 'PASS', ?, ?, ?)""",
        (card.source_sha256, card.parser_version, source_filename, source_path, card.source_format,
         json.dumps(validation.to_dict(), sort_keys=True), card.as_of.isoformat(), card_id),
    )
    import_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return MarkdownPersistenceResult(
        card_id=card_id, already_imported=False, repaired_existing_card=existing_card_id is not None,
        materially_different_from_existing=materially_different,
        persisted_runner_count=_active_entry_count(conn, card_id),
        persisted_past_performance_count=_history_count(conn, card_id),
        persisted_workout_count=_workout_count(conn, card_id), import_id=import_id,
    )


def _rank(value: str | None) -> int | None:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else None


def _active_entry_count(conn: sqlite3.Connection, card_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM entries WHERE card_id=? AND scratch_flag=0", (card_id,)).fetchone()[0])


def _history_count(conn: sqlite3.Connection, card_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM horse_starts WHERE card_id=?", (card_id,)).fetchone()[0])


def _workout_count(conn: sqlite3.Connection, card_id: int) -> int:
    return int(conn.execute(
        """SELECT COUNT(*) FROM workouts w JOIN entries e ON e.horse_id=w.horse_id
           WHERE e.card_id=?""", (card_id,)
    ).fetchone()[0])


def markdown_card_score_readiness(conn: sqlite3.Connection, card_id: int) -> MarkdownCardScoreReadiness:
    """Return visible, input-specific blockers before feature build or scoring."""
    ensure_draftkings_markdown_intake_tables(conn)
    provenance = conn.execute(
        """SELECT validation_status, parsed_runner_count FROM dk_markdown_imports
           WHERE card_id=? ORDER BY import_id DESC LIMIT 1""", (card_id,)
    ).fetchone()
    blockers: list[ScoreReadinessBlocker] = []
    expected = int(provenance[1]) if provenance else 0
    if not provenance:
        blockers.append(ScoreReadinessBlocker("MISSING_MARKDOWN_PROVENANCE", "Cannot score: no validated Markdown import is linked to this card."))
    elif provenance[0] != "PASS":
        blockers.append(ScoreReadinessBlocker("MARKDOWN_VALIDATION_FAILED", "Cannot score: latest Markdown import did not pass validation."))
    active = _active_entry_count(conn, card_id)
    if expected and active != expected:
        blockers.append(ScoreReadinessBlocker("RUNNER_COUNT_MISMATCH", f"Cannot score: persisted active runners={active}; validated runners={expected}."))
    rows = conn.execute(
        """SELECT h.name, e.program_number, e.weight, e.morning_line_odds,
                  tr.full_name AS trainer, jo.full_name AS jockey
           FROM entries e JOIN horses h ON h.horse_id=e.horse_id
           LEFT JOIN people tr ON tr.person_id=e.trainer_id
           LEFT JOIN people jo ON jo.person_id=e.jockey_id
           WHERE e.card_id=? AND e.scratch_flag=0 ORDER BY e.post_position""", (card_id,)
    ).fetchall()
    for row in rows:
        name = str(row[0])
        for code, value, label in (
            ("MISSING_PROGRAM_NUMBER", row[1], "program number"),
            ("MISSING_WEIGHT", row[2], "weight"),
            ("MISSING_MORNING_LINE", row[3], "morning line"),
            ("MISSING_TRAINER", row[4], "trainer"),
            ("MISSING_JOCKEY", row[5], "jockey"),
        ):
            if value is None or str(value).strip() == "":
                blockers.append(ScoreReadinessBlocker(code, f"Cannot score: {label} missing for {name}."))
    return MarkdownCardScoreReadiness(
        score_eligible=not blockers, blockers=tuple(blockers), validated_runner_count=expected,
        persisted_runner_count=len(rows), active_runner_count=active,
    )


def exportable_markdown_card_entries(conn: sqlite3.Connection, card_id: int) -> list[dict[str, Any]]:
    """Canonical pre-score export.  Model fields intentionally remain null."""
    rows = conn.execute(
        """SELECT h.name, e.post_position, tr.full_name, jo.full_name, e.morning_line_odds,
                  e.morning_line_prob
           FROM entries e JOIN horses h ON h.horse_id=e.horse_id
           LEFT JOIN people tr ON tr.person_id=e.trainer_id
           LEFT JOIN people jo ON jo.person_id=e.jockey_id
           WHERE e.card_id=? AND e.scratch_flag=0 ORDER BY e.post_position""", (card_id,)
    ).fetchall()
    return [
        {"Horse": row[0], "Post": row[1], "Trainer": row[2], "Jockey": row[3],
         "Morning Line": row[4], "ML-Implied Probability": row[5], "Model Probability": None,
         "Fair Odds": None, "Value Score": None, "Bet Tag": "not_scored"}
        for row in rows
    ]


def canonical_markdown_card_ui_payload(conn: sqlite3.Connection, card_id: int) -> dict[str, Any]:
    """Hydrate the existing Market Intake preview from canonical persisted rows."""
    race = conn.execute(
        """SELECT t.abbrev, t.name, rc.card_date, rc.race_number, rc.distance_yards,
                  rc.surface, rc.conditions, rc.race_class, rc.purse, rc.field_size
           FROM race_cards rc JOIN tracks t ON t.track_id=rc.track_id WHERE rc.card_id=?""",
        (card_id,),
    ).fetchone()
    if not race:
        raise ValueError(f"Canonical card_id={card_id} no longer exists.")
    export_rows = exportable_markdown_card_entries(conn, card_id)
    runners = [
        {"horse_name": row["Horse"], "post_position": row["Post"], "program_number": row["Post"],
         "trainer": row["Trainer"], "jockey": row["Jockey"], "morning_line": row["Morning Line"],
         "ml": row["Morning Line"], "morning_line_decimal": row["Morning Line"], "is_scratched": False}
        for row in export_rows
    ]
    return {
        "ok": True, "error": None, "warnings": [], "track_code": race[0],
        "track_code_resolved": race[0], "track_name": race[1], "race_date": race[2],
        "race_number": race[3], "distance_text": f"{float(race[4]) / 220:g} F",
        "surface": race[5], "going": race[6], "race_type": race[7], "purse_usd": race[8],
        "field_size": race[9], "runners": runners, "is_draftkings_markdown": True,
        "canonical_card_id": card_id,
    }


def reprocess_stored_draftkings_markdown_import(
    conn: sqlite3.Connection, file_sha256: str
) -> MarkdownPersistenceResult:
    """Re-extract a stored Markdown source with the current parser version.

    The original SHA remains immutable provenance.  The revision table makes
    ``SHA + parser_version`` idempotent, so a parser upgrade repairs newly
    supported fields without duplicating natural-key PP/workout rows.
    """
    ensure_draftkings_markdown_intake_tables(conn)
    row = conn.execute(
        """SELECT source_filename, source_path, scoring_as_of
           FROM dk_markdown_import_revisions WHERE file_sha256=?
           ORDER BY revision_id DESC LIMIT 1""",
        (file_sha256,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """SELECT source_filename, source_path, scoring_as_of
               FROM dk_markdown_imports WHERE file_sha256=?""", (file_sha256,)
        ).fetchone()
    if row is None:
        raise ValueError(f"No stored DraftKings Markdown provenance for SHA {file_sha256}.")
    source_path = str(row[1] or "")
    if not source_path or not Path(source_path).is_file():
        raise ValueError("Stored Markdown raw source is unavailable; cannot safely reprocess it.")
    as_of = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00"))
    card = parse_draftkings_markdown(Path(source_path), as_of=as_of)
    validation = validate_draftkings_markdown_card(card)
    return persist_validated_draftkings_markdown(
        conn, card, validation, source_filename=str(row[0]), source_path=source_path,
    )
