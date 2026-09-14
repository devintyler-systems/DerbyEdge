"""Contracts for the isolated TwinSpires Speed/Power/Style source lane."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest.draftkings_markdown import parse_draftkings_markdown, validate_draftkings_markdown_card
from src.ingest.twinspires_markdown import PARSER_VERSION, parse_twinspires_markdown
from src.services.draftkings_markdown_intake import persist_validated_draftkings_markdown
from src.services.twinspires_intake import (
    persist_twinspires_card, reconcile_twinspires_records, validate_twinspires_card,
)

ROOT = Path(__file__).resolve().parents[1]
DK = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R6_9-4-26.md"
TS = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R6_Speed_Power_Style_9-4-26.md"
AS_OF = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    schema = "\n".join(line for line in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in line)
    conn.executescript(schema)
    card = parse_draftkings_markdown(DK, as_of=AS_OF)
    persisted = persist_validated_draftkings_markdown(conn, card, validate_draftkings_markdown_card(card), source_filename=DK.name)
    assert persisted.card_id == 1
    return conn


def test_parser_extracts_expected_fields_and_preserves_nulls(caplog):
    caplog.set_level("INFO", logger="derbyedge.ingest.source_guard")
    card = parse_twinspires_markdown(TS)
    assert card.parser_version == PARSER_VERSION
    assert "INGEST_SOURCE_STAMP" in caplog.text
    assert len(card.records) == 10
    mykonos = next(row for row in card.records if row.horse_name == "Mykonos")
    assert (mykonos.run_style, mykonos.avg_speed, mykonos.back_speed, mykonos.last_speed) == ("E/P3", 75.0, 79.0, 64.0)
    assert (mykonos.class_rating, mykonos.power_rating) == (107.8, 105.6)
    assert (mykonos.jockey_win_pct, mykonos.trainer_win_pct) == (0.19, 0.122)
    fame = next(row for row in card.records if row.horse_name == "Fame Chaser")
    assert fame.back_speed is None
    assert fame.raw_values["back_speed"] == "-"
    assert card.as_of_status == "UNPROVEN"


def test_provenance_and_all_ten_runner_reconciliation_are_isolated():
    conn = _conn(); raw = TS.read_bytes(); card = parse_twinspires_markdown(TS)
    validation = validate_twinspires_card(conn, 1, card)
    # The named fixture is a different field than the canonical DK card.  The
    # lane retains the raw source but fails closed before observation hydration.
    assert not validation.passed
    assert len(validation.reconciliation) == 10
    assert {item.match_status for item in validation.reconciliation} == {"UNMATCHED"}
    artifact_id = persist_twinspires_card(conn, card, validation, raw_bytes=raw)
    artifact = conn.execute("SELECT source_provider, sha256, parser_version, raw_bytes, as_of_status, normalized_record_count, normalized_records_json FROM source_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    assert tuple(artifact[:3]) == ("twinspires", card.source_sha256, PARSER_VERSION)
    assert bytes(artifact[3]) == raw and artifact[4] == "UNPROVEN" and artifact[5] == 10
    assert len(json.loads(artifact[6])) == 10
    assert conn.execute("SELECT COUNT(*) FROM source_observations WHERE artifact_id=?", (artifact_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_feature_candidates WHERE feature_name='beyer_last'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_feature_candidates WHERE feature_name IN ('pace_fit_score','pace_pressure','collapse_risk','sectional_pace','finish_energy')").fetchone()[0] == 0
    # No DK-owned canonical data is updated, relabeled, or overwritten.
    assert conn.execute("SELECT COUNT(*) FROM horse_starts WHERE source_provider='twinspires'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM workouts WHERE source_provider='twinspires'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE card_id=1").fetchone()[0] == 10


def test_ambiguous_and_unmatched_identity_fail_closed():
    conn = _conn(); card = parse_twinspires_markdown(TS)
    conn.execute("INSERT INTO horses (name) VALUES ('Mykonos (IRE)')")
    horse_id = conn.execute("SELECT horse_id FROM horses WHERE name='Mykonos (IRE)'").fetchone()[0]
    conn.execute("INSERT INTO entries (card_id,horse_id,post_position,morning_line_odds) VALUES (1,?,?,10)", (horse_id, 99))
    conn.execute("INSERT INTO horses (name) VALUES ('Mykonos-Ire')")
    second_horse_id = conn.execute("SELECT horse_id FROM horses WHERE name='Mykonos-Ire'").fetchone()[0]
    conn.execute("INSERT INTO entries (card_id,horse_id,post_position,morning_line_odds) VALUES (1,?,?,10)", (second_horse_id, 98))
    # Explicit test records make the candidate set deterministic; no fuzzy fallback exists.
    altered = card.records[0].__class__(**{**card.records[0].__dict__, "horse_name": "Mykonos IRE"})
    assert reconcile_twinspires_records(conn, 1, (altered,))[0].match_status == "AMBIGUOUS"
    unmatched = card.records[0].__class__(**{**card.records[0].__dict__, "horse_name": "No Such Horse"})
    result = validate_twinspires_card(conn, 1, card.__class__(**{**card.__dict__, "records": (unmatched,) + card.records[1:]}))
    assert not result.passed and any("unmatched" in error for error in result.errors)


def test_explicit_as_of_can_be_proven_but_unproven_is_not_score_evidence():
    conn = _conn()
    proven = parse_twinspires_markdown(TS, declared_as_of="2026-09-04T12:00:00Z")
    assert proven.as_of_status == "PROVEN"
    unproven = parse_twinspires_markdown(TS)
    validation = validate_twinspires_card(conn, 1, unproven)
    artifact_id = persist_twinspires_card(conn, unproven, validation, raw_bytes=TS.read_bytes())
    assert validation.passed is False
    assert conn.execute("SELECT COUNT(*) FROM source_feature_candidates WHERE observation_id IN (SELECT observation_id FROM source_observations WHERE artifact_id=?)", (artifact_id,)).fetchone()[0] == 0


def test_matching_unproven_source_hydrates_only_isolated_candidates_not_runtime_features():
    conn = _conn(); parsed = parse_twinspires_markdown(TS)
    names = [row[0] for row in conn.execute("SELECT h.name FROM entries e JOIN horses h ON h.horse_id=e.horse_id WHERE e.card_id=1 ORDER BY e.post_position")]
    records = tuple(record.__class__(**{**record.__dict__, "horse_name": names[idx]}) for idx, record in enumerate(parsed.records))
    card = parsed.__class__(**{**parsed.__dict__, "records": records})
    validation = validate_twinspires_card(conn, 1, card)
    assert validation.passed
    artifact_id = persist_twinspires_card(conn, card, validation, raw_bytes=TS.read_bytes())
    assert conn.execute("SELECT COUNT(*) FROM source_feature_candidates WHERE observation_id IN (SELECT observation_id FROM source_observations WHERE artifact_id=?) AND as_of_status='UNPROVEN'", (artifact_id,)).fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM feature_store").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_feature_candidates WHERE feature_name IN ('beyer_last','pace_fit_score','pace_pressure','collapse_risk','sectional_pace','finish_energy')").fetchone()[0] == 0
