"""Regression coverage for the two per-entry feature-status display paths."""

from __future__ import annotations

import json

from src.services.feature_lineage import (
    entry_details_feature_status_rows,
    model_diagnostics_feature_status_rows,
)


def test_entry_details_and_model_diagnostics_share_runtime_lineage_statuses():
    """Static catalog defaults must never override persisted runtime lineage."""
    lineage = [
        {
            "feature_name": "dk_history_start_count", "status": "SOURCE_BACKED",
            "source_system": "draftkings_markdown", "evidence_count": 3,
            "fallback_reason": None,
        },
        {
            "feature_name": "distance_fit_n", "status": "DERIVED",
            "source_system": "draftkings_markdown", "evidence_count": 3,
            "fallback_reason": None,
        },
        {
            "feature_name": "speed_best", "status": "UNAVAILABLE",
            "source_system": "seeded/default", "evidence_count": 0,
            "fallback_reason": "official speed or sectional source is unavailable",
        },
    ]
    feature_row = {
        "entry_id": 42,
        "horse_name": "Fixture Horse",
        "dk_history_start_count": 3,
        "distance_fit_n": 3,
        "speed_best": None,
        "feature_lineage_json": json.dumps(lineage),
    }
    importances = {"dk_history_start_count": 0.1, "distance_fit_n": 0.2, "speed_best": 0.3}

    entry_rows = {
        row["feature"]: row
        for row in entry_details_feature_status_rows(feature_row, importances=importances)
    }
    diagnostic_rows = {
        row["feature"]: row
        for row in model_diagnostics_feature_status_rows(feature_row, importances=importances)
    }

    for feature_name in ("dk_history_start_count", "distance_fit_n", "speed_best"):
        assert tuple(entry_rows[feature_name][key] for key in ("tier", "source", "reason")) == tuple(
            diagnostic_rows[feature_name][key] for key in ("tier", "source", "reason")
        )
    assert entry_rows["distance_fit_n"]["tier"] == "DERIVED"
    assert entry_rows["speed_best"]["tier"] == "UNAVAILABLE"


def test_valid_market_lineage_is_never_replaced_by_default_display_metadata():
    """A persisted ML-derived value must retain its concrete runtime lineage."""
    feature_row = {
        "entry_id": 654,
        "market_implied_prob": 0.047619,
        "feature_lineage_json": json.dumps([{
            "feature_name": "market_implied_prob",
            "status": "DERIVED",
            "source_system": "draftkings_markdown",
            "evidence_count": 1,
            "fallback_reason": None,
        }]),
    }
    for builder in (
        entry_details_feature_status_rows,
        model_diagnostics_feature_status_rows,
    ):
        market_row = next(row for row in builder(feature_row) if row["feature"] == "market_implied_prob")
        assert market_row["value"] == 0.047619
        assert market_row["tier"] == "DERIVED"
        assert market_row["source"] == "draftkings_markdown"
        assert market_row["evidence"] == 1
        assert market_row["reason"] is None


def test_malformed_or_legacy_lineage_is_unknown_not_static_catalog_provenance():
    """Missing runtime JSON is explicitly unverifiable, even for a non-null value."""
    feature_row = {
        "entry_id": 654,
        "market_implied_prob": 0.047619,
        "feature_lineage_json": "not valid JSON",
    }
    for builder in (
        entry_details_feature_status_rows,
        model_diagnostics_feature_status_rows,
    ):
        market_row = next(row for row in builder(feature_row) if row["feature"] == "market_implied_prob")
        assert (market_row["tier"], market_row["source"], market_row["evidence"], market_row["reason"]) == (
            "UNKNOWN", "unknown", 0, "runtime lineage unavailable",
        )
