"""Read-only durable-provenance audit for historical ``horse_starts`` rows.

The audit is intentionally unable to repair, infer, hydrate, or reconcile a
start.  It uses a SQLite ``mode=ro`` connection and emits only supplied report
files.  A provider/document string is not an artifact foreign key.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.services.model_family import classify_model_family
from src.services.training_as_of import training_as_of_status


ROW_COLUMNS = (
    "start_id", "entry_id", "horse_id", "card_id", "race_date", "historical_target_race_date", "surface", "distance_furlongs",
    "stakes_name", "race_class", "model_family", "source_provider", "source_artifact_id",
    "start_source_as_of_ts", "artifact_exists", "artifact_source_as_of_ts", "training_as_of_status", "classification", "reason_code",
)
SUMMARY_COLUMNS = (
    "model_family", "total_rows", "repairable_count", "repairable_partial_count",
    "ineligible_no_artifact_count", "ineligible_postrace_count", "first_repair_blocker",
)
CLASSIFICATIONS = ("REPAIRABLE", "REPAIRABLE_PARTIAL", "INELIGIBLE_NO_ARTIFACT", "INELIGIBLE_POSTRACE")


def open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    """Open the audit's sole database connection with SQLite write access disabled."""
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")} if _table_exists(conn, name) else set()


def _before(left: Any, right: Any) -> bool:
    """Conservative lexical ISO/date comparison: missing proof never passes."""
    return bool(left and right and str(left).replace("Z", "+00:00") < str(right).replace("Z", "+00:00"))


def _at_or_after(left: Any, right: Any) -> bool:
    return bool(left and right and str(left).replace("Z", "+00:00") >= str(right).replace("Z", "+00:00"))


def _schema_gap_rows(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    starts = _columns(conn, "horse_starts")
    artifacts = _columns(conn, "source_artifacts")
    if "source_artifact_id" not in starts:
        return True, "HORSE_STARTS_SOURCE_ARTIFACT_ID_COLUMN_MISSING"
    if "source_as_of_ts" not in artifacts and "declared_as_of_timestamp" not in artifacts:
        return True, "SOURCE_ARTIFACTS_AS_OF_COLUMN_MISSING"
    return False, None


def collect_horse_starts_provenance(conn: sqlite3.Connection, *, family: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Classify starts from an existing connection; used by in-memory tests too."""
    if not _table_exists(conn, "horse_starts"):
        return [], {"schema_gap": "HORSE_STARTS_TABLE_MISSING", "artifact_table": None}
    starts = _columns(conn, "horse_starts")
    artifacts = _columns(conn, "source_artifacts")
    gap, gap_reason = _schema_gap_rows(conn)
    has_cards, card_cols = _table_exists(conn, "race_cards"), _columns(conn, "race_cards")
    card_select = (
        "rc.card_date AS race_date,rc.surface AS card_surface,rc.distance_furlongs AS card_distance,rc.stakes_name AS stakes_name,rc.race_class AS card_race_class"
        if has_cards else "NULL AS race_date,NULL AS card_surface,NULL AS card_distance,NULL AS stakes_name,NULL AS card_race_class"
    )
    join_cards = "LEFT JOIN race_cards rc ON rc.card_id=hs.card_id" if has_cards else ""
    # Names are checked before they are interpolated, so this dynamic query is
    # solely for backward-compatible schema observation.
    source_id = "hs.source_artifact_id" if "source_artifact_id" in starts else "NULL"
    start_asof = "hs.source_as_of_ts" if "source_as_of_ts" in starts else "NULL"
    persisted_status = "hs.provenance_status" if "provenance_status" in starts else "NULL"
    provider = "hs.source_provider" if "source_provider" in starts else "NULL"
    source = "hs.surface" if "surface" in starts else "NULL"
    distance = "hs.distance_furlongs" if "distance_furlongs" in starts else "NULL"
    race_class = "hs.race_class_raw" if "race_class_raw" in starts else "NULL"
    historical_date = "hs.start_date AS historical_target_race_date" if "start_date" in starts else "NULL AS historical_target_race_date"
    conn.row_factory = sqlite3.Row
    query = f"""SELECT hs.start_id,hs.entry_id,hs.horse_id,hs.card_id,{provider} AS source_provider,
                      {source_id} AS source_artifact_id,{start_asof} AS start_source_as_of_ts,{persisted_status} AS provenance_status,{historical_date},
                      {source} AS start_surface,{distance} AS start_distance,{race_class} AS start_race_class,{card_select}
               FROM horse_starts hs {join_cards} ORDER BY hs.start_id"""
    raw_rows = [dict(row) for row in conn.execute(query)]
    artifact_by_id: dict[int, dict[str, Any]] = {}
    artifact_as_of_col = "source_as_of_ts" if "source_as_of_ts" in artifacts else "declared_as_of_timestamp"
    if not gap and artifacts:
        validation = "validation_status" if "validation_status" in artifacts else "NULL"
        as_of_status = "as_of_status" if "as_of_status" in artifacts else "NULL"
        for row in conn.execute(f"SELECT artifact_id,{artifact_as_of_col},{validation},{as_of_status} FROM source_artifacts"):
            artifact_by_id[int(row[0])] = {"source_as_of_ts": row[1], "validation_status": row[2], "as_of_status": row[3]}
    result: list[dict[str, Any]] = []
    for raw in raw_rows:
        surface = raw.get("card_surface") or raw.get("start_surface")
        distance = raw.get("card_distance") if raw.get("card_distance") is not None else raw.get("start_distance")
        race_class = raw.get("card_race_class") or raw.get("start_race_class")
        model_family = classify_model_family(surface, distance, raw.get("stakes_name"), race_class)
        artifact_id = raw.get("source_artifact_id")
        artifact = artifact_by_id.get(int(artifact_id)) if artifact_id is not None and not gap else None
        artifact_asof = artifact.get("source_as_of_ts") if artifact else None
        target_status = training_as_of_status(
            source_artifact_id=artifact_id if artifact is not None else None,
            source_as_of_timestamp=artifact_asof,
            target_decision_timestamp=raw.get("historical_target_race_date"),
            artifact_validation_status=artifact.get("validation_status") if artifact else None,
            artifact_as_of_status=artifact.get("as_of_status") if artifact else None,
        )
        race_date = raw.get("race_date")
        persisted_status = raw.get("provenance_status") if "provenance_status" in starts else None
        if persisted_status in CLASSIFICATIONS:
            classification, reason = persisted_status, "PERSISTED_PROVENANCE_STATUS"
        elif gap:
            classification, reason = "INELIGIBLE_NO_ARTIFACT", gap_reason
        elif artifact_id is None:
            classification, reason = "INELIGIBLE_NO_ARTIFACT", "SOURCE_ARTIFACT_ID_MISSING"
        elif artifact is None:
            classification, reason = "REPAIRABLE_PARTIAL", "SOURCE_ARTIFACT_REFERENCE_MISSING"
        elif artifact_asof is None:
            classification, reason = "REPAIRABLE_PARTIAL", "ARTIFACT_SOURCE_AS_OF_MISSING"
        elif _at_or_after(artifact_asof, race_date):
            classification, reason = "INELIGIBLE_POSTRACE", "ARTIFACT_SOURCE_AS_OF_NOT_PRE_RACE"
        elif not _before(artifact_asof, race_date):
            classification, reason = "REPAIRABLE_PARTIAL", "RACE_DATE_OR_AS_OF_UNPROVABLE"
        else:
            classification, reason = "REPAIRABLE", "ARTIFACT_AS_OF_PRE_RACE_PROVEN"
        row = {"start_id": raw["start_id"], "entry_id": raw.get("entry_id"), "horse_id": raw.get("horse_id"), "card_id": raw.get("card_id"), "race_date": race_date, "historical_target_race_date": raw.get("historical_target_race_date"), "surface": surface, "distance_furlongs": distance, "stakes_name": raw.get("stakes_name"), "race_class": race_class, "model_family": model_family, "source_provider": raw.get("source_provider"), "source_artifact_id": artifact_id, "start_source_as_of_ts": raw.get("start_source_as_of_ts"), "artifact_exists": artifact is not None, "artifact_source_as_of_ts": artifact_asof, "training_as_of_status": target_status, "classification": classification, "reason_code": reason}
        if family is None or model_family == family:
            result.append(row)
    return result, {"schema_gap": gap_reason, "artifact_table": "source_artifacts" if artifacts else None, "horse_starts_columns": sorted(starts), "source_artifacts_columns": sorted(artifacts)}


def summarize_horse_starts_provenance(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        grouped[str(row["model_family"])][str(row["classification"])] += 1
    summaries = []
    for family in sorted(grouped):
        counts = grouped[family]
        if counts["INELIGIBLE_NO_ARTIFACT"]:
            blocker = "INELIGIBLE_NO_ARTIFACT"
        elif counts["REPAIRABLE_PARTIAL"]:
            blocker = "REPAIRABLE_PARTIAL"
        elif counts["INELIGIBLE_POSTRACE"]:
            blocker = "INELIGIBLE_POSTRACE"
        else:
            blocker = "NONE"
        summaries.append({"model_family": family, "total_rows": sum(counts.values()), "repairable_count": counts["REPAIRABLE"], "repairable_partial_count": counts["REPAIRABLE_PARTIAL"], "ineligible_no_artifact_count": counts["INELIGIBLE_NO_ARTIFACT"], "ineligible_postrace_count": counts["INELIGIBLE_POSTRACE"], "first_repair_blocker": blocker})
    return summaries


def _write_csv(path: Path, columns: tuple[str, ...], rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def run_horse_starts_provenance_audit(db_path: Path, output_dir: Path, *, family: str | None = None) -> dict[str, Any]:
    """Audit existing starts and write deterministic acceptance artifacts only."""
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = open_readonly_connection(db_path)
    try:
        rows, schema = collect_horse_starts_provenance(conn, family=family)
    finally:
        conn.close()
    summaries = summarize_horse_starts_provenance(rows)
    paths = {"rows": output_dir / "horse_starts_provenance_audit.csv", "summary": output_dir / "horse_starts_provenance_summary.csv", "json": output_dir / "horse_starts_provenance_audit.json"}
    _write_csv(paths["rows"], ROW_COLUMNS, rows); _write_csv(paths["summary"], SUMMARY_COLUMNS, summaries)
    classification_counts = Counter(row["classification"] for row in rows)
    target_status_counts = Counter(row["training_as_of_status"] for row in rows)
    payload = {"read_only": True, "database_path": str(db_path), "artifact_table": schema["artifact_table"], "schema_gap": schema["schema_gap"], "overall_classification_counts": dict(sorted(classification_counts.items())), "training_as_of_status_counts": dict(sorted(target_status_counts.items())), "per_family": summaries, "blocker_diagnosis": schema["schema_gap"] or "row-level provenance classifications recorded", "artifacts": {name: str(path) for name, path in paths.items()}, "executed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    paths["json"].write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"rows": rows, "summaries": summaries, "schema": schema, "paths": paths, "summary": payload}
