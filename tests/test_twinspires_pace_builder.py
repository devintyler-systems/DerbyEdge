"""TwinSpires pace source reaches the existing race-shape calculation safely."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.features.builder import build_features
from src.ingest.twinspires_markdown import parse_twinspires_markdown
from src.services.twinspires_intake import persist_twinspires_card, validate_twinspires_card


ROOT = Path(__file__).resolve().parents[1]
CT = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "CT_TwinSpires__R7_Summary_9-12-26.md"
AS_OF = "2026-09-11T20:00:00Z"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _ct_card(tmp_path: Path, monkeypatch) -> int:
    db_path = tmp_path / "pace.sqlite"
    conn = _connect(db_path)
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO tracks (name,abbrev) VALUES ('Charles Town','CT')")
    conn.execute(
        "INSERT INTO race_cards (track_id,card_date,race_number,distance_yards,field_size) VALUES (1,'2026-09-12',7,880,10)"
    )
    card_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    parsed = parse_twinspires_markdown(CT)
    assert not parsed.parser_errors and len(parsed.records) == 10
    for record in parsed.records:
        conn.execute("INSERT INTO horses (name) VALUES (?)", (record.horse_name,))
        horse_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """INSERT INTO entries
               (card_id,horse_id,post_position,program_number,morning_line_odds,
                career_starts,career_wins,career_places,career_shows,career_earnings,
                dirt_starts,dirt_wins,dist_starts,dist_wins,last_race_days,
                last_race_finish,last_speed_fig,best_speed_fig,avg_speed_fig,
                workouts_30,gate_class,stamina_index)
               VALUES (?,?,?,?,?,5,1,1,1,20000,5,1,3,1,29,3,70,75,72,2,3,0.6)""",
            (card_id, horse_id, int(record.program_number), record.program_number, 5.0),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.utils.db.get_connection", lambda: _connect(db_path))
    return card_id


def test_ct_parser_separates_style_points_and_source_specific_figures():
    card = parse_twinspires_markdown(CT)
    assert not card.parser_errors
    assert len(card.records) == 10  # three SCR runners excluded
    by_program = {record.program_number: record for record in card.records}
    first = by_program["1"]
    assert (first.run_style, first.run_style_code, first.early_speed_points) == ("E/P4", "E/P", 4)
    assert (first.avg_speed, first.back_speed, first.last_speed) == (68, 71, 61)
    assert (first.class_rating, first.power_rating) == (106.5, 106.8)
    assert (by_program["3"].run_style_code, by_program["3"].early_speed_points) == ("S", 0)
    assert (by_program["4"].run_style_code, by_program["4"].early_speed_points) == ("E", 3)


def test_matching_ct_summary_populates_existing_race_shape(tmp_path, monkeypatch):
    card_id = _ct_card(tmp_path, monkeypatch)
    features = build_features(card_id, twinspires_summary=CT, twinspires_as_of=AS_OF)
    assert set(features.pace_state) == {"PACE_READY"}
    assert features.pace_fit_score.notna().all()
    assert features.pace_fit_score.nunique() > 1
    assert set(features.run_style_source) == {"twinspires"}
    first = features.loc[features.post_position == 1].iloc[0]
    assert (first.run_style_bucket, first.run_style_code, first.early_speed_points) == ("presser", "E/P", 4)
    assert first.early_intent == 0.7
    assert first.pace_pressure == first.collapse_risk
    assert features.lone_speed_edge.sum() == 0  # two E runners in this field
    lineage = {item["feature_name"]: item for item in json.loads(first.feature_lineage_json)}
    assert lineage["pace_fit_score"]["source_system"] == "twinspires"
    assert lineage["pace_fit_score"]["as_of_max_date"] == "2026-09-11T20:00:00+00:00"


def test_proven_ingested_card_is_discovered_without_filename_matching(tmp_path, monkeypatch):
    card_id = _ct_card(tmp_path, monkeypatch)
    conn = _connect(tmp_path / "pace.sqlite")
    card = parse_twinspires_markdown(CT, declared_as_of=AS_OF)
    validation = validate_twinspires_card(conn, card_id, card)
    assert validation.passed
    persist_twinspires_card(conn, card, validation, raw_bytes=CT.read_bytes())
    conn.close()
    features = build_features(card_id)
    assert set(features.pace_state) == {"PACE_READY"}
    assert set(features.run_style_source) == {"twinspires"}
    assert features.pace_fit_score.notna().all()


def test_absent_or_unproven_summary_keeps_pace_unavailable(tmp_path, monkeypatch):
    card_id = _ct_card(tmp_path, monkeypatch)
    absent = build_features(card_id)
    assert set(absent.pace_state) == {"PACE_UNAVAILABLE"}
    assert absent.pace_fit_score.isna().all()
    unproven = build_features(card_id, twinspires_summary=CT)
    assert set(unproven.pace_state) == {"PACE_UNAVAILABLE"}
    assert unproven.pace_fit_score.isna().all()


def test_malformed_or_partial_summary_fails_closed(tmp_path, monkeypatch):
    card_id = _ct_card(tmp_path, monkeypatch)
    raw = CT.read_text(encoding="utf-8")
    malformed = tmp_path / "malformed.md"
    malformed.write_text(raw.replace("E/P4", "UNKNOWN", 1), encoding="utf-8")
    wrong_schema = tmp_path / "wrong_schema.md"
    wrong_schema.write_text(raw.replace("PRM\nPWR", "BAD\nPWR", 1), encoding="utf-8")
    partial = tmp_path / "partial.md"
    partial.write_text(raw[:raw.index("10\n21\nM: 12")], encoding="utf-8")
    for source in (malformed, partial, wrong_schema):
        features = build_features(card_id, twinspires_summary=source, twinspires_as_of=AS_OF)
        assert set(features.pace_state) == {"PACE_UNAVAILABLE"}
        assert features.pace_fit_score.isna().all()
        assert features.run_style_code.isna().all()
