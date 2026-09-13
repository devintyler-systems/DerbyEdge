"""Fail-closed, read-only score-eligibility audit for persisted race inputs."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.models.trainer import calibration_audit_for_display, load_model_artifact
from src.services.feature_lineage import runtime_lineage_by_feature


DERBY_ONLY_FEATURES = frozenset({
    "classic_distance_projection", "churchill_readiness", "jan_apr_improvement_curve",
    "derby_override_score",
})
INVALID_STATUS = frozenset({"PLACEHOLDER", "UNAVAILABLE", "UNKNOWN"})
INVALID_SOURCE = frozenset({"", "unknown", "seeded/default"})
SUPPORTED_SOURCES = frozenset({"draftkings_markdown", "canonical_db", "firstbet_pdf"})
CSV_COLUMNS = (
    "feature_name", "effective_weight", "feature_value", "lineage_status", "lineage_source",
    "evidence_count", "lineage_reason", "as_of_valid", "race_family_applicable",
    "missingness_policy_status", "calibration_compatible", "eligibility_result", "failure_reason",
)


def _ro_connection(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def resolve_race_context(db_path: Path, card_id: int, entry_id: int) -> dict[str, Any]:
    """Resolve persisted card/entry context without relying on a feature catalog."""
    conn = _ro_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT rc.card_id, rc.card_date, rc.race_number, rc.surface, rc.distance_yards,
                      rc.distance_furlongs, rc.race_class, rc.field_size, rc.stakes_name,
                      rc.scheduled_post_time_utc, e.entry_id, fs.build_ts, fs.feature_source_mix
                 FROM race_cards rc JOIN entries e ON e.card_id=rc.card_id
                 JOIN feature_store fs ON fs.card_id=rc.card_id AND fs.entry_id=e.entry_id
                 WHERE rc.card_id=? AND e.entry_id=?""",
            (card_id, entry_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError(f"No persisted card/entry/feature row for card_id={card_id}, entry_id={entry_id}.")
    context = dict(row)
    derby_text = " ".join(str(context.get(key) or "").lower() for key in ("stakes_name", "race_class"))
    context["race_family_classification"] = "KENTUCKY_DERBY" if "kentucky derby" in derby_text else "NORMAL_RACE"
    return context


def resolve_scoring_context(db_path: Path, card_id: int, entry_id: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Resolve current feature vector plus the latest persisted scoring decision/artifact."""
    conn = _ro_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        feature = conn.execute("SELECT * FROM feature_store WHERE card_id=? AND entry_id=?", (card_id, entry_id)).fetchone()
        decision = conn.execute(
            "SELECT * FROM score_runs WHERE card_id=? ORDER BY run_timestamp DESC, created_at DESC LIMIT 1", (card_id,)
        ).fetchone()
        if decision is None:
            raise LookupError(f"No persisted score decision for card_id={card_id}.")
        model = conn.execute("SELECT * FROM model_registry WHERE model_id=?", (decision["model_id"],)).fetchone()
    finally:
        conn.close()
    if feature is None:
        raise LookupError(f"No feature_store row for card_id={card_id}, entry_id={entry_id}.")
    if model is None:
        raise LookupError(f"No model_registry row for model_id={decision['model_id']}.")
    return dict(feature), dict(decision), dict(model)


def active_feature_weights(artifact: object) -> list[dict[str, Any]]:
    """Extract nonzero effective weights from the artifact's persisted group config."""
    config = artifact.get("config", {}) if isinstance(artifact, dict) else getattr(artifact, "config", {})
    active: dict[str, dict[str, Any]] = {}
    for group_name, group in (config.get("feature_groups", {}) or {}).items():
        group_weight = float(group.get("group_weight", 0.0) or 0.0)
        for feature_name, feature_weight in (group.get("features", {}) or {}).items():
            effective = group_weight * float(feature_weight or 0.0)
            if effective:
                item = active.setdefault(feature_name, {"feature_name": feature_name, "effective_weight": 0.0, "groups": []})
                item["effective_weight"] += effective
                item["groups"].append(group_name)
    return sorted((item for item in active.values() if item["effective_weight"] != 0), key=lambda item: item["feature_name"])


def _artifact_value(artifact: object, name: str, default: Any = None) -> Any:
    return artifact.get(name, default) if isinstance(artifact, dict) else getattr(artifact, name, default)


def _policy_status(artifact: object, calibration: Mapping[str, Any], feature_name: str) -> str:
    config = _artifact_value(artifact, "config", {}) or {}
    policy = config.get("missingness_policy") if isinstance(config, dict) else None
    allowed = policy.get("allowed_features", ()) if isinstance(policy, dict) else ()
    audited = isinstance(policy, dict) and bool(policy.get("version")) and feature_name in allowed and (
        calibration.get("missingness_policy_status") == "covered"
        and calibration.get("missingness_policy_version") == policy.get("version")
    )
    return "AUDITED_ALLOWED" if audited else "NO_AUDITED_POLICY"


def evaluate_score_eligibility(
    *,
    race_context: Mapping[str, Any],
    feature_row: Mapping[str, Any],
    decision: Mapping[str, Any],
    model: Mapping[str, Any],
    artifact: object,
) -> dict[str, Any]:
    """Fail closed for unsupported, unavailable, stale, or inapplicable active inputs."""
    calibration = calibration_audit_for_display(artifact)
    decision_timestamp = decision.get("run_timestamp") or decision.get("created_at")
    calibration_status = calibration.get("calibration_status")
    calibration_timestamp = calibration.get("audit_timestamp") or calibration.get("calibration_audit_timestamp")
    calibration_valid = bool(
        calibration_status in {"soft_market_anchor", "approved", "valid"}
        and calibration_timestamp
        and _artifact_value(artifact, "artifact_schema_version")
        and model.get("feature_schema_version")
    )
    global_reasons: list[str] = []
    if not decision_timestamp:
        global_reasons.append("scoring_decision_timestamp_unresolved")
    if not calibration_valid:
        global_reasons.append("calibration_unavailable_or_unaudited")
    lineage = runtime_lineage_by_feature(feature_row)
    rows: list[dict[str, Any]] = []
    for item in active_feature_weights(artifact):
        name = item["feature_name"]
        value = feature_row.get(name)
        if isinstance(value, float) and (math.isnan(value) or not math.isfinite(value)):
            value = None
        meta = lineage.get(name, {})
        status = meta.get("status") or meta.get("tier") or "UNKNOWN"
        source = meta.get("source_system") or meta.get("source") or "unknown"
        evidence = meta.get("evidence_count", 0)
        reason = meta.get("fallback_reason")
        as_of = meta.get("as_of_max_date")
        as_of_valid = bool(decision_timestamp and as_of and str(as_of) <= str(decision_timestamp)[:10])
        applicable = not (
            race_context.get("race_family_classification") == "NORMAL_RACE" and name in DERBY_ONLY_FEATURES
        )
        policy_status = _policy_status(artifact, calibration, name)
        failures: list[str] = []
        unavailable = value is None or status in INVALID_STATUS or str(source).strip().lower() in INVALID_SOURCE or not isinstance(evidence, (int, float)) or evidence <= 0
        if unavailable:
            if policy_status != "AUDITED_ALLOWED":
                failures.append("active_input_unavailable_or_defaulted_without_audited_policy")
        if str(source).strip().lower() not in SUPPORTED_SOURCES:
            failures.append("unsupported_or_unknown_runtime_source")
        if not as_of_valid:
            failures.append("source_evidence_missing_or_not_proven_as_of_decision_timestamp")
        if not applicable:
            failures.append("derby_only_feature_active_for_normal_race")
        if not calibration_valid:
            failures.append("calibration_unavailable_or_incompatible")
        rows.append({
            "feature_name": name, "effective_weight": round(item["effective_weight"], 8), "feature_value": value,
            "lineage_status": status, "lineage_source": source, "evidence_count": evidence,
            "lineage_reason": reason, "as_of_valid": as_of_valid, "race_family_applicable": applicable,
            "missingness_policy_status": policy_status, "calibration_compatible": calibration_valid,
            "eligibility_result": "PASS" if not failures else "FAIL", "failure_reason": ";".join(failures),
        })
    invalid_rows = [row for row in rows if row["eligibility_result"] == "FAIL"]
    reasons = list(dict.fromkeys(global_reasons + [reason for row in invalid_rows for reason in row["failure_reason"].split(";") if reason]))
    score_valid = not invalid_rows and not global_reasons
    return {
        "race_context": dict(race_context), "artifact": {
            "model_id": model.get("model_id"), "model_name": model.get("model_name"), "version": model.get("version"),
            "artifact_path": model.get("artifact_path"), "race_type_key": _artifact_value(artifact, "race_type_key"),
            "training_rows": _artifact_value(artifact, "training_rows"), "artifact_schema_version": _artifact_value(artifact, "artifact_schema_version"),
            "training_scope": {"target_race_type_key": model.get("target_race_type_key"), "training_window_start": model.get("training_window_start"), "training_window_end": model.get("training_window_end")},
        },
        "calibration": {"identifier": (_artifact_value(artifact, "config", {}) or {}).get("calibration_method"), "status": calibration_status, "audit_timestamp": calibration_timestamp, "audit": calibration},
        "decision_timestamp": decision_timestamp, "active_rows": rows, "invalid_rows": invalid_rows,
        "derby_isolation_valid": not any("derby_only_feature_active_for_normal_race" in row["failure_reason"] for row in rows),
        "as_of_valid": not any("source_evidence_missing_or_not_proven_as_of_decision_timestamp" in row["failure_reason"] for row in rows),
        "score_valid": score_valid, "reason_codes": reasons,
    }


def write_score_eligibility_artifacts(result: Mapping[str, Any], output_dir: Path) -> tuple[Path, Path, Path | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    context = result["race_context"]
    stem = f"score_eligibility_card_{context['card_id']}_entry_{context['entry_id']}"
    active_path, summary_path = output_dir / f"{stem}_active_features.csv", output_dir / f"{stem}_summary.json"
    failures = result["invalid_rows"]
    failure_path = output_dir / f"{stem}_failures.csv" if failures else None
    for path, rows in ((active_path, result["active_rows"]), (failure_path, failures)):
        if path is None:
            continue
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader(); writer.writerows(rows)
    summary = {
        "execution_timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "card_id": context["card_id"], "entry_id": context["entry_id"], "race_family_classification": context["race_family_classification"],
        "artifact": result["artifact"], "calibration": result["calibration"], "decision_timestamp": result["decision_timestamp"],
        "active_feature_count": len(result["active_rows"]), "invalid_active_feature_count": len(failures),
        "derby_isolation_result": result["derby_isolation_valid"], "as_of_result": result["as_of_valid"],
        "score_valid": result["score_valid"], "fair_odds_valid": False, "market_comparison_valid": False, "wager_valid": False,
        "reason_codes": result["reason_codes"], "read_only": True,
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return active_path, summary_path, failure_path
