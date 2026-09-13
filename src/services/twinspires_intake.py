"""Append-only TwinSpires observation lane.

This module never writes ``entries``, ``horses``, ``horse_starts``,
``workouts``, or ``feature_store``.  Its candidates are diagnostic evidence,
not runtime feature values.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ingest.twinspires_markdown import (
    TwinSpiresMarkdownCard, TwinSpiresRecord, parse_twinspires_markdown,
)
from src.utils.horse_norm import normalize_horse_name


_DDL = """
CREATE TABLE IF NOT EXISTS source_artifacts (
  artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_provider TEXT NOT NULL,
  source_filename TEXT NOT NULL,
  source_path TEXT,
  raw_bytes BLOB NOT NULL,
  sha256 TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  ingestion_timestamp TEXT NOT NULL,
  declared_as_of_timestamp TEXT,
  as_of_status TEXT NOT NULL CHECK(as_of_status IN ('PROVEN','UNPROVEN','INVALID')),
  validation_status TEXT NOT NULL,
  validation_errors_json TEXT NOT NULL,
  normalized_record_count INTEGER NOT NULL,
  normalized_records_json TEXT NOT NULL,
  card_id INTEGER REFERENCES race_cards(card_id),
  UNIQUE(source_provider, sha256, parser_version, card_id)
);
CREATE TABLE IF NOT EXISTS source_identity_reconciliations (
  reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id INTEGER NOT NULL REFERENCES source_artifacts(artifact_id),
  source_horse_name TEXT NOT NULL,
  entry_id INTEGER REFERENCES entries(entry_id),
  horse_id INTEGER REFERENCES horses(horse_id),
  match_status TEXT NOT NULL CHECK(match_status IN ('EXACT_MATCH','NORMALIZED_EXACT_MATCH','AMBIGUOUS','UNMATCHED')),
  match_method TEXT NOT NULL,
  confidence REAL NOT NULL,
  candidate_set_json TEXT NOT NULL,
  UNIQUE(artifact_id, source_horse_name)
);
CREATE TABLE IF NOT EXISTS source_observations (
  observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id INTEGER NOT NULL REFERENCES source_artifacts(artifact_id),
  entry_id INTEGER NOT NULL REFERENCES entries(entry_id),
  horse_id INTEGER NOT NULL REFERENCES horses(horse_id),
  source_provider TEXT NOT NULL CHECK(source_provider='twinspires'),
  source_row_id TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  normalized_json TEXT NOT NULL,
  UNIQUE(artifact_id, source_row_id)
);
CREATE TABLE IF NOT EXISTS source_feature_candidates (
  candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  observation_id INTEGER NOT NULL REFERENCES source_observations(observation_id),
  entry_id INTEGER NOT NULL REFERENCES entries(entry_id),
  feature_name TEXT NOT NULL,
  source_provider TEXT NOT NULL CHECK(source_provider='twinspires'),
  source_artifact_sha256 TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  as_of_status TEXT NOT NULL CHECK(as_of_status IN ('PROVEN','UNPROVEN','INVALID')),
  evidence_count INTEGER NOT NULL,
  normalized_value REAL,
  normalized_text TEXT,
  derivation_formula TEXT,
  reason_limitation TEXT NOT NULL,
  candidate_status TEXT NOT NULL,
  UNIQUE(observation_id, feature_name)
);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_card ON source_artifacts(card_id, source_provider);
CREATE INDEX IF NOT EXISTS idx_source_candidates_entry ON source_feature_candidates(entry_id, feature_name);
"""


@dataclasses.dataclass(frozen=True)
class IdentityResult:
    source_horse_name: str
    entry_id: int | None
    horse_id: int | None
    match_status: str
    match_method: str
    confidence: float
    candidates: tuple[dict[str, Any], ...]


@dataclasses.dataclass(frozen=True)
class TwinSpiresValidation:
    passed: bool
    card_id: int
    race_identity: dict[str, Any]
    record_count: int
    reconciliation: tuple[IdentityResult, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def ensure_twinspires_source_tables(conn: sqlite3.Connection) -> None:
    """Install isolated, additive source-observation entities idempotently."""
    conn.executescript(_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(source_artifacts)").fetchall()}
    if "normalized_records_json" not in columns:
        conn.execute("ALTER TABLE source_artifacts ADD COLUMN normalized_records_json TEXT NOT NULL DEFAULT '[]'")
    conn.commit()


def _card_entries(conn: sqlite3.Connection, card_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    card = conn.execute(
        """SELECT rc.card_id, rc.card_date, rc.race_number, rc.field_size, t.name AS track_name, t.abbrev
           FROM race_cards rc JOIN tracks t ON t.track_id=rc.track_id WHERE rc.card_id=?""", (card_id,)
    ).fetchone()
    if card is None:
        raise LookupError(f"card_id={card_id} not found")
    entries = [dict(row) for row in conn.execute(
        """SELECT e.entry_id, e.horse_id, h.name FROM entries e JOIN horses h ON h.horse_id=e.horse_id
           WHERE e.card_id=? AND e.scratch_flag=0 ORDER BY e.post_position""", (card_id,)
    ).fetchall()]
    return dict(card), entries


def reconcile_twinspires_records(conn: sqlite3.Connection, card_id: int, records: tuple[TwinSpiresRecord, ...]) -> tuple[IdentityResult, ...]:
    _, entries = _card_entries(conn, card_id)
    results: list[IdentityResult] = []
    for record in records:
        exact = [row for row in entries if str(row["name"]).casefold() == record.horse_name.casefold()]
        normalized = [row for row in entries if normalize_horse_name(str(row["name"])) == normalize_horse_name(record.horse_name)]
        candidates = [{"entry_id": row["entry_id"], "horse_id": row["horse_id"], "horse_name": row["name"]} for row in (exact or normalized)]
        if len(exact) == 1:
            row, status, method, confidence = exact[0], "EXACT_MATCH", "casefold_exact", 1.0
        elif len(exact) > 1:
            row, status, method, confidence = None, "AMBIGUOUS", "casefold_exact", 0.0
        elif len(normalized) == 1:
            row, status, method, confidence = normalized[0], "NORMALIZED_EXACT_MATCH", "horse_norm", 1.0
        elif len(normalized) > 1:
            row, status, method, confidence = None, "AMBIGUOUS", "horse_norm", 0.0
        else:
            row, status, method, confidence = None, "UNMATCHED", "none", 0.0
        results.append(IdentityResult(record.horse_name, row["entry_id"] if row else None, row["horse_id"] if row else None,
                                      status, method, confidence, tuple(candidates)))
    return tuple(results)


def validate_twinspires_card(conn: sqlite3.Connection, card_id: int, card: TwinSpiresMarkdownCard) -> TwinSpiresValidation:
    race, entries = _card_entries(conn, card_id)
    reconciliation = reconcile_twinspires_records(conn, card_id, card.records)
    errors = list(card.parser_errors)
    warnings = list(card.parser_warnings)
    # This compact export has no track/date/race header.  Association comes
    # from the explicit card_id caller parameter, never the filename.
    warnings.append("artifact has no explicit track/race/date identity; card association supplied by --card-id")
    if len(card.records) != len(entries):
        errors.append(f"runner count mismatch: source={len(card.records)} canonical_active={len(entries)}")
    matched_ids = [item.entry_id for item in reconciliation if item.entry_id is not None]
    if len(matched_ids) != len(set(matched_ids)):
        errors.append("multiple TwinSpires rows reconcile to one canonical entry")
    for item in reconciliation:
        if item.match_status in {"AMBIGUOUS", "UNMATCHED"}:
            errors.append(f"identity {item.match_status.lower()}: {item.source_horse_name}")
    return TwinSpiresValidation(not errors, card_id, race, len(card.records), reconciliation,
                                tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(warnings)))


def _candidate_rows(record: TwinSpiresRecord, artifact_sha: str, parser_version: str, as_of_status: str) -> list[dict[str, Any]]:
    fields = [
        ("run_style", None, record.run_style, "source-linked E/P-style observation only; no pace pressure or pace-fit inference"),
        ("twinspires_speed_last", record.last_speed, None, "proprietary LR speed; not labeled Beyer without explicit artifact evidence"),
        ("twinspires_speed_average", record.avg_speed, None, "proprietary AVG speed; not labeled Beyer without explicit artifact evidence"),
        ("twinspires_speed_back", record.back_speed, None, "proprietary BACK speed/reference; not labeled Beyer without explicit artifact evidence"),
        ("twinspires_class_rating", record.class_rating, None, "source-specific class rating; race-type/source context is not universal truth"),
        ("twinspires_power_rating", record.power_rating, None, "source-specific power rating; not mapped to model class or power feature"),
        ("twinspires_jockey_win_pct", record.jockey_win_pct, None, "source-context jockey percentage; no global jockey inference"),
        ("twinspires_trainer_win_pct", record.trainer_win_pct, None, "source-context trainer percentage; no global trainer inference"),
    ]
    rows = []
    for name, value, text, reason in fields:
        rows.append({"feature_name": name, "normalized_value": value, "normalized_text": text,
                     "derivation_formula": None, "reason_limitation": reason,
                     "candidate_status": "SOURCE_BACKED" if value is not None or text is not None else "UNAVAILABLE",
                     "evidence_count": 1 if value is not None or text is not None else 0,
                     "source_artifact_sha256": artifact_sha, "parser_version": parser_version, "as_of_status": as_of_status})
    return rows


def persist_twinspires_card(conn: sqlite3.Connection, card: TwinSpiresMarkdownCard, validation: TwinSpiresValidation, *, raw_bytes: bytes) -> int:
    """Persist raw source and separated observations. Invalid reconciliation is raw-only."""
    ensure_twinspires_source_tables(conn)
    status = "PASS" if validation.passed else "FAIL"
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT artifact_id FROM source_artifacts WHERE source_provider='twinspires' AND sha256=? AND parser_version=? AND card_id=?",
        (card.source_sha256, card.parser_version, validation.card_id),
    ).fetchone()
    if row:
        # Repair only the newly introduced immutable extraction payload when a
        # same-version artifact was first persisted by an older lane revision.
        conn.execute("UPDATE source_artifacts SET normalized_records_json=? WHERE artifact_id=? AND normalized_records_json='[]'",
                     (json.dumps([dataclasses.asdict(record) for record in card.records], sort_keys=True), int(row[0])))
        conn.commit()
        return int(row[0])
    conn.execute(
        """INSERT INTO source_artifacts (source_provider,source_filename,source_path,raw_bytes,sha256,parser_version,
           ingestion_timestamp,declared_as_of_timestamp,as_of_status,validation_status,validation_errors_json,normalized_record_count,normalized_records_json,card_id)
           VALUES ('twinspires',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (card.source_filename, card.source_path, raw_bytes, card.source_sha256, card.parser_version, now,
         card.declared_as_of, card.as_of_status, status, json.dumps({"errors": validation.errors, "warnings": validation.warnings}),
         len(card.records), json.dumps([dataclasses.asdict(record) for record in card.records], sort_keys=True), validation.card_id),
    )
    artifact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    by_name = {item.source_horse_name: item for item in validation.reconciliation}
    for item in validation.reconciliation:
        conn.execute("""INSERT INTO source_identity_reconciliations
          (artifact_id,source_horse_name,entry_id,horse_id,match_status,match_method,confidence,candidate_set_json)
          VALUES (?,?,?,?,?,?,?,?)""", (artifact_id, item.source_horse_name, item.entry_id, item.horse_id,
          item.match_status, item.match_method, item.confidence, json.dumps(item.candidates, sort_keys=True)))
    if validation.passed:
        for record in card.records:
            item = by_name[record.horse_name]
            source_row_id = f"twinspires:{card.source_sha256}:{record.program_number}"
            normalized = dataclasses.asdict(record)
            conn.execute("""INSERT INTO source_observations
              (artifact_id,entry_id,horse_id,source_provider,source_row_id,raw_text,normalized_json)
              VALUES (?,?,?,'twinspires',?,?,?)""", (artifact_id, item.entry_id, item.horse_id, source_row_id,
              record.raw_text, json.dumps(normalized, sort_keys=True)))
            observation_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            for candidate in _candidate_rows(record, card.source_sha256, card.parser_version, card.as_of_status):
                conn.execute("""INSERT INTO source_feature_candidates
                  (observation_id,entry_id,feature_name,source_provider,source_artifact_sha256,parser_version,as_of_status,
                   evidence_count,normalized_value,normalized_text,derivation_formula,reason_limitation,candidate_status)
                  VALUES (?,? ,?,'twinspires',?,?,?,?,?,?,?,?,?)""",
                  (observation_id, item.entry_id, candidate["feature_name"], candidate["source_artifact_sha256"],
                   candidate["parser_version"], candidate["as_of_status"], candidate["evidence_count"],
                   candidate["normalized_value"], candidate["normalized_text"], candidate["derivation_formula"],
                   candidate["reason_limitation"], candidate["candidate_status"]))
    conn.commit()
    return artifact_id


def hydrate_twinspires_candidates(conn: sqlite3.Connection, card_id: int) -> list[dict[str, Any]]:
    """Read source-linked candidates only; no runtime feature hydration occurs."""
    ensure_twinspires_source_tables(conn)
    return [dict(row) for row in conn.execute("""SELECT c.*, h.name AS horse_name FROM source_feature_candidates c
      JOIN source_observations o ON o.observation_id=c.observation_id JOIN entries e ON e.entry_id=c.entry_id
      JOIN horses h ON h.horse_id=e.horse_id JOIN source_artifacts a ON a.artifact_id=o.artifact_id
      WHERE a.card_id=? AND c.source_provider='twinspires' ORDER BY c.entry_id,c.feature_name""", (card_id,)).fetchall()]


_STYLE_BUCKET = {"E": "front", "E/P": "presser", "P": "stalker", "S": "closer"}


def twinspires_pace_for_card(
    conn: sqlite3.Connection, card_id: int, *, source_path: str | Path | None = None,
    declared_as_of: str | None = None,
) -> tuple[dict[int, TwinSpiresRecord], str | None, str | None]:
    """Return validated, pre-race records by entry_id without writing to the DB.

    An explicit file is associated with the caller's card_id. Otherwise a
    single previously ingested, proven artifact for that card is eligible.
    The filename alone never establishes race identity or observation time.
    """
    if source_path is not None:
        try:
            card = parse_twinspires_markdown(source_path, declared_as_of=declared_as_of)
        except (OSError, UnicodeError) as exc:
            return {}, None, f"TwinSpires source unreadable: {exc}"
    else:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_artifacts'"
        ).fetchone()
        if not exists:
            return {}, None, None
        artifacts = conn.execute(
            """SELECT raw_bytes, source_filename, declared_as_of_timestamp
               FROM source_artifacts WHERE card_id=? AND source_provider='twinspires'
               AND validation_status='PASS' AND as_of_status='PROVEN'""",
            (card_id,),
        ).fetchall()
        if not artifacts:
            return {}, None, None
        if len(artifacts) != 1:
            return {}, None, "multiple proven TwinSpires artifacts for card; source ambiguous"
        raw, filename, as_of = artifacts[0]
        card = parse_twinspires_markdown(bytes(raw), source_filename=filename, declared_as_of=as_of)

    if card.as_of_status != "PROVEN" or card.declared_as_of is None:
        return {}, None, "TwinSpires source as-of timestamp is not proven"
    race, _ = _card_entries(conn, card_id)
    observed = datetime.fromisoformat(card.declared_as_of)
    post = conn.execute(
        "SELECT scheduled_post_time_utc FROM race_cards WHERE card_id=?", (card_id,)
    ).fetchone()[0]
    if post:
        try:
            post_time = datetime.fromisoformat(str(post).replace("Z", "+00:00"))
        except ValueError:
            return {}, None, "card scheduled post time is invalid"
        if post_time.tzinfo is None or observed >= post_time.astimezone(timezone.utc):
            return {}, None, "TwinSpires source is not proven pre-post"
    elif observed.date().isoformat() >= str(race["card_date"]):
        return {}, None, "TwinSpires source is not proven before race date"

    validation = validate_twinspires_card(conn, card_id, card)
    if not validation.passed:
        return {}, None, "TwinSpires source validation failed: " + "; ".join(validation.errors)
    records = {
        item.entry_id: record
        for item, record in zip(validation.reconciliation, card.records, strict=True)
        if item.entry_id is not None and record.run_style_code in _STYLE_BUCKET
    }
    if len(records) != len(card.records):
        return {}, None, "TwinSpires source lacks an unambiguous style for every active runner"
    return records, card.declared_as_of, None


def write_reconciliation_csv(validation: TwinSpiresValidation, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("source_horse_name","entry_id","horse_id","match_status","match_method","confidence","unresolved_candidates"))
        writer.writeheader()
        for item in validation.reconciliation:
            writer.writerow({"source_horse_name": item.source_horse_name, "entry_id": item.entry_id, "horse_id": item.horse_id,
                             "match_status": item.match_status, "match_method": item.match_method, "confidence": item.confidence,
                             "unresolved_candidates": json.dumps(item.candidates, sort_keys=True)})
