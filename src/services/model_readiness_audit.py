"""Read-only training-corpus and production-model readiness audit.

This module is deliberately an *observer*.  It opens SQLite with ``mode=ro``
and only emits report files supplied by the caller.  In particular, it does
not import any builder, scorer, trainer, calibration, or intake function.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.models.trainer import calibration_audit_for_display, load_model_artifact
from src.services.feature_lineage import runtime_lineage_by_feature
from src.services.model_family import classify_model_family
from src.services.score_eligibility import active_feature_weights
from src.services.training_as_of import (
    POST_RACE_TARGET_SNAPSHOT,
    VALID_TRAINING_AS_OF_STATUSES,
    training_as_of_status,
)


FAMILIES = ("dirt_sprint", "dirt_route", "turf_sprint", "turf_route", "synthetic", "kentucky_derby")
INVALID_LINEAGE_STATUS = frozenset({"PLACEHOLDER", "UNAVAILABLE", "DEFAULTED", "UNKNOWN", ""})
INVALID_SOURCES = frozenset({"", "unknown", "seeded/default", "seeded", "defaulted"})
SUPPORTED_SOURCES = frozenset({"draftkings_markdown", "canonical_db", "firstbet_pdf"})
# These intentionally conservative, documented policy floors avoid declaring a
# three-way split merely because three completed races happen to exist.
MIN_RACES_PER_CALIBRATION_FOLD = 25
MIN_WINS_PER_CALIBRATION_FOLD = 10

INVENTORY_COLUMNS = (
    "card_id", "entry_id", "horse_id", "race_date", "decision_timestamp", "surface",
    "distance_furlongs", "distance_bucket", "race_class", "age_restriction", "track",
    "field_size", "model_family", "declared_race_family", "outcome_linked", "training_as_of_status",
    "pre_race_source_artifact_exists", "source_sha256", "parser_version", "source_provider",
    "source_as_of_timestamp", "source_as_of_status", "feature_store_row_exists",
    "feature_build_timestamp", "feature_lineage_valid", "active_features_evidence_valid",
    "artifact_context", "training_eligible", "exclusion_reason_codes",
)
FAMILY_SUMMARY_COLUMNS = (
    "model_family", "surface", "distance_bucket", "race_count_total", "race_count_training_eligible",
    "runner_count_total", "runner_count_training_eligible", "win_count_training_eligible", "date_min",
    "date_max", "as_of_provenance_coverage", "outcome_linkage_coverage", "feature_lineage_valid_coverage",
    "active_feature_valid_coverage", "race_grouped_split_viable", "artifact_status", "calibration_status",
    "production_eligible", "first_blocker", "ordered_blockers",
)
FEATURE_COLUMNS = (
    "model_family", "feature_name", "effective_weight", "race_family_applicable", "historical_non_null_count",
    "historical_evidence_valid_count", "historical_as_of_proven_count", "placeholder_count", "unavailable_count",
    "defaulted_count", "unknown_count", "source_provider_mix", "coverage_status", "blocking_reason",
)
EXCLUSION_COLUMNS = ("model_family", "card_id", "entry_id", "horse_id", "training_as_of_status", "reason_code", "detail")
SPLIT_COLUMNS = (
    "model_family", "fold", "race_count", "runner_count", "win_count", "date_min", "date_max",
    "race_ids", "split_status", "reason",
)
CALIBRATION_COLUMNS = (
    "model_family", "model_id", "model_name", "version", "artifact_path", "artifact_status",
    "calibration_artifact_path", "calibration_method", "calibration_audit_status", "calibration_audit_timestamp",
    "out_of_sample_chronological_race_grouped_data", "readiness_status", "production_eligible", "reason",
)
REMEDIATION_COLUMNS = ("model_family", "sequence", "blocker", "detail")


def open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    """Return the only SQLite connection used by this audit (read-only URI)."""
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _safe_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _iso_before(left: Any, right: Any) -> bool:
    """Conservative ISO/date comparison; absent or unparsable values fail."""
    if not left or not right:
        return False
    try:
        return str(left).replace("Z", "+00:00") <= str(right).replace("Z", "+00:00")
    except Exception:
        return False


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _value_present(value: Any) -> bool:
    return value is not None and not (isinstance(value, float) and not math.isfinite(value))


def _source_artifacts(conn: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    """Collect retained source metadata without assuming optional tables exist."""
    by_card: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if _table_exists(conn, "dk_markdown_imports"):
        for row in conn.execute("SELECT card_id,file_sha256,parser_version,validation_status,scoring_as_of FROM dk_markdown_imports"):
            if row[0] is not None:
                by_card[int(row[0])].append({"provider": "draftkings_markdown", "sha256": row[1], "parser_version": row[2], "validation_status": row[3], "as_of": row[4], "as_of_status": "PROVEN" if row[4] else "UNPROVEN"})
    if _table_exists(conn, "source_artifacts"):
        for row in conn.execute("SELECT card_id,source_provider,sha256,parser_version,validation_status,declared_as_of_timestamp,as_of_status FROM source_artifacts"):
            if row[0] is not None:
                by_card[int(row[0])].append({"provider": row[1], "sha256": row[2], "parser_version": row[3], "validation_status": row[4], "as_of": row[5], "as_of_status": row[6]})
    return by_card


def _outcomes(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """Return PP outcome candidates with historical-target leakage labels.

    A horse-start ``card_id`` belongs to the card that received the PP
    document, while ``start_date`` is the separately described past race.
    Later PP snapshots are retained for runtime evidence but cannot act as
    supervised labels for that past race.
    """
    result: dict[int, dict[str, Any]] = {}
    if _table_exists(conn, "horse_starts"):
        starts = _columns(conn, "horse_starts")
        artifact_table = _table_exists(conn, "source_artifacts")
        artifacts = _columns(conn, "source_artifacts") if artifact_table else set()
        start_id = "hs.start_id" if "start_id" in starts else "hs.rowid"
        source_id = "hs.source_artifact_id" if "source_artifact_id" in starts else "NULL"
        start_as_of = "hs.source_as_of_ts" if "source_as_of_ts" in starts else "NULL"
        target_date = "hs.start_date" if "start_date" in starts else "NULL"
        join = "LEFT JOIN source_artifacts sa ON sa.artifact_id=hs.source_artifact_id" if artifact_table and "source_artifact_id" in starts else ""
        artifact_as_of = "sa.declared_as_of_timestamp" if "declared_as_of_timestamp" in artifacts else start_as_of
        validation = "sa.validation_status" if "validation_status" in artifacts else "NULL"
        as_of_status = "sa.as_of_status" if "as_of_status" in artifacts else "NULL"
        query = f"""SELECT {start_id},hs.entry_id,hs.finish_position,{source_id},{start_as_of},
                          {target_date},{artifact_as_of},{validation},{as_of_status}
                   FROM horse_starts hs {join} ORDER BY {start_id}"""
        precedence = {POST_RACE_TARGET_SNAPSHOT: 5, "MISSING_SOURCE_ARTIFACT": 4,
                      "MISSING_SOURCE_AS_OF": 3, "MISSING_TARGET_DECISION_TIME": 2,
                      "PRE_RACE_TARGET_OPERATOR_ATTESTED": 1, "PRE_RACE_TARGET_PROVEN": 0}
        for row in conn.execute(query):
            if row[1] is None or row[2] is None:
                continue
            status = training_as_of_status(
                source_artifact_id=row[3], source_as_of_timestamp=row[6] or row[4],
                target_decision_timestamp=row[5], artifact_validation_status=row[7],
                artifact_as_of_status=row[8],
            )
            candidate = {"finish_position": int(row[2]), "training_as_of_status": status, "start_id": int(row[0])}
            current = result.get(int(row[1]))
            if current is None or precedence.get(status, 99) > precedence.get(str(current.get("training_as_of_status")), 99):
                result[int(row[1])] = candidate
    if _table_exists(conn, "starter_observations"):
        for row in conn.execute("SELECT race_id,post,finish_pos FROM starter_observations WHERE finish_pos IS NOT NULL"):
            # Only used as a fallback; stable entry identity remains mandatory.
            result.setdefault(-(int(row[0]) * 10000 + int(row[1] or 0)), {"finish_position": int(row[2])})
    return result


def _artifact_contracts(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    if not _table_exists(conn, "model_registry"):
        return contracts
    names = [item[1] for item in conn.execute("PRAGMA table_info(model_registry)")]
    # A registry can retain experimental rows whose files were intentionally
    # removed.  Prefer the artifact actually selected by a persisted runtime
    # decision for that family; only then fall back to newest registry entry.
    runtime_rows: list[Any] = []
    if _table_exists(conn, "score_runs"):
        runtime_rows = conn.execute(
            """SELECT mr.* FROM score_runs sr JOIN model_registry mr ON mr.model_id=sr.model_id
               ORDER BY sr.run_timestamp DESC, sr.created_at DESC, mr.model_id DESC"""
        ).fetchall()
    registry_rows = conn.execute("SELECT * FROM model_registry ORDER BY model_id DESC").fetchall()
    rows = runtime_rows + registry_rows
    for raw in rows:
        row = dict(zip(names, raw))
        family = str(row.get("model_family") or "")
        if family == "derby_override":
            family = "kentucky_derby"
        if family not in FAMILIES or family in contracts:
            continue
        artifact: Any = None
        path = Path(str(row.get("artifact_path"))) if row.get("artifact_path") else None
        if path and path.exists():
            try:
                artifact = load_model_artifact(path)
            except Exception as exc:  # an unreadable artifact is a readiness failure, not an audit crash
                row["artifact_load_error"] = type(exc).__name__
        active = active_feature_weights(artifact) if artifact is not None else []
        audit = calibration_audit_for_display(artifact) if artifact is not None else {}
        contracts[family] = {"registry": row, "artifact": artifact, "active_features": active, "calibration_audit": audit}
    return contracts


def _lineage_feature_valid(lineage: Mapping[str, Any], decision_timestamp: Any) -> tuple[bool, str]:
    status = str(lineage.get("status") or lineage.get("tier") or "UNKNOWN").upper()
    source = str(lineage.get("source_system") or lineage.get("source") or "unknown").strip().casefold()
    evidence = _number(lineage.get("evidence_count"), 0) or 0
    if status in INVALID_LINEAGE_STATUS:
        return False, "ACTIVE_FEATURE_PLACEHOLDER_OR_UNAVAILABLE"
    if source in INVALID_SOURCES:
        return False, "ACTIVE_FEATURE_SEEDED_OR_DEFAULTED"
    if source not in SUPPORTED_SOURCES:
        return False, "ACTIVE_FEATURE_UNKNOWN_OR_UNSUPPORTED_SOURCE"
    if evidence <= 0:
        return False, "ACTIVE_FEATURE_NO_EVIDENCE"
    if not _iso_before(lineage.get("as_of_max_date"), decision_timestamp):
        return False, "ACTIVE_FEATURE_AS_OF_INVALID"
    return True, ""


def _inventory(conn: sqlite3.Connection, contracts: Mapping[str, Mapping[str, Any]], *, family_filter: str | None = None, surface: str | None = None, distance_bucket: str | None = None, as_of_cutoff: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources, outcomes = _source_artifacts(conn), _outcomes(conn)
    fs_exists = _table_exists(conn, "feature_store")
    fs_cols = _columns(conn, "feature_store")
    fields = "fs.*" if fs_exists else "NULL AS feature_id"
    query = f"""SELECT rc.card_id,rc.card_date,rc.scheduled_post_time_utc,rc.surface,rc.distance_furlongs,
                       rc.race_class,rc.age_restriction,rc.field_size,rc.stakes_name,rc.race_number,
                       t.abbrev AS track,e.entry_id,e.horse_id,e.post_position,e.scratch_flag,{fields}
                FROM race_cards rc JOIN entries e ON e.card_id=rc.card_id
                JOIN tracks t ON t.track_id=rc.track_id
                {'LEFT JOIN feature_store fs ON fs.entry_id=e.entry_id AND fs.card_id=rc.card_id' if fs_exists else ''}
                ORDER BY rc.card_date,rc.card_id,e.post_position"""
    conn.row_factory = sqlite3.Row
    raw_rows = [dict(row) for row in conn.execute(query)]
    inventory: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for raw in raw_rows:
        family = classify_model_family(raw["surface"], raw["distance_furlongs"], raw["stakes_name"], raw["race_class"])
        bucket = "sprint" if _number(raw["distance_furlongs"], 0) < 8.5 else "route"
        if family_filter and family != family_filter:
            continue
        if surface and str(raw["surface"]).casefold() != surface.casefold():
            continue
        if distance_bucket and bucket != distance_bucket:
            continue
        decision = raw.get("scheduled_post_time_utc") or raw.get("card_date")
        if as_of_cutoff and not _iso_before(decision, as_of_cutoff):
            continue
        source_rows = sources.get(int(raw["card_id"]), [])
        source_ok = [item for item in source_rows if item.get("sha256") and item.get("parser_version") and str(item.get("validation_status")).upper() in {"PASS", "VALID", "VALIDATED"} and str(item.get("as_of_status")).upper() == "PROVEN" and _iso_before(item.get("as_of"), decision)]
        # A retained artifact is still reported even when none meets strict as-of rules.
        representative = source_rows[0] if source_rows else {}
        contract = contracts.get(family)
        active = list(contract.get("active_features", [])) if contract else []
        feature_row_exists = bool(fs_exists and raw.get("feature_id") is not None)
        lineage = runtime_lineage_by_feature(raw) if feature_row_exists else {}
        feature_reasons: list[str] = []
        active_valid = bool(active)
        for feature in active:
            name = str(feature["feature_name"])
            if not feature_row_exists or name not in fs_cols:
                active_valid = False; feature_reasons.append("ACTIVE_FEATURE_UNSUPPORTED_BY_FEATURE_STORE")
                continue
            if not _value_present(raw.get(name)):
                active_valid = False; feature_reasons.append("ACTIVE_FEATURE_VALUE_MISSING")
            valid, reason = _lineage_feature_valid(lineage.get(name, {}), decision)
            if not valid:
                active_valid = False; feature_reasons.append(reason)
        lineage_valid = feature_row_exists and bool(lineage) and active_valid
        outcome = outcomes.get(int(raw["entry_id"]))
        target_as_of = str((outcome or {}).get("training_as_of_status") or "MISSING_SOURCE_ARTIFACT")
        reason_codes: list[str] = []
        if not contract:
            reason_codes.append("MODEL_ARTIFACT_CONTRACT_MISSING")
        if not raw.get("scheduled_post_time_utc"):
            reason_codes.append("RACE_DECISION_TIMESTAMP_MISSING")
        if not source_rows:
            reason_codes.append("PRE_RACE_SOURCE_ARTIFACT_MISSING")
        elif not source_ok:
            reason_codes.append("SOURCE_PROVENANCE_OR_AS_OF_INVALID")
        if target_as_of == POST_RACE_TARGET_SNAPSHOT:
            reason_codes.append("POST_RACE_TARGET_SNAPSHOT_LEAKAGE")
        elif target_as_of not in VALID_TRAINING_AS_OF_STATUSES:
            reason_codes.append(f"TRAINING_AS_OF_{target_as_of}")
        if not outcome:
            reason_codes.append("OUTCOME_LINKAGE_MISSING")
        if not feature_row_exists:
            reason_codes.append("FEATURE_STORE_ROW_MISSING")
        if feature_row_exists and not lineage:
            reason_codes.append("FEATURE_LINEAGE_MISSING")
        if feature_row_exists and raw.get("build_ts") and not _iso_before(raw.get("build_ts"), decision):
            reason_codes.append("FEATURE_BUILT_POST_DECISION")
        reason_codes.extend(dict.fromkeys(feature_reasons))
        if raw.get("horse_id") is None or raw.get("entry_id") is None:
            reason_codes.append("STABLE_IDENTITY_MISSING")
        if int(raw.get("scratch_flag") or 0):
            reason_codes.append("SCRATCHED_ENTRY")
        eligible = not reason_codes
        row = {
            "card_id": raw["card_id"], "entry_id": raw["entry_id"], "horse_id": raw["horse_id"],
            "race_date": raw["card_date"], "decision_timestamp": decision, "surface": raw["surface"],
            "distance_furlongs": raw["distance_furlongs"], "distance_bucket": bucket, "race_class": raw["race_class"],
            "age_restriction": raw["age_restriction"], "track": raw["track"], "field_size": raw["field_size"],
            "model_family": family, "declared_race_family": "KENTUCKY_DERBY" if family == "kentucky_derby" else "ORDINARY_RACE",
            "outcome_linked": bool(outcome), "training_as_of_status": target_as_of, "pre_race_source_artifact_exists": bool(source_rows),
            "source_sha256": representative.get("sha256"), "parser_version": representative.get("parser_version"),
            "source_provider": representative.get("provider"), "source_as_of_timestamp": representative.get("as_of"),
            "source_as_of_status": representative.get("as_of_status"), "feature_store_row_exists": feature_row_exists,
            "feature_build_timestamp": raw.get("build_ts"), "feature_lineage_valid": lineage_valid,
            "active_features_evidence_valid": active_valid, "artifact_context": json.dumps({"model_id": contract.get("registry", {}).get("model_id"), "active_feature_count": len(active)} if contract else {}),
            "training_eligible": eligible, "exclusion_reason_codes": ";".join(dict.fromkeys(reason_codes)),
            "_raw": raw, "_lineage": lineage, "_outcome": outcome,
        }
        inventory.append(row)
        for code in dict.fromkeys(reason_codes):
            exclusions.append({"model_family": family, "card_id": raw["card_id"], "entry_id": raw["entry_id"], "horse_id": raw["horse_id"], "training_as_of_status": target_as_of, "reason_code": code, "detail": "strict training-row eligibility"})
    return inventory, exclusions


def propose_chronological_race_grouped_splits(rows: Iterable[Mapping[str, Any]], *, min_races_per_fold: int = MIN_RACES_PER_CALIBRATION_FOLD, min_wins_per_fold: int = MIN_WINS_PER_CALIBRATION_FOLD) -> list[dict[str, Any]]:
    """Create deterministic contiguous race-group folds, or a single insufficiency row."""
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("training_eligible"):
            grouped[int(row["card_id"])].append(row)
    races = sorted(grouped.values(), key=lambda group: (str(group[0]["race_date"]), int(group[0]["card_id"])))
    if len(races) < min_races_per_fold * 3:
        return [{"fold": "NONE", "race_count": len(races), "runner_count": sum(len(group) for group in races), "win_count": sum(1 for group in races for row in group if row.get("_outcome", {}).get("finish_position") == 1), "date_min": races[0][0]["race_date"] if races else None, "date_max": races[-1][0]["race_date"] if races else None, "race_ids": json.dumps([group[0]["card_id"] for group in races]), "split_status": "INSUFFICIENT", "reason": f"requires at least {min_races_per_fold * 3} eligible races for three chronological folds"}]
    base, remainder = divmod(len(races), 3)
    sizes = [base + (1 if index < remainder else 0) for index in range(3)]
    result, cursor = [], 0
    for name, size in zip(("TRAIN", "VALIDATION", "TEST"), sizes):
        group_slice = races[cursor:cursor + size]; cursor += size
        flat = [row for group in group_slice for row in group]
        wins = sum(1 for row in flat if row.get("_outcome", {}).get("finish_position") == 1)
        enough = len(group_slice) >= min_races_per_fold and wins >= min_wins_per_fold
        result.append({"fold": name, "race_count": len(group_slice), "runner_count": len(flat), "win_count": wins, "date_min": group_slice[0][0]["race_date"], "date_max": group_slice[-1][0]["race_date"], "race_ids": json.dumps([group[0]["card_id"] for group in group_slice]), "split_status": "VIABLE" if enough else "INSUFFICIENT", "reason": "" if enough else f"requires >= {min_races_per_fold} races and >= {min_wins_per_fold} wins per fold"})
    return result


def calibration_readiness(contract: Mapping[str, Any] | None, split_viable: bool) -> dict[str, Any]:
    if not contract:
        return {"artifact_status": "MODEL_ARTIFACT_MISSING", "calibration_status": "CALIBRATION_MISSING", "production_eligible": False, "method": None, "audit_timestamp": None, "reason": "no valid model artifact contract"}
    registry, artifact, audit = contract["registry"], contract.get("artifact"), contract.get("calibration_audit") or {}
    if artifact is None:
        return {"artifact_status": "MODEL_ARTIFACT_UNREADABLE", "calibration_status": "CALIBRATION_MISSING", "production_eligible": False, "method": None, "audit_timestamp": None, "reason": "artifact path is absent or unreadable"}
    method = (getattr(artifact, "config", {}) or {}).get("calibration_method") if not isinstance(artifact, dict) else (artifact.get("config", {}) or {}).get("calibration_method")
    timestamp = audit.get("audit_timestamp") or audit.get("calibration_audit_timestamp")
    audit_status = audit.get("calibration_status")
    if not method:
        status, reason = "CALIBRATION_MISSING", "calibration method missing"
    elif not timestamp:
        status, reason = "CALIBRATION_UNAUDITED", "calibration audit timestamp absent"
    elif not registry.get("calibration_artifact_path"):
        status, reason = "CALIBRATION_MISSING", "calibration artifact path missing"
    elif not split_viable:
        status, reason = "SPLIT_INSUFFICIENT", "no adequate chronological race-grouped out-of-sample folds"
    else:
        # This remains non-production until training/as-of evidence is independently complete.
        status, reason = "READY_FOR_TRAINING_AND_CALIBRATION", "artifact metadata is present; corpus eligibility still governs production"
    return {"artifact_status": "SEED_ONLY_NOT_PRODUCTION_ELIGIBLE" if "seed" in str(registry.get("version", "")).casefold() else "ARTIFACT_PRESENT_NOT_PRODUCTION_ELIGIBLE", "calibration_status": status, "production_eligible": False, "method": method, "audit_timestamp": timestamp, "audit_status": audit_status, "reason": reason}


def _write_csv(path: Path, columns: tuple[str, ...], rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def run_model_readiness_audit(db_path: Path, output_dir: Path, *, family: str | None = None, surface: str | None = None, distance_bucket: str | None = None, as_of_cutoff: str | None = None) -> dict[str, Any]:
    """Run the audit and write only the required, gitignored acceptance files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = open_readonly_connection(db_path)
    try:
        contracts = _artifact_contracts(conn)
        inventory, exclusions = _inventory(conn, contracts, family_filter=family, surface=surface, distance_bucket=distance_bucket, as_of_cutoff=as_of_cutoff)
    finally:
        conn.close()
    selected = [family] if family else list(FAMILIES)
    summaries: list[dict[str, Any]] = []; features: list[dict[str, Any]] = []; splits: list[dict[str, Any]] = []; calibrations: list[dict[str, Any]] = []; remediation: list[dict[str, Any]] = []
    for current in selected:
        rows = [row for row in inventory if row["model_family"] == current]
        eligible = [row for row in rows if row["training_eligible"]]
        race_total, race_eligible = len({row["card_id"] for row in rows}), len({row["card_id"] for row in eligible})
        split_rows = propose_chronological_race_grouped_splits(rows)
        split_viable = len(split_rows) == 3 and all(row["split_status"] == "VIABLE" for row in split_rows)
        for row in split_rows: splits.append({"model_family": current, **row})
        readiness = calibration_readiness(contracts.get(current), split_viable)
        # Corpus gates always precede artifact/calibration remediation.
        checks = [
            ("NO_AS_OF_VALID_HISTORICAL_CORPUS", not eligible or any(not row["pre_race_source_artifact_exists"] or row["source_as_of_status"] != "PROVEN" or row["training_as_of_status"] not in VALID_TRAINING_AS_OF_STATUSES for row in rows), "retained pre-race source provenance and target-race as-of proof are required"),
            ("OUTCOME_LINKAGE_INSUFFICIENT", not eligible or any(not row["outcome_linked"] for row in rows), "linked official results are required"),
            ("FEATURES_INSUFFICIENT", not eligible or any(not row["active_features_evidence_valid"] for row in rows), "every active feature requires valid evidence and lineage"),
            ("SPLIT_INSUFFICIENT", not split_viable, "chronological race-grouped calibration folds are inadequate"),
            ("MODEL_ARTIFACT_MISSING" if readiness["artifact_status"] == "MODEL_ARTIFACT_MISSING" else "TRAINED_ARTIFACT_NOT_PRODUCTION_ELIGIBLE", True, readiness["artifact_status"]),
            (readiness["calibration_status"], readiness["calibration_status"] != "READY_FOR_TRAINING_AND_CALIBRATION", readiness["reason"]),
        ]
        blockers = [name for name, blocked, _ in checks if blocked]
        for sequence, (name, blocked, detail) in enumerate(checks, 1):
            if blocked: remediation.append({"model_family": current, "sequence": sequence, "blocker": name, "detail": detail})
        dates = sorted(str(row["race_date"]) for row in rows if row.get("race_date"))
        surface_value = rows[0]["surface"] if rows else ("synthetic" if current == "synthetic" else current.split("_")[0] if "_" in current else "dirt")
        bucket_value = "derby" if current == "kentucky_derby" else (current.split("_")[-1] if "_" in current else "")
        summaries.append({"model_family": current, "surface": surface_value, "distance_bucket": bucket_value, "race_count_total": race_total, "race_count_training_eligible": race_eligible, "runner_count_total": len(rows), "runner_count_training_eligible": len(eligible), "win_count_training_eligible": sum(1 for row in eligible if row.get("_outcome", {}).get("finish_position") == 1), "date_min": dates[0] if dates else None, "date_max": dates[-1] if dates else None, "as_of_provenance_coverage": round(sum(bool(row["pre_race_source_artifact_exists"] and row["source_as_of_status"] == "PROVEN" and row["training_as_of_status"] in VALID_TRAINING_AS_OF_STATUSES) for row in rows) / len(rows), 6) if rows else 0, "outcome_linkage_coverage": round(sum(bool(row["outcome_linked"]) for row in rows) / len(rows), 6) if rows else 0, "feature_lineage_valid_coverage": round(sum(bool(row["feature_lineage_valid"]) for row in rows) / len(rows), 6) if rows else 0, "active_feature_valid_coverage": round(sum(bool(row["active_features_evidence_valid"]) for row in rows) / len(rows), 6) if rows else 0, "race_grouped_split_viable": split_viable, "artifact_status": readiness["artifact_status"], "calibration_status": readiness["calibration_status"], "production_eligible": False, "first_blocker": blockers[0] if blockers else "NONE", "ordered_blockers": ";".join(blockers)})
        contract = contracts.get(current); active = list(contract.get("active_features", [])) if contract else []
        if not active:
            features.append({"model_family": current, "feature_name": "__NO_ACTIVE_FEATURE_CONTRACT__", "effective_weight": 0, "race_family_applicable": current != "kentucky_derby", "historical_non_null_count": 0, "historical_evidence_valid_count": 0, "historical_as_of_proven_count": 0, "placeholder_count": 0, "unavailable_count": len(rows), "defaulted_count": 0, "unknown_count": len(rows), "source_provider_mix": "{}", "coverage_status": "INSUFFICIENT", "blocking_reason": "MODEL_ARTIFACT_CONTRACT_MISSING"})
        for item in active:
            name = str(item["feature_name"]); providers: Counter[str] = Counter(); non_null = valid = as_of = placeholder = unavailable = defaulted = unknown = 0
            for row in rows:
                raw, lineage = row["_raw"], row["_lineage"].get(name, {})
                if _value_present(raw.get(name)): non_null += 1
                status = str(lineage.get("status") or lineage.get("tier") or "UNKNOWN").upper(); source_name = str(lineage.get("source_system") or lineage.get("source") or "unknown")
                providers[source_name] += 1
                if status == "PLACEHOLDER": placeholder += 1
                if status in {"UNAVAILABLE", ""}: unavailable += 1
                if status == "DEFAULTED" or source_name.casefold() in INVALID_SOURCES: defaulted += 1
                if status == "UNKNOWN" or source_name.casefold() not in SUPPORTED_SOURCES: unknown += 1
                good, _ = _lineage_feature_valid(lineage, row["decision_timestamp"])
                valid += int(good and _value_present(raw.get(name)))
                as_of += int(_iso_before(lineage.get("as_of_max_date"), row["decision_timestamp"]))
            coverage = "READY" if rows and valid == len(rows) else "INSUFFICIENT"
            features.append({"model_family": current, "feature_name": name, "effective_weight": item["effective_weight"], "race_family_applicable": not (current != "kentucky_derby" and name in {"classic_distance_projection", "churchill_readiness", "jan_apr_improvement_curve", "derby_override_score"}), "historical_non_null_count": non_null, "historical_evidence_valid_count": valid, "historical_as_of_proven_count": as_of, "placeholder_count": placeholder, "unavailable_count": unavailable, "defaulted_count": defaulted, "unknown_count": unknown, "source_provider_mix": json.dumps(dict(sorted(providers.items())), sort_keys=True), "coverage_status": coverage, "blocking_reason": "" if coverage == "READY" else "ACTIVE_FEATURE_EVIDENCE_OR_AS_OF_COVERAGE_INSUFFICIENT"})
        registry = contract.get("registry", {}) if contract else {}
        calibrations.append({"model_family": current, "model_id": registry.get("model_id"), "model_name": registry.get("model_name"), "version": registry.get("version"), "artifact_path": registry.get("artifact_path"), "artifact_status": readiness["artifact_status"], "calibration_artifact_path": registry.get("calibration_artifact_path"), "calibration_method": readiness.get("method"), "calibration_audit_status": readiness.get("audit_status"), "calibration_audit_timestamp": readiness.get("audit_timestamp"), "out_of_sample_chronological_race_grouped_data": split_viable, "readiness_status": readiness["calibration_status"], "production_eligible": False, "reason": readiness["reason"]})
    paths = {"inventory": output_dir / "model_readiness_inventory.csv", "family_summary": output_dir / "model_readiness_family_summary.csv", "feature_coverage": output_dir / "model_readiness_feature_coverage.csv", "exclusions": output_dir / "model_readiness_exclusions.csv", "split_candidates": output_dir / "model_readiness_split_candidates.csv", "calibration_status": output_dir / "model_readiness_calibration_status.csv", "remediation_order": output_dir / "model_readiness_remediation_order.csv", "summary": output_dir / "model_readiness_summary.json"}
    _write_csv(paths["inventory"], INVENTORY_COLUMNS, inventory); _write_csv(paths["family_summary"], FAMILY_SUMMARY_COLUMNS, summaries); _write_csv(paths["feature_coverage"], FEATURE_COLUMNS, features); _write_csv(paths["exclusions"], EXCLUSION_COLUMNS, exclusions); _write_csv(paths["split_candidates"], SPLIT_COLUMNS, splits); _write_csv(paths["calibration_status"], CALIBRATION_COLUMNS, calibrations); _write_csv(paths["remediation_order"], REMEDIATION_COLUMNS, remediation)
    card73 = [row for row in inventory if int(row["card_id"]) == 73]
    card73_reminder = {"source_lineage_valid": bool(card73) and all(bool(row["_lineage"]) for row in card73), "twinspires_artifact_1": "unmatched and unproven-as-of; raw/provenance-only", "score_eligibility_remains_false": True, "probability_valid": False, "fair_odds_valid": False, "market_comparison_valid": False, "wager_valid": False, "expected_not_regression": True}
    summary = {"read_only": True, "training_or_calibration_performed": False, "feature_build_or_rescore_performed": False, "source_ingestion_or_reconciliation_performed": False, "database_path": str(db_path), "families": summaries, "card_73_operational_reminder": card73_reminder, "artifacts": {key: str(path) for key, path in paths.items()}}
    paths["summary"].write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return {"summary": summary, "paths": paths, "inventory": inventory, "family_summaries": summaries}
