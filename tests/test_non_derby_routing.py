"""Regression coverage for non-Derby run isolation and feature gating."""
from __future__ import annotations

import pandas as pd

from src.features.builder import _fill_race_level_features
from src.models.trainer import TRAIN_CONFIGS
from src.utils.run_assets import card_run_key, run_dir_for_card


def test_card_run_key_is_stable_and_card_scoped():
    assert card_run_key("SAR", "2026-08-29", 13) == "sar_2026-08-29_r13"


def test_run_dir_uses_card_identity(mem_conn):
    track_id = mem_conn.execute(
        "INSERT INTO tracks (name, abbrev) VALUES ('Saratoga', 'SAR')"
    ).lastrowid
    card_id = mem_conn.execute(
        """INSERT INTO race_cards
               (track_id, card_date, race_number, distance_yards, surface)
           VALUES (?, '2026-08-29', 13, 1760, 'dirt')""",
        (track_id,),
    ).lastrowid

    assert run_dir_for_card(card_id, conn=mem_conn).as_posix().endswith(
        "output/runs/sar_2026-08-29_r13"
    )


def test_non_derby_feature_values_are_null():
    frame = pd.DataFrame({
        "market_implied_prob": [0.4, 0.2],
        "speed_last": [90.0, 85.0],
        "run_style_bucket": ["front", "stalker"],
        "publicness_score": [1.0, 0.7],
        "classic_distance_projection": [0.8, 0.7],
        "churchill_readiness": [0.5, 0.4],
        "jan_apr_improvement_curve": [0.2, 0.1],
        "pedigree_route_proxy": [0.8, 0.7],
        "work_readiness_score": [0.5, 0.4],
        "gate_reliability": [0.8, 0.7],
        "class_level": [1.0, 0.8],
    })

    result = _fill_race_level_features(frame, derby_active=False)
    derby_only = [
        "classic_distance_projection",
        "churchill_readiness",
        "jan_apr_improvement_curve",
        "derby_override_score",
    ]
    assert result[derby_only].isna().all().all()


def test_ordinary_dirt_route_config_excludes_derby_override():
    config = TRAIN_CONFIGS["dirt_route"]
    assert config["model_family"] == "dirt_route_stakes_v1"
    assert config["model_name"] == "dirt_route_stakes_v1"
    assert "derby_override" not in config["feature_groups"]
