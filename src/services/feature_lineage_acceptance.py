"""Read-only acceptance checks for persisted per-entry feature lineage."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.feature_lineage import (
    entry_details_feature_status_rows,
    model_diagnostics_feature_status_rows,
    runtime_lineage_by_feature,
)


DISPLAY_COLUMNS = ("Feature", "Value", "Status", "Source", "Evidence", "Reason", "Weight")
RUNTIME_BACKED_FEATURES = (
    "market_implied_prob", "distance_fit", "surface_fit", "work_readiness_score",
    "form_cycle_idx", "career_win_pct",
)
DK_ONLY_UNAVAILABLE_FEATURES = (
    "speed_last", "speed_best", "speed_avg", "beyer_last", "pace_fit_score",
)
INVALID_RUNTIME_STATUSES = {"PLACEHOLDER", "UNAVAILABLE", "UNKNOWN"}
INVALID_RUNTIME_SOURCES = {"", "unknown", "seeded/default"}


def read_feature_store_row(db_path: Path, card_id: int, entry_id: int) -> dict[str, Any]:
    """Read one persisted feature-store row without opening a writable database."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM feature_store WHERE card_id=? AND entry_id=?",
            (card_id, entry_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError(
            f"No feature_store row for card_id={card_id}, entry_id={entry_id}."
        )
    return dict(row)


def _plain_value(value: Any) -> Any:
    return None if isinstance(value, float) and math.isnan(value) else value


def normalize_panel_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalize either panel adapter into the stable acceptance CSV schema."""
    return {
        str(row["feature"]): {
            "Feature": str(row["feature"]),
            "Value": _plain_value(row.get("value")),
            "Status": row.get("tier"),
            "Source": row.get("source"),
            "Evidence": row.get("evidence"),
            "Reason": row.get("reason"),
            "Weight": row.get("importance", 0.0),
        }
        for row in rows
    }


def _problem(
    problems: list[dict[str, Any]],
    *,
    rule: str,
    feature: str,
    entry_row: Mapping[str, Any] | None,
    diagnostics_row: Mapping[str, Any] | None,
    persisted_lineage: Mapping[str, Any] | None,
) -> None:
    problems.append({
        "rule": rule,
        "feature": feature,
        "entry_details": dict(entry_row) if entry_row is not None else None,
        "model_diagnostics": dict(diagnostics_row) if diagnostics_row is not None else None,
        "persisted_lineage": dict(persisted_lineage) if persisted_lineage is not None else None,
    })


def cross_panel_lineage_mismatches(
    entry_rows: Mapping[str, Mapping[str, Any]],
    diagnostics_rows: Mapping[str, Mapping[str, Any]],
    persisted_lineage: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return exact status/source/evidence/reason discrepancies between panels."""
    problems: list[dict[str, Any]] = []
    for feature in sorted(set(entry_rows) & set(diagnostics_rows)):
        entry_row = entry_rows[feature]
        diagnostics_row = diagnostics_rows[feature]
        if any(
            entry_row[key] != diagnostics_row[key]
            for key in ("Status", "Source", "Evidence", "Reason")
        ):
            _problem(
                problems,
                rule="cross_panel_metadata_mismatch",
                feature=feature,
                entry_row=entry_row,
                diagnostics_row=diagnostics_row,
                persisted_lineage=persisted_lineage.get(feature),
            )
    return problems


def validate_persisted_feature_lineage(
    feature_row: Mapping[str, Any],
    *,
    card_id: int,
    entry_id: int,
    entry_importances: Mapping[str, float] | None = None,
    diagnostics_importances: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Validate both panel views from the exact same persisted lineage record.

    This function does not infer catalog provenance or alter the supplied row.
    It is intentionally suitable for unit tests and the read-only CLI.
    """
    entry_rows = normalize_panel_rows(entry_details_feature_status_rows(
        feature_row, importances=entry_importances,
    ))
    diagnostics_rows = normalize_panel_rows(model_diagnostics_feature_status_rows(
        feature_row, importances=diagnostics_importances,
    ))
    lineage = runtime_lineage_by_feature(feature_row)
    problems: list[dict[str, Any]] = []

    shared_features = sorted(set(entry_rows) & set(diagnostics_rows))
    problems.extend(cross_panel_lineage_mismatches(entry_rows, diagnostics_rows, lineage))

    for feature in RUNTIME_BACKED_FEATURES:
        row = entry_rows.get(feature)
        if row is None or row["Value"] is None:
            continue
        status = str(row["Status"] or "")
        source = str(row["Source"] or "").strip().lower()
        evidence = row["Evidence"]
        has_evidence = isinstance(evidence, (int, float)) and not isinstance(evidence, bool) and evidence > 0
        if status in INVALID_RUNTIME_STATUSES or source in INVALID_RUNTIME_SOURCES or not has_evidence:
            _problem(
                problems,
                rule=(
                    "non_null_runtime_backed_feature_requires_concrete_status_source_and_positive_evidence"
                ),
                feature=feature,
                entry_row=row,
                diagnostics_row=diagnostics_rows.get(feature),
                persisted_lineage=lineage.get(feature),
            )

    market_row = entry_rows.get("market_implied_prob")
    if market_row is not None and market_row["Value"] is not None:
        market_source_kind = str(feature_row.get("market_implied_prob_source") or "")
        market_reason = str(market_row["Reason"] or "").lower()
        if market_row["Status"] != "DERIVED":
            _problem(problems, rule="market_implied_prob_requires_derived_status", feature="market_implied_prob",
                     entry_row=market_row, diagnostics_row=diagnostics_rows.get("market_implied_prob"),
                     persisted_lineage=lineage.get("market_implied_prob"))
        if "draftkings_markdown" in str(feature_row.get("feature_source_mix") or "") and market_row["Source"] != "draftkings_markdown":
            _problem(problems, rule="dk_market_implied_prob_requires_draftkings_markdown_source", feature="market_implied_prob",
                     entry_row=market_row, diagnostics_row=diagnostics_rows.get("market_implied_prob"),
                     persisted_lineage=lineage.get("market_implied_prob"))
        if market_source_kind != "morning_line" or "morning-line" not in market_reason:
            _problem(problems, rule="market_implied_prob_requires_persisted_morning_line_provenance_note", feature="market_implied_prob",
                     entry_row=market_row, diagnostics_row=diagnostics_rows.get("market_implied_prob"),
                     persisted_lineage=lineage.get("market_implied_prob"))

    for feature in DK_ONLY_UNAVAILABLE_FEATURES:
        row = entry_rows.get(feature)
        if row is not None and row["Status"] != "UNAVAILABLE":
            _problem(problems, rule="dk_only_speed_or_pace_feature_must_remain_unavailable", feature=feature,
                     entry_row=row, diagnostics_row=diagnostics_rows.get(feature),
                     persisted_lineage=lineage.get(feature))

    return {
        "passed": not problems,
        "card_id": card_id,
        "entry_id": entry_id,
        "entry_details_rows": [entry_rows[name] for name in sorted(entry_rows)],
        "model_diagnostics_rows": [diagnostics_rows[name] for name in sorted(diagnostics_rows)],
        "shared_feature_count": len(shared_features),
        "mismatches": problems,
        "market_implied_prob_lineage": {
            "panel": entry_rows.get("market_implied_prob"),
            "persisted": lineage.get("market_implied_prob"),
            "input_source": feature_row.get("market_implied_prob_source"),
        },
    }


def write_acceptance_artifacts(
    result: Mapping[str, Any],
    feature_row: Mapping[str, Any],
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    """Write deterministic panel CSVs and a machine-readable acceptance summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"lineage_card_{result['card_id']}_entry_{result['entry_id']}"
    entry_path = output_dir / f"{suffix}_entry_details.csv"
    diagnostics_path = output_dir / f"{suffix}_model_diagnostics.csv"
    summary_path = output_dir / f"{suffix}_summary.json"
    for path, rows in ((entry_path, result["entry_details_rows"]), (diagnostics_path, result["model_diagnostics_rows"])):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=DISPLAY_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    summary = {
        "execution_timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "card_id": result["card_id"],
        "entry_id": result["entry_id"],
        "feature_store_build_ts": feature_row.get("build_ts"),
        "source_provider": (result["market_implied_prob_lineage"].get("panel") or {}).get("Source"),
        "source_mix": feature_row.get("feature_source_mix"),
        "market_implied_prob_source": feature_row.get("market_implied_prob_source"),
        "passed": result["passed"],
        "shared_feature_count": result["shared_feature_count"],
        "mismatches": result["mismatches"],
        "market_implied_prob_lineage": result["market_implied_prob_lineage"],
        "score_valid": False,
        "score_validation": "not evaluated",
        "wager_valid": False,
        "wager_validation": "not evaluated",
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return entry_path, diagnostics_path, summary_path
