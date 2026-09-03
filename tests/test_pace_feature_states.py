from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.builder import (
    _fill_race_level_features,
    classify_1stbet_trip_comments,
)
from src.ingest.run_state import DataQuality, RunMode, resolve_mode_with_feature_checks
from src.services.feature_state import verify_feature_frame
from src.services.model_independence import pre_market_signal_probabilities


def _pace_frame(styles: list[str | None]) -> pd.DataFrame:
    size = len(styles)
    return pd.DataFrame({
        "run_style_bucket": styles,
        "market_implied_prob": np.linspace(0.08, 0.20, size),
        "speed_last": np.linspace(70, 80, size),
        "publicness_score": np.linspace(0.8, 1.2, size),
        "class_level": np.linspace(1.0, 2.0, size),
        "form_cycle_idx": np.linspace(0.2, 0.9, size),
        "distance_fit": np.linspace(0.1, 0.8, size),
        "surface_fit": np.linspace(0.2, 0.7, size),
    })


def _verification_config() -> dict:
    return {
        "feature_groups": {
            "form_class": {
                "group_weight": 0.5,
                "features": {"form_cycle_idx": 1.0},
            },
            "distance_surface": {
                "group_weight": 0.3,
                "features": {"distance_fit": 1.0},
            },
            "race_shape": {
                "group_weight": 0.2,
                "features": {"pace_fit_score": 1.0},
            },
        },
    }


def test_trip_comment_classification_uses_ordered_evidence_not_first_match():
    # The first term is closer evidence, but front has two terms and wins.
    style, evidence_count = classify_1stbet_trip_comments(
        ["rallied late, set pace, led throughout"]
    )
    assert style == "front"
    assert evidence_count == 2


def test_pace_partial_keeps_unknown_runners_null():
    result = _fill_race_level_features(
        _pace_frame(["front", "closer", None, None, None]), derby_active=False
    )

    assert set(result["pace_state"]) == {"PACE_PARTIAL"}
    assert set(result["pace_band"]) == {"moderate"}
    assert set(result["classified_runner_count"]) == {2}
    assert set(result["active_runner_count"]) == {5}
    assert result.loc[result["run_style_bucket"].isna(), "pace_fit_score"].isna().all()
    assert result.loc[result["run_style_bucket"].notna(), "pace_fit_score"].notna().all()


def test_pace_unavailable_nulls_constant_field_and_keeps_valid_forecast_mode():
    result = _fill_race_level_features(
        _pace_frame(["front", "front", "front", "front"]), derby_active=False
    )
    config = _verification_config()
    verification = verify_feature_frame(result, config, expected_entries=4)
    quality = DataQuality(
        entries_parsed=4,
        field_size_declared=4,
        entries_with_pp_history=4,
        starter_match_rate=1.0,
        race_metadata_complete=True,
        has_morning_lines=True,
        has_live_odds=False,
        required_model_features_complete=verification.passed,
    )
    mode, reasons = resolve_mode_with_feature_checks(quality, verification.core_rows)

    assert set(result["pace_state"]) == {"PACE_UNAVAILABLE"}
    assert result["pace_fit_score"].isna().all()
    assert verification.passed is True
    assert verification.pace_state == "PACE_UNAVAILABLE"
    assert any(reason.startswith("PACE_UNAVAILABLE") for reason in verification.warnings)
    assert mode == RunMode.MODEL_READY_LIMITED
    assert not any("forecast scoring is blocked" in reason for reason in reasons)

    with_pace_group = pre_market_signal_probabilities(result, config)
    without_pace_group = pre_market_signal_probabilities(
        result,
        {"feature_groups": {k: v for k, v in config["feature_groups"].items() if k != "race_shape"}},
    )
    assert np.isfinite(with_pace_group).all()
    assert np.allclose(with_pace_group, without_pace_group)
