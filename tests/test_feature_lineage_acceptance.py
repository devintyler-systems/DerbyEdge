"""Read-only acceptance-gate tests independent of Streamlit/browser state."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from src.services.feature_lineage_acceptance import (
    cross_panel_lineage_mismatches,
    normalize_panel_rows,
    validate_persisted_feature_lineage,
    write_acceptance_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]


def _saratoga_654_row() -> dict[str, object]:
    statuses = {
        "market_implied_prob": ("DERIVED", "draftkings_markdown", 1,
            "derived from persisted morning-line input; not current tote/live market odds"),
        "distance_fit": ("DERIVED", "draftkings_markdown", 3, None),
        "surface_fit": ("DERIVED", "draftkings_markdown", 3, None),
        "work_readiness_score": ("DERIVED", "draftkings_markdown", 2, None),
        "form_cycle_idx": ("DERIVED", "draftkings_markdown", 3, None),
        "career_win_pct": ("DERIVED", "draftkings_markdown", 3, None),
        "speed_last": ("UNAVAILABLE", "seeded/default", 0, "official speed or sectional source is unavailable"),
        "speed_best": ("UNAVAILABLE", "seeded/default", 0, "official speed or sectional source is unavailable"),
        "speed_avg": ("UNAVAILABLE", "seeded/default", 0, "official speed or sectional source is unavailable"),
        "beyer_last": ("UNAVAILABLE", "seeded/default", 0, "official speed or sectional source is unavailable"),
        "pace_fit_score": ("UNAVAILABLE", "canonical_db", 0, "pace calls/sectionals are unavailable; no run-style evidence"),
    }
    values = {
        "market_implied_prob": 0.047619, "distance_fit": 0.5, "surface_fit": 0.5,
        "work_readiness_score": 0.5, "form_cycle_idx": 0.25, "career_win_pct": 0.125,
        "speed_last": None, "speed_best": None, "speed_avg": None, "beyer_last": None,
        "pace_fit_score": None,
    }
    lineage = [
        {"feature_name": name, "value": values[name], "status": status, "tier": status,
         "source_system": source, "source": source, "evidence_count": evidence,
         "fallback_reason": reason}
        for name, (status, source, evidence, reason) in statuses.items()
    ]
    return {
        "card_id": 73, "entry_id": 654, "horse_name": "Magnum's Macrobrst",
        "build_ts": "2026-09-06T00:00:00Z", "feature_source_mix": "draftkings_markdown",
        "market_implied_prob_source": "morning_line", **values,
        "feature_lineage_json": json.dumps(lineage, sort_keys=True),
    }


def test_saratoga_card_73_entry_654_lineage_acceptance_passes_and_writes_artifacts(tmp_path):
    row = _saratoga_654_row()
    result = validate_persisted_feature_lineage(row, card_id=73, entry_id=654)

    assert result["passed"]
    entry_path, diagnostics_path, summary_path = write_acceptance_artifacts(result, row, tmp_path)
    assert entry_path.read_text(encoding="utf-8") == diagnostics_path.read_text(encoding="utf-8")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["passed"] is True
    assert summary["source_provider"] == "draftkings_markdown"
    assert summary["market_implied_prob_lineage"]["input_source"] == "morning_line"
    assert summary["score_valid"] is False
    assert summary["wager_valid"] is False


def test_stale_non_null_market_lineage_fails_acceptance():
    row = _saratoga_654_row()
    lineage = json.loads(str(row["feature_lineage_json"]))
    market = next(item for item in lineage if item["feature_name"] == "market_implied_prob")
    market.update({"status": "PLACEHOLDER", "tier": "PLACEHOLDER", "source_system": "seeded/default",
                   "source": "seeded/default", "evidence_count": 0, "fallback_reason": None})
    row["feature_lineage_json"] = json.dumps(lineage)

    result = validate_persisted_feature_lineage(row, card_id=73, entry_id=654)

    assert not result["passed"]
    assert any(issue["feature"] == "market_implied_prob" for issue in result["mismatches"])
    assert any(
        issue["rule"] == "non_null_runtime_backed_feature_requires_concrete_status_source_and_positive_evidence"
        for issue in result["mismatches"]
    )


def test_cross_panel_mismatch_is_detected_with_both_rows():
    row = _saratoga_654_row()
    result = validate_persisted_feature_lineage(row, card_id=73, entry_id=654)
    entry_rows = {item["Feature"]: item for item in result["entry_details_rows"]}
    diagnostics_rows = normalize_panel_rows([])
    diagnostics_rows.update({name: dict(item) for name, item in entry_rows.items()})
    diagnostics_rows["market_implied_prob"]["Source"] = "seeded/default"

    issues = cross_panel_lineage_mismatches(entry_rows, diagnostics_rows, {})

    assert issues[0]["rule"] == "cross_panel_metadata_mismatch"
    assert issues[0]["entry_details"]["Source"] == "draftkings_markdown"
    assert issues[0]["model_diagnostics"]["Source"] == "seeded/default"


def test_acceptance_cli_reads_database_read_only_and_passes(tmp_path):
    row = _saratoga_654_row()
    db_path = tmp_path / "fixture.db"
    conn = sqlite3.connect(db_path)
    columns = ", ".join(f'"{name}" TEXT' for name in row)
    conn.execute(f"CREATE TABLE feature_store ({columns})")
    conn.execute(
        f"INSERT INTO feature_store ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
        [str(value) if value is not None else None for value in row.values()],
    )
    conn.commit()
    conn.close()
    before = db_path.read_bytes()

    output_dir = tmp_path / "acceptance"
    completed = subprocess.run(
        [sys.executable, "scripts/validate_feature_lineage_acceptance.py", "--card-id", "73", "--entry-id", "654",
         "--db-path", str(db_path), "--output-dir", str(output_dir),
         "--model-path", str(tmp_path / "missing-model.pkl")],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "market_implied_prob is morning-line-derived" in completed.stdout
    assert "PASS shared_feature_count=" in completed.stdout
    assert (output_dir / "lineage_card_73_entry_654_summary.json").exists()
    assert db_path.read_bytes() == before
