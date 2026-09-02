from __future__ import annotations

from pathlib import Path

from src.ingest.firstbet_pdf import parse_firstbet_text
from src.ingest.run_state import (
    DataQuality,
    RunMode,
    feature_degeneracy_warnings,
    resolve_mode_with_feature_checks,
)


FIXTURE = Path(__file__).parent / "fixtures" / "Saratoga_R8_9-2-26.txt"


def test_saratoga_coverage_is_source_truthful():
    _, audit = parse_firstbet_text(
        FIXTURE.read_text(encoding="utf-8"),
        filename="Saratoga_R8_9-2-26.pdf",
        sha256="fixture",
        uploaded_at_utc="2026-09-02T20:24:00Z",
    )
    coverage = audit["feature_coverage"]
    assert audit["run_mode"] == RunMode.MODEL_READY_LIMITED.value
    assert coverage["recent_form"] == 1.0
    assert coverage["run_style_proxy"] == 0.89
    assert coverage["off_track_evidence"] == 0.44
    assert coverage["speed_figures"] == 0.0
    assert coverage["fractional_pace"] == 0.0
    assert coverage["workouts"] == 0.0
    assert coverage["live_odds"] == 0.0


def test_constant_nontrivial_features_emit_warning_and_block_forecast():
    rows = [
        {"pace_fit": 0.5, "form": 0.5, "surface_distance_fit": 0.5},
        {"pace_fit": 0.5, "form": 0.5, "surface_distance_fit": 0.5},
    ]
    warnings = feature_degeneracy_warnings(rows, rows[0].keys())
    assert len(warnings) == 3
    assert all(warning.startswith("FEATURE_DEGENERACY_WARNING") for warning in warnings)

    quality = DataQuality(
        entries_parsed=2,
        field_size_declared=2,
        entries_with_pp_history=2,
        starter_match_rate=1.0,
        race_metadata_complete=True,
        has_morning_lines=True,
        has_live_odds=False,
        required_model_features_complete=False,
    )
    mode, reasons = resolve_mode_with_feature_checks(quality, rows)
    assert mode == RunMode.BLOCKED
    assert any("core engineered features are degenerate" in reason for reason in reasons)

