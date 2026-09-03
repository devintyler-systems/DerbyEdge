from __future__ import annotations

from datetime import date
import csv
import math
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd
import pytest

from src.ingest.draftkings_pdf import (
    DraftKingsEntryRecord,
    DraftKingsParsedRace,
    DraftKingsStartRecord,
    DraftKingsWorkoutRecord,
)
from src.models.trainer import (
    TRAIN_CONFIGS,
    XGBOOST_PROMOTION_CONFIG,
    _softmax,
    assess_xgboost_promotion,
    build_seed_baseline,
    calibrate_temperature,
    compute_group_scores,
    temperature_adjustment_status,
    train_or_build,
    register_model,
)
from src.services.draftkings_enrich import _finish_percentile, generate_dk_pre_race_features
from src.services.odds_intake import market_eligibility
from scripts.build_features import CATALOG, write_catalog


def _entry(name: str = "Alpha") -> DraftKingsEntryRecord:
    return DraftKingsEntryRecord(
        post_position=1, program_number=1, horse_name=name,
        horse_source_key=f"draftkings:{name.lower()}:gelding:2022:ky",
        morning_line_raw="4/1", morning_line_decimal=5.0,
        other_odds_raw=None, odds_type="unknown", sex="gelding", age=4,
        foaling_year=2022, color="bay", state_bred="KY", lasix=False,
        angles=[], source_page_number=1, source_row_id=f"entry:{name}",
        raw_text=name, parse_confidence=1.0,
    )


def _start(
    when: date, finish: int | None, field: int | None,
    *, scratch: bool = False, target: bool = False, row_id: str = "s1",
) -> DraftKingsStartRecord:
    return DraftKingsStartRecord(
        horse_name="Alpha", horse_source_key="draftkings:alpha:gelding:2022:ky",
        start_date=when, is_target_race=target, track_code="SAR",
        track_name="Saratoga", race_class="ALW", distance_text="1M",
        distance_furlongs=8.0, surface="dirt", surface_condition="fast",
        program_post="1", odds_raw="3/1", finish_position=finish,
        is_scratch=scratch, source_page_number=1, source_row_id=row_id,
        raw_text=row_id, parse_confidence=1.0, field_size=field,
    )


def _work(when: date, row_id: str, *, target: bool = False) -> DraftKingsWorkoutRecord:
    return DraftKingsWorkoutRecord(
        horse_name="Alpha", horse_source_key="draftkings:alpha:gelding:2022:ky",
        workout_date=when, is_target_race=target, track_code="SAR",
        track_name="Saratoga", distance_text="4F", distance_furlongs=4.0,
        surface="dirt", surface_condition="fast", time_seconds=48.0,
        time_text="48.0", work_grade="B", rank=2, source_page_number=1,
        source_row_id=row_id, raw_text=row_id, parse_confidence=1.0,
    )


def _parsed(starts=(), workouts=(), annotations=()) -> DraftKingsParsedRace:
    return DraftKingsParsedRace(
        source_document_id="dk_doc_test", file_sha256="0" * 64,
        file_size_bytes=1, filename_track_code="SAR", header_track_code="SAR",
        filename_race_number=1, header_race_number=1,
        target_race_date=date(2026, 1, 10), track_name="Saratoga",
        stakes_name=None, race_class="ALW", purse=100_000,
        distance_text="1M", distance_furlongs=8.0, surface="dirt",
        conditions=None, field_size_declared=8, captured_at="2026-01-09T12:00:00Z",
        is_post_race=False, production_eligible=True, eligibility_reason="pre_race",
        status="success", entry_count=1, entry_parse_coverage=1.0,
        workout_count=len(workouts), historical_start_count=len(starts),
        unparsed_runner_blocks=[], entries=[_entry()], starts=list(starts),
        workouts=list(workouts), scratches=[], annotations=list(annotations),
        odds_records=[], manifest={},
    )


def _minimal_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE horses (horse_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE entries (entry_id INTEGER PRIMARY KEY, card_id INTEGER, horse_id INTEGER);
    """)
    return conn


def _baseline_frame() -> pd.DataFrame:
    config = TRAIN_CONFIGS["dirt_route"]
    columns = {
        name for group in config["feature_groups"].values()
        for name in group["features"]
    }
    frame = pd.DataFrame({name: [0.2, 0.8, 0.5] for name in columns})
    for coverage in ("form_class_coverage", "distance_surface_coverage", "readiness_coverage"):
        frame[coverage] = [1.0, 1.0, 1.0]
    frame["market_implied_prob"] = [0.2, 0.5, 0.3]
    frame["market_implied_prob_source"] = "morning_line"
    return frame


def test_dirt_route_group_weights_are_governed_and_sum_to_one():
    weights = {name: group["group_weight"] for name, group in TRAIN_CONFIGS["dirt_route"]["feature_groups"].items()}
    assert weights == {
        "speed_quality": 0.25, "form_class": 0.18,
        "distance_surface": 0.17, "race_shape": 0.15,
        "readiness": 0.13, "derby_override": 0.07, "market_prior": 0.05,
    }
    assert sum(weights.values()) == pytest.approx(1.0)


def test_xgboost_gate_thresholds_match_controlled_policy():
    gate = XGBOOST_PROMOTION_CONFIG
    assert gate.minimum_completed_races == 500
    assert gate.minimum_labeled_starters == 4000
    assert gate.minimum_rolling_validation_folds == 12
    assert gate.minimum_core_non_market_feature_coverage == pytest.approx(0.80)
    assert gate.minimum_brier_improvement == pytest.approx(0.02)
    assert gate.minimum_log_loss_improvement == pytest.approx(0.01)


def test_career_win_pct_and_editorial_annotations_do_not_change_baseline():
    base = _baseline_frame()
    altered = base.copy()
    altered["career_win_pct"] = [0.99, 0.01, 0.75]
    altered["hot_trainer"] = [1, 0, 1]
    altered["top_pick"] = ["yes", "no", "yes"]
    expected_artifact, expected = build_seed_baseline(base, pd.DataFrame(), TRAIN_CONFIGS["dirt_route"])
    actual_artifact, actual = build_seed_baseline(altered, pd.DataFrame(), TRAIN_CONFIGS["dirt_route"])
    assert "career_win_pct" not in TRAIN_CONFIGS["dirt_route"]["feature_groups"]["form_class"]["features"]
    assert np.allclose(actual, expected)
    assert actual_artifact.calibration_audit == expected_artifact.calibration_audit
    live_market = np.array([0.30, 0.40, 0.30])
    expected_value, actual_value = expected - live_market, actual - live_market
    threshold = TRAIN_CONFIGS["dirt_route"]["bet_edge_threshold"]
    assert np.allclose(actual_value, expected_value)
    assert (actual_value >= threshold).tolist() == (expected_value >= threshold).tolist()


def test_recent_finish_is_field_adjusted_pre_race_non_scratch_and_target_excluded():
    parsed = _parsed(starts=[
        _start(date(2026, 1, 1), 2, 10, row_id="valid"),
        _start(date(2026, 1, 2), 1, 5, scratch=True, row_id="scratch"),
        _start(date(2026, 1, 10), 1, 5, target=True, row_id="target"),
    ])
    conn = _minimal_conn()
    row = generate_dk_pre_race_features(conn, 1, parsed).iloc[0]
    observed = 1.0 - ((2 - 1) / (10 - 1))
    assert row["recent_finish_percentile_w"] == pytest.approx(0.2 * observed + 0.8 * 0.5, abs=1e-4)
    assert row["recent_finish_evidence_count"] == 1
    assert row["starts_last_90d"] == 1
    assert row["target_race_records_excluded"] == 1
    assert row["market_implied_prob_source"] == "morning_line"
    assert row["market_implied_prob"] == pytest.approx(0.20)
    assert row["prior_publicness"] != row["market_implied_prob"]
    assert 0 <= row["recent_finish_percentile_w"] <= 1


def test_sparse_fit_and_group_features_are_shrunk_toward_neutral():
    parsed = _parsed(starts=[_start(date(2026, 1, 1), 1, 8)])
    row = generate_dk_pre_race_features(_minimal_conn(), 1, parsed).iloc[0]
    assert row["distance_fit_eb"] == pytest.approx(2 / 3, abs=1e-4)
    assert row["surface_fit_eb"] == pytest.approx(2 / 3, abs=1e-4)
    assert row["distance_surface_coverage"] == pytest.approx(0.25)
    config = {
        "feature_groups": {"g": {
            "group_weight": 1.0, "features": {"x": 1.0},
            "coverage_feature": "coverage", "reliability_features": {"x"},
        }}
    }
    scores = compute_group_scores(pd.DataFrame({"x": [0.0, 1.0], "coverage": [0.25, 0.25]}), config)["g"]
    assert scores.tolist() == pytest.approx([0.375, 0.625])


def test_historical_scratch_rate_exposes_denominator_and_low_sample_policy():
    starts = [
        _start(date(2026, 1, 1), 2, 8, row_id="a"),
        _start(date(2025, 12, 20), 3, 8, row_id="b"),
        _start(date(2025, 12, 1), None, 8, scratch=True, row_id="c"),
    ]
    row = generate_dk_pre_race_features(_minimal_conn(), 1, _parsed(starts=starts)).iloc[0]
    assert row["historical_scratch_n"] == 3
    assert row["historical_scratch_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert row["historical_scratch_confidence"] == "adequate"


def test_workout_readiness_excludes_target_date_and_missing_cohort_is_neutral_low_coverage():
    parsed = _parsed(workouts=[
        _work(date(2026, 1, 5), "valid"),
        _work(date(2026, 1, 10), "target", target=True),
    ])
    row = generate_dk_pre_race_features(_minimal_conn(), 1, parsed).iloc[0]
    assert row["days_since_last_workout"] == 5
    assert row["workout_count_30d"] == 1
    assert row["readiness_coverage"] == pytest.approx(1 / 3, abs=1e-4)
    assert row["workout_time_normalization_available"] == 0
    assert 0 <= row["workout_readiness_score_v2"] <= 1


@pytest.mark.parametrize("odds_type", ["off_odds", "unknown"])
def test_historical_and_unknown_odds_are_never_market_eligible(odds_type):
    assert market_eligibility(odds_type, "2026-01-10T10:00:00Z", "2026-01-10T12:00:00Z")[0] is False


def test_morning_line_label_and_live_tote_timestamp_post_rules():
    assert market_eligibility("morning_line", None) == (True, "morning_line_prior")
    assert market_eligibility("live_tote", None, "2026-01-10T12:00:00Z")[0] is False
    assert market_eligibility("live_tote", "2026-01-10T13:00:00Z", "2026-01-10T12:00:00Z")[0] is False
    assert market_eligibility("live_tote", "2026-01-10T11:00:00Z", "2026-01-10T12:00:00Z")[0] is True


def test_calibration_audit_is_finite_bounded_and_labeled():
    artifact, probabilities = build_seed_baseline(_baseline_frame(), pd.DataFrame(), TRAIN_CONFIGS["dirt_route"])
    audit = artifact.calibration_audit
    for key in ("uncalibrated_entropy", "calibrated_entropy", "selected_temperature", "divergence_from_morning_line"):
        assert math.isfinite(audit[key])
    assert TRAIN_CONFIGS["dirt_route"]["temperature_lower_bound"] <= artifact.temperature <= TRAIN_CONFIGS["dirt_route"]["temperature_upper_bound"]
    assert audit["market_prior_source"] == "morning_line"
    assert probabilities.sum() == pytest.approx(1.0)


def test_temperature_bounds_and_softmax_concentration_are_deterministic():
    config = TRAIN_CONFIGS["dirt_route"]
    scores = np.array([0.0, 1.0, 2.0])
    market = np.array([0.2, 0.3, 0.5])
    _, selected = calibrate_temperature(scores, market, config)

    assert config["temperature_lower_bound"] == pytest.approx(0.25)
    assert config["temperature_upper_bound"] == pytest.approx(4.00)
    assert selected >= 0.25
    assert selected <= 4.00
    assert _softmax(scores, 0.5).max() < _softmax(scores, 1.0).max()
    assert _softmax(scores, 2.0).max() > _softmax(scores, 1.0).max()


@pytest.mark.parametrize(
    ("temperature", "expected_status"),
    [(0.25, "softened"), (1.0, "unchanged"), (4.0, "sharpened")],
)
def test_calibration_audit_identifies_temperature_adjustment(temperature, expected_status):
    config = {
        **TRAIN_CONFIGS["dirt_route"],
        "temperature_lower_bound": temperature,
        "temperature_upper_bound": temperature,
        "temperature_default": temperature,
    }
    artifact, _ = build_seed_baseline(_baseline_frame(), pd.DataFrame(), config)
    assert artifact.calibration_audit["temperature_adjustment_status"] == expected_status
    assert temperature_adjustment_status(temperature) == expected_status


@pytest.mark.parametrize(
    "start",
    [
        _start(date(2026, 1, 1), 1, 1, row_id="singleton"),
        _start(date(2026, 1, 1), 1, None, row_id="missing-field"),
        _start(date(2026, 1, 1), None, 10, row_id="missing-finish"),
        _start(date(2026, 1, 1), 11, 10, row_id="out-of-range"),
        _start(date(2026, 1, 1), 1, 10, scratch=True, row_id="scratch"),
        _start(date(2026, 1, 10), 1, 10, row_id="target-date"),
        _start(date(2026, 1, 11), 1, 10, row_id="post-target"),
    ],
)
def test_invalid_recent_finish_inputs_are_excluded_from_observed_evidence(start):
    row = generate_dk_pre_race_features(_minimal_conn(), 1, _parsed(starts=[start])).iloc[0]
    assert row["recent_finish_evidence_count"] == 0
    assert row["form_class_coverage"] == pytest.approx(0.0)
    assert row["recent_finish_percentile_w"] == pytest.approx(0.50)


def test_valid_finish_percentile_endpoints_and_weighted_evidence_are_exact():
    win = _start(date(2026, 1, 1), 1, 10, row_id="win")
    last = _start(date(2025, 12, 31), 10, 10, row_id="last")
    invalid = _start(date(2026, 1, 2), 1, 1, row_id="singleton")
    assert _finish_percentile(win) == pytest.approx(1.0)
    assert _finish_percentile(last) == pytest.approx(0.0)

    row = generate_dk_pre_race_features(
        _minimal_conn(), 1, _parsed(starts=[win, invalid])
    ).iloc[0]
    assert row["recent_finish_evidence_count"] == 1
    assert row["form_class_coverage"] == pytest.approx(0.2)
    assert row["recent_finish_percentile_w"] == pytest.approx(0.60)


def test_recent_finish_catalog_matches_valid_observation_contract(tmp_path):
    source_entry = next(item for item in CATALOG if item[0] == "recent_finish_percentile_w")
    source_text = " ".join(str(value) for value in source_entry)
    source_path = Path("scripts/build_features.py")
    generated_path = Path("output/feature_catalog.csv")

    assert source_entry[2] == "horse_pre_race"
    for required in (
        "non-scratch", "start_date < target race date", "field_size present >= 2",
        "finish_position present", "1 <= finish_position <= field_size",
        "finish_percentile = 1 - ((finish_position - 1) / (field_size - 1))",
        "recency-weighted across valid historical starts", "Invalid/singleton records contribute no observed percentile",
        "lowers coverage", "neutral prior", "target-date/target-race records are excluded",
    ):
        assert required in source_text
    assert "max(field_size" not in source_path.read_text(encoding="utf-8")

    with generated_path.open(newline="", encoding="utf-8") as handle:
        row = next(row for row in csv.DictReader(handle) if row["feature_name"] == "recent_finish_percentile_w")
    generated_text = " ".join(row.values())
    for required in (
        "field_size present >= 2", "1 <= finish_position <= field_size",
        "field_size - 1", "Invalid/singleton records contribute no observed percentile",
        "lowers coverage", "neutral prior",
    ):
        assert required in generated_text
    assert "max(field_size" not in generated_path.read_text(encoding="utf-8")

    regenerated = tmp_path / "feature_catalog.csv"
    write_catalog(regenerated)
    assert regenerated.read_bytes() == generated_path.read_bytes()


def _ready_summary(**changes):
    summary = {
        "completed_races": 500, "labeled_starters": 4000,
        "rolling_validation_folds": 12, "core_feature_coverage": 0.80,
        "race_group_membership_valid": True, "valid_outcome_labels": True,
        "no_target_race_leakage": True,
    }
    summary.update(changes)
    return summary


def _passing_metrics():
    return {
        "baseline_brier_score": 0.20, "brier_score": 0.195,
        "baseline_log_loss": 0.50, "log_loss": 0.49,
        "calibration_acceptable": True,
        "field_size_regression_acceptable": True,
        "artifact_path": "model.pkl", "feature_schema_version": "2",
        "training_window_start": "2024-01-01", "training_window_end": "2025-12-31",
        "target_race_type_key": "dirt_route", "calibration_artifact_path": "cal.pkl",
    }


def test_xgboost_gate_reason_codes_shadow_and_promotion_require_oof_evidence():
    low = assess_xgboost_promotion(_ready_summary(completed_races=5, labeled_starters=50, rolling_validation_folds=0, core_feature_coverage=0.2))
    assert low.production_model == "seed_only_baseline"
    assert "insufficient_completed_races" in low.reason_codes
    shadow = assess_xgboost_promotion(_ready_summary(completed_races=100, labeled_starters=800, rolling_validation_folds=4, core_feature_coverage=0.7))
    assert shadow.mode == "shadow" and shadow.production_model == "seed_only_baseline"
    denied = assess_xgboost_promotion(_ready_summary(), {})
    assert denied.production_model == "seed_only_baseline"
    assert "oof_baseline_not_beaten" in denied.reason_codes
    promoted = assess_xgboost_promotion(_ready_summary(), _passing_metrics())
    assert promoted.mode == "promoted" and promoted.reason_codes == ("promoted",)


def test_fifty_horse_starts_never_activates_xgboost():
    root = Path(__file__).resolve().parents[1]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = (root / "db" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript("\n".join(line for line in schema.splitlines() if "journal_mode" not in line))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executemany(
        "INSERT INTO horse_starts(entry_id, horse_id, card_id, lengths_behind) VALUES(1,1,1,0)",
        [()] * 50,
    )
    artifact, _ = train_or_build(_baseline_frame(), pd.DataFrame(), conn=conn)
    assert artifact.model_type == "seed_only_baseline"
    assert artifact.dispatcher_audit["production_model"] == "seed_only_baseline"
    assert "insufficient_completed_races" in artifact.dispatcher_audit["reason_codes"]


def test_methodology_schema_migrations_are_additive_and_idempotent():
    from src.utils.db import (
        ensure_feature_store_columns, ensure_horse_starts_columns,
        ensure_model_registry_columns, ensure_workouts_columns,
    )
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE feature_store (feature_id INTEGER PRIMARY KEY);
        CREATE TABLE horse_starts (start_id INTEGER PRIMARY KEY);
        CREATE TABLE workouts (workout_id INTEGER PRIMARY KEY);
        CREATE TABLE model_registry (model_id INTEGER PRIMARY KEY);
    """)
    functions = (
        ensure_feature_store_columns, ensure_horse_starts_columns,
        ensure_workouts_columns, ensure_model_registry_columns,
    )
    for function in functions:
        function(conn)
        function(conn)
    feature_cols = {row[1] for row in conn.execute("PRAGMA table_info(feature_store)")}
    start_cols = {row[1] for row in conn.execute("PRAGMA table_info(horse_starts)")}
    workout_cols = {row[1] for row in conn.execute("PRAGMA table_info(workouts)")}
    registry_cols = {row[1] for row in conn.execute("PRAGMA table_info(model_registry)")}
    assert {"form_class_coverage", "distance_surface_coverage", "readiness_coverage"} <= feature_cols
    assert {"start_date", "historical_odds_type", "source_provider"} <= start_cols
    assert {"source_rank", "location_label", "source_provider"} <= workout_cols
    assert {"dispatcher_mode", "rolling_validation_folds", "feature_schema_version"} <= registry_cols


def test_dk_generator_is_not_a_feature_store_persistence_boundary():
    import inspect
    source = inspect.getsource(generate_dk_pre_race_features)
    assert "feature_store" not in source
    assert ".to_sql" not in source


def test_model_registry_records_dispatcher_audit_without_parallel_registry(tmp_path):
    root = Path(__file__).resolve().parents[1]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = (root / "db" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript("\n".join(line for line in schema.splitlines() if "journal_mode" not in line))
    artifact, _ = build_seed_baseline(_baseline_frame(), pd.DataFrame(), TRAIN_CONFIGS["dirt_route"])
    artifact.dispatcher_audit = {
        "mode": "baseline", "reason_codes": ["insufficient_completed_races"],
        "completed_races": 0, "labeled_starters": 0,
        "rolling_validation_folds": 0, "core_feature_coverage": 0.0,
    }
    model_id = register_model(artifact, tmp_path / "audit-only.pkl", {}, conn)
    row = conn.execute("SELECT * FROM model_registry WHERE model_id=?", (model_id,)).fetchone()
    assert row["dispatcher_mode"] == "baseline"
    assert "insufficient_completed_races" in row["dispatcher_reason_codes"]
    assert row["feature_schema_version"]


def test_dk_canonical_history_flows_through_build_features_only(tmp_path, monkeypatch):
    from src.features.builder import build_features
    from src.services.draftkings_enrich import ingest_draftkings_to_canonical
    import src.utils.db as db_module

    db_path = tmp_path / "dk-methodology.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    root = Path(__file__).resolve().parents[1]
    schema = (root / "db" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    parsed = _parsed(
        starts=[
            _start(date(2026, 1, 1), 1, 8, row_id="a"),
            _start(date(2025, 12, 20), 4, 10, row_id="b"),
        ],
        workouts=[_work(date(2026, 1, 5), "w1")],
    )
    card_id, _ = ingest_draftkings_to_canonical(conn, parsed)
    conn.close()
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    frame = build_features(card_id)
    row = frame.iloc[0]
    assert row["dk_history_start_count"] == 2
    assert row["dk_workout_count"] == 1
    assert row["form_class_coverage"] == pytest.approx(0.4)
    assert row["distance_surface_coverage"] == pytest.approx(0.5)
    verify = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM feature_store WHERE card_id=?", (card_id,)
    ).fetchone()[0]
    assert verify == 1
