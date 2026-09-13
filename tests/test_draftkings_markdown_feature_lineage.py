"""Feature-lineage integration for canonical DraftKings Markdown records."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.features.builder import (
    _canonical_history_overlay,
    _entry_features,
    _feature_lineage_json,
    _fill_race_level_features,
)
from src.ingest.draftkings_markdown import parse_draftkings_markdown, validate_draftkings_markdown_card
from src.services.draftkings_markdown_intake import persist_validated_draftkings_markdown
from src.services.feature_lineage import (
    entry_details_feature_status_rows,
    model_diagnostics_feature_status_rows,
)
from src.services.horse_profile import get_horse_profile


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R6_9-4-26.md"
AS_OF = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


def _card_and_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    card = parse_draftkings_markdown(FIXTURE, as_of=AS_OF)
    validation = validate_draftkings_markdown_card(card)
    assert validation.passed, validation.errors
    persisted = persist_validated_draftkings_markdown(
        conn, card, validation, source_filename=FIXTURE.name, source_path=str(FIXTURE)
    )
    return conn, persisted.card_id


def _feature_frame(conn, card_id: int) -> pd.DataFrame:
    entries = pd.read_sql("SELECT * FROM v_entries_live WHERE card_id=? ORDER BY post_position", conn, params=(card_id,))
    rows = []
    for _, entry in entries.iterrows():
        feature = _entry_features(entry, entries)
        feature.update({
            "entry_id": int(entry["entry_id"]), "horse_id": int(entry["horse_id"]),
            "card_id": int(entry["card_id"]), "horse_name": entry["horse_name"],
            "post_position": int(entry["post_position"]),
        })
        rows.append(feature)
    return _canonical_history_overlay(pd.DataFrame(rows), card_id, conn)


def _minimal_overlay_context() -> tuple[sqlite3.Connection, pd.DataFrame]:
    """Create the smallest canonical card context needed by the overlay."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('Test Track', 'TST')")
    conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface) "
        "VALUES (1, '2026-09-04', 1, 1760, 'dirt')"
    )
    conn.execute("INSERT INTO horses (name) VALUES ('Test Horse')")
    conn.execute(
        "INSERT INTO entries (card_id, horse_id, post_position, morning_line_odds) "
        "VALUES (1, 1, 1, 3.0)"
    )
    return conn, pd.DataFrame([{
        "horse_id": 1,
        "feature_source_mix": "seed",
        "dk_history_start_count": 0,
        "dk_workout_count": 0,
        "layoff_days": None,
        "career_itm_pct": None,
    }])


def test_dk_source_provider_counted():
    conn, features = _minimal_overlay_context()
    conn.execute(
        "INSERT INTO horse_starts "
        "(entry_id, horse_id, card_id, finish_position, field_size_last, start_date, "
        "distance_furlongs, surface, source_provider) "
        "VALUES (1, 1, 1, 2, 8, '2026-08-01', 8.0, 'dirt', 'draftkings_markdown')"
    )

    overlaid = _canonical_history_overlay(features, 1, conn)

    assert int(overlaid.at[0, "dk_history_start_count"]) >= 1
    assert "draftkings_markdown" in overlaid.at[0, "feature_source_mix"]
    # Canonical pre-race history fills these fields even when seed values are absent.
    assert overlaid.at[0, "last_race_date"] == "2026-08-01"
    assert int(overlaid.at[0, "layoff_days"]) == 34
    conn.close()


def test_dk_workout_counted():
    conn, features = _minimal_overlay_context()
    conn.execute(
        "INSERT INTO workouts "
        "(horse_id, workout_date, distance_furlongs, time_seconds, source_provider) "
        "VALUES (1, '2026-08-20', 4.0, 49.0, 'draftkings_markdown')"
    )

    overlaid = _canonical_history_overlay(features, 1, conn)

    assert int(overlaid.at[0, "dk_workout_count"]) >= 1
    assert int(overlaid.at[0, "days_since_last_workout"]) == 15
    assert int(overlaid.at[0, "days_since_last_work"]) == 15
    conn.close()


def test_legacy_provider_still_counted():
    conn, features = _minimal_overlay_context()
    conn.execute(
        "INSERT INTO horse_starts "
        "(entry_id, horse_id, card_id, finish_position, field_size_last, start_date, "
        "distance_furlongs, surface, source_provider) "
        "VALUES (1, 1, 1, 2, 8, '2026-08-01', 8.0, 'dirt', 'draftkings')"
    )

    overlaid = _canonical_history_overlay(features, 1, conn)

    assert int(overlaid.at[0, "dk_history_start_count"]) >= 1
    conn.close()


def test_canonical_dk_history_and_workouts_reach_feature_assembly_with_lineage():
    conn, card_id = _card_and_conn()
    persisted_rank = conn.execute(
        "SELECT source_rank, rank_denominator FROM workouts "
        "WHERE source_provider LIKE 'draftkings%' "
        "AND source_rank IS NOT NULL AND rank_denominator IS NOT NULL "
        "ORDER BY workout_id LIMIT 1"
    ).fetchone()
    assert tuple(persisted_rank) == (95, 115)
    features = _fill_race_level_features(_feature_frame(conn, card_id), derby_active=False)
    first = features.iloc[0]

    assert int(first["dk_history_start_count"]) > 0
    assert int(first["dk_workout_count"]) > 0
    assert first["feature_source_mix"] == "draftkings_markdown"
    assert int(first["days_since_last_workout"]) == 12
    assert first["last_workout_date"] == "2026-08-23"
    assert int(first["workout_count_30d"]) > 0
    assert int(first["days_since_last_start"]) == 49
    assert int(first["layoff_days"]) == 49
    assert first["last_finish_position"] == 5
    assert first["workout_rank_percentile"] is not None
    assert first["distance_fit"] != 0.5 or int(first["distance_fit_n"]) > 0
    assert first["surface_fit"] != 0.5 or int(first["surface_fit_n"]) > 0
    assert set(features["pace_state"]) == {"PACE_UNAVAILABLE"}
    assert features["pace_fit_score"].isna().all()

    lineage = {item["feature_name"]: item for item in json.loads(_feature_lineage_json(first))}
    assert lineage["dk_history_start_count"]["status"] == "SOURCE_BACKED"
    assert lineage["dk_history_start_count"]["source_system"] == "draftkings_markdown"
    assert lineage["dk_history_start_count"]["as_of_max_date"] == "2026-07-17"
    assert lineage["distance_fit_n"]["status"] == "SOURCE_BACKED"
    assert lineage["distance_fit_n"]["source_system"] == "draftkings_markdown"
    assert lineage["distance_fit_n"]["evidence_count"] > 0
    assert lineage["market_implied_prob"]["status"] == "DERIVED"
    assert lineage["market_implied_prob"]["source_system"] == "draftkings_markdown"
    assert lineage["market_implied_prob"]["evidence_count"] == 1
    assert lineage["class_level"]["status"] == "DERIVED"
    assert lineage["feature_source_mix"]["status"] == "SOURCE_BACKED"
    assert lineage["feature_source_mix"]["evidence_count"] > 0
    for feature_name in ("last_race_date", "last_finish_position", "last_beaten_lengths"):
        assert lineage[feature_name]["source"] == "draftkings_markdown"
        assert lineage[feature_name]["tier"] == "SOURCE_BACKED"
        assert lineage[feature_name]["evidence_count"] == 1
    for feature_name in ("layoff_days", "career_win_pct", "form_cycle_idx"):
        assert lineage[feature_name]["source"] == "draftkings_markdown"
        assert lineage[feature_name]["tier"] == "DERIVED"
        assert lineage[feature_name]["evidence_count"] > 0
    assert first["workout_rank_percentile"] is not None
    assert lineage["workout_rank_percentile"]["source"] == "draftkings_markdown"
    assert lineage["workout_rank_percentile"]["tier"] == "DERIVED"
    assert lineage["workout_rank_percentile"]["evidence_count"] == min(5, int(first["dk_workout_count"]))
    assert lineage["work_readiness_score"]["status"] == "DERIVED"
    assert lineage["work_readiness_score"]["as_of_max_date"] == "2026-08-23"
    for feature_name in ("speed_last", "speed_best", "speed_avg", "beyer_last", "pace_fit_score"):
        assert lineage[feature_name]["status"] == "UNAVAILABLE"
    assert "pace calls/sectionals" in lineage["pace_fit_score"]["fallback_reason"]

    # Both per-entry panels consume the same persisted JSON, rather than a
    # static feature-catalog tier, for this actual Markdown fixture entry.
    entry_statuses = {
        item["feature"]: item
        for item in entry_details_feature_status_rows(first)
    }
    diagnostics_statuses = {
        item["feature"]: item
        for item in model_diagnostics_feature_status_rows(first)
    }
    for feature_name in (
        "market_implied_prob", "distance_fit", "surface_fit", "work_readiness_score",
        "form_cycle_idx", "career_win_pct", "speed_best",
    ):
        assert tuple(entry_statuses[feature_name][key] for key in ("tier", "source", "evidence", "reason")) == tuple(
            diagnostics_statuses[feature_name][key] for key in ("tier", "source", "evidence", "reason")
        )
    conn.close()


def test_entry_profile_hydrates_dk_owner_records_pedigree_and_completed_history():
    conn, card_id = _card_and_conn()
    entry_id = conn.execute("SELECT entry_id FROM entries WHERE card_id=? ORDER BY post_position LIMIT 1", (card_id,)).fetchone()[0]
    profile = get_horse_profile(conn, entry_id)

    assert profile["owner"] == "Lessell Edward R and Mary Jo"
    assert profile["sire"] == "Summer Front"
    assert profile["dam"] == "Matty's Magnum"
    assert (profile["career_starts"], profile["career_wins"]) == (8, 1)
    assert profile["lifetime_earnings"] == 26547
    assert profile["dirt_last5_starts"] == 4
    assert profile["distance_last5_starts"] == 3
    assert profile["last_race_date"] == "2026-07-17"
    assert profile["pp_starts"]
    conn.close()


def test_full_builder_persists_dk_lineage_to_feature_store(monkeypatch, tmp_path):
    """Exercise the same DB-backed entrypoint used by Build features in the UI."""
    from src.features.builder import build_features
    from src.utils import db as db_utils

    db_path = tmp_path / "derbyedge.db"
    monkeypatch.setattr(db_utils, "DB_PATH", db_path)
    db_utils.init_db()
    conn = db_utils.get_connection()
    card = parse_draftkings_markdown(FIXTURE, as_of=AS_OF)
    validation = validate_draftkings_markdown_card(card)
    persisted = persist_validated_draftkings_markdown(
        conn, card, validation, source_filename=FIXTURE.name, source_path=str(FIXTURE)
    )
    conn.close()

    built = build_features(persisted.card_id)
    assert (built["dk_history_start_count"] > 0).any()
    assert (built["dk_workout_count"] > 0).any()
    assert set(built["feature_source_mix"]) == {"draftkings_markdown"}
    assert built["feature_lineage_json"].notna().all()

    conn = db_utils.get_connection()
    exported = conn.execute(
        "SELECT dk_history_start_count, dk_workout_count, feature_source_mix, market_implied_prob, "
        "market_implied_prob_source, feature_lineage_json "
        "FROM feature_store WHERE card_id=? ORDER BY post_position LIMIT 1",
        (persisted.card_id,),
    ).fetchone()
    assert exported[0] > 0 and exported[1] > 0
    assert exported[2] == "draftkings_markdown"
    assert exported[3] is not None
    assert exported[4] == "morning_line"
    persisted_lineage = {item["feature_name"]: item for item in json.loads(exported[5])}
    market_lineage = persisted_lineage["market_implied_prob"]
    assert market_lineage["value"] == exported[3]
    assert market_lineage["status"] == "DERIVED"
    assert market_lineage["source_system"] == "draftkings_markdown"
    assert market_lineage["evidence_count"] > 0
    conn.close()
