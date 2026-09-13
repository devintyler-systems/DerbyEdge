"""Read-only source coverage audit; it cannot score, hydrate runtime features, or rebuild rows."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.models.trainer import load_model_artifact  # noqa: E402
from src.services.feature_lineage import runtime_lineage_by_feature  # noqa: E402
from src.services.score_eligibility import DERBY_ONLY_FEATURES, active_feature_weights, evaluate_score_eligibility, resolve_race_context, resolve_scoring_context  # noqa: E402
from src.utils.db import DB_PATH  # noqa: E402


COLUMNS = ("feature_name","effective_weight","race_family_applicable","dk_status","dk_source","dk_evidence_count",
           "twinspires_status","twinspires_source","twinspires_evidence_count","twinspires_as_of_status",
           "combined_coverage_status","score_eligible","failure_reason")
# These are deliberately narrow semantic aliases.  There is no alias for
# Beyer, pace fit, pace pressure, sectional pace, or finish energy.
TWINSPIRES_MODEL_ALIASES = {"speed_last": "twinspires_speed_last"}


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _finite(value: Any) -> bool:
    return value is not None and str(value).lower() not in {"nan", "none", ""}


def build_audit(db_path: Path, card_id: int, source_provider: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Return coverage rows and source candidates using only a read-only DB handle."""
    conn = _ro(db_path)
    try:
        feature_rows = [dict(row) for row in conn.execute("SELECT * FROM feature_store WHERE card_id=?", (card_id,)).fetchall()]
        if not feature_rows:
            raise LookupError(f"No persisted feature rows for card_id={card_id}")
        # The active runtime decision identifies the deployed artifact contract;
        # this reads it but does not invoke prediction.
        first = feature_rows[0]
        race, feature, decision, model = resolve_race_context(db_path, card_id, int(first["entry_id"])), *resolve_scoring_context(db_path, card_id, int(first["entry_id"]))
        # tuple unpack above makes race + (feature, decision, model)
        artifact = load_model_artifact(Path(str(model.get("artifact_path") or "")))
        score = evaluate_score_eligibility(race_context=race, feature_row=feature, decision=decision, model=model, artifact=artifact)
        candidates: list[dict[str, Any]] = []
        if _table(conn, "source_feature_candidates") and _table(conn, "source_artifacts"):
            candidates = [dict(row) for row in conn.execute("""SELECT c.*, h.name AS horse_name FROM source_feature_candidates c
                JOIN source_observations o ON o.observation_id=c.observation_id
                JOIN source_artifacts a ON a.artifact_id=o.artifact_id
                JOIN entries e ON e.entry_id=c.entry_id JOIN horses h ON h.horse_id=e.horse_id
                WHERE a.card_id=? AND c.source_provider=? ORDER BY c.entry_id,c.feature_name""", (card_id, source_provider)).fetchall()]
    finally:
        conn.close()
    lineage_rows = [runtime_lineage_by_feature(row) for row in feature_rows]
    coverage: list[dict[str, Any]] = []
    race_normal = race.get("race_family_classification") == "NORMAL_RACE"
    for active in active_feature_weights(artifact):
        name = active["feature_name"]
        metas = [lineage.get(name, {}) for lineage in lineage_rows]
        values = [row.get(name) for row in feature_rows]
        dk_backed = [meta for meta, value in zip(metas, values) if _finite(value) and str(meta.get("source_system") or meta.get("source") or "").lower() == "draftkings_markdown" and int(meta.get("evidence_count") or 0) > 0]
        dk_status = "SOURCE_BACKED" if dk_backed else ("UNAVAILABLE" if not any(_finite(v) for v in values) else "UNKNOWN")
        alias = TWINSPIRES_MODEL_ALIASES.get(name)
        source_rows = [row for row in candidates if row["feature_name"] == alias] if alias else []
        ts_count = sum(int(row.get("evidence_count") or 0) for row in source_rows)
        statuses = {str(row.get("as_of_status") or "UNKNOWN") for row in source_rows}
        if not source_rows:
            ts_status, ts_asof = "UNSUPPORTED", "UNKNOWN"
        elif statuses == {"PROVEN"}:
            ts_status, ts_asof = "SOURCE_BACKED", "PROVEN"
        elif "UNPROVEN" in statuses:
            ts_status, ts_asof = "PRESENT_AS_OF_UNPROVEN", "UNPROVEN"
        else:
            ts_status, ts_asof = "INVALID", "INVALID"
        applicable = not (race_normal and name in DERBY_ONLY_FEATURES)
        if not applicable:
            combined = "DERBY_ONLY_INAPPLICABLE"
        elif dk_backed and ts_status == "SOURCE_BACKED":
            combined = "SOURCE_CONFLICTED"  # no silent provider coalescing
        elif dk_backed:
            combined = "DK_SOURCE_BACKED"
        elif ts_status == "SOURCE_BACKED":
            combined = "TWINSPIRES_CANDIDATE_ONLY_NOT_RUNTIME_HYDRATED"
        elif ts_status == "PRESENT_AS_OF_UNPROVEN":
            combined = "TWINSPIRES_AS_OF_UNPROVEN"
        elif dk_status == "UNAVAILABLE":
            combined = "UNAVAILABLE_DEFAULTED"
        else:
            combined = "UNSUPPORTED"
        invalid = next((row for row in score["active_rows"] if row["feature_name"] == name), None)
        coverage.append({"feature_name": name, "effective_weight": active["effective_weight"], "race_family_applicable": applicable,
                         "dk_status": dk_status, "dk_source": "draftkings_markdown" if dk_backed else "",
                         "dk_evidence_count": sum(int(item.get("evidence_count") or 0) for item in dk_backed),
                         "twinspires_status": ts_status, "twinspires_source": source_provider if source_rows else "",
                         "twinspires_evidence_count": ts_count, "twinspires_as_of_status": ts_asof,
                         "combined_coverage_status": combined, "score_eligible": False,
                         "failure_reason": (invalid or {}).get("failure_reason") or "coverage audit never grants runtime eligibility"})
    summary = {"execution_timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "card_id": card_id,
               "source_provider": source_provider, "read_only": True, "active_model_feature_count": len(coverage),
               "feature_coverage": {"dk_source_backed": sum(r["combined_coverage_status"] == "DK_SOURCE_BACKED" for r in coverage),
                                    "twinspires_valid_candidate": sum(r["twinspires_status"] == "SOURCE_BACKED" for r in coverage),
                                    "twinspires_as_of_unproven": sum(r["twinspires_status"] == "PRESENT_AS_OF_UNPROVEN" for r in coverage),
                                    "unsupported_or_unavailable": sum(r["combined_coverage_status"] in {"UNSUPPORTED", "UNAVAILABLE_DEFAULTED"} for r in coverage)},
               "as_of_valid_coverage": False, "model_score_eligibility": False, "calibration_eligibility": False,
               "score_valid": False, "fair_odds_valid": False, "market_comparison_valid": False, "wager_valid": False,
               "runtime_score_eligibility_reasons": score.get("reason_codes", [])}
    validation = {"card_id": card_id, "source_provider": source_provider, "candidate_count": len(candidates),
                  "candidate_as_of_statuses": sorted({str(row.get("as_of_status")) for row in candidates}), "read_only": True}
    return coverage, candidates, summary, validation


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only active-model source coverage audit")
    parser.add_argument("--card-id", type=int, required=True)
    parser.add_argument("--source-provider", required=True)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "acceptance")
    args = parser.parse_args()
    coverage, candidates, summary, validation = build_audit(args.db_path, args.card_id, args.source_provider)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"twinspires_card_{args.card_id}"
    candidate_path = args.output_dir / f"{stem}_feature_candidates.csv"
    coverage_path = args.output_dir / f"{stem}_model_coverage.csv"
    summary_path = args.output_dir / f"{stem}_summary.json"
    # Ingestion writes the first two output files.  Preserve them if present;
    # audit itself is read-only against DerbyEdge state.
    with candidate_path.open("w", newline="", encoding="utf-8") as handle:
        fields = sorted({key for row in candidates for key in row}) if candidates else ["entry_id", "feature_name"]
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(candidates)
    with coverage_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS); writer.writeheader(); writer.writerows(coverage)
    summary_path.write_text(json.dumps({**summary, "source_validation": validation}, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"model_coverage_csv={coverage_path}\nfeature_candidates_csv={candidate_path}\nsummary_json={summary_path}")
    print("score_valid=false fair_odds_valid=false market_comparison_valid=false wager_valid=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
