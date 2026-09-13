"""Canonical persistence and readiness contracts for validated DK Markdown cards."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest.draftkings_markdown import parse_draftkings_markdown, validate_draftkings_markdown_card
from src.services.draftkings_markdown_intake import (
    markdown_card_score_readiness,
    persist_validated_draftkings_markdown,
    exportable_markdown_card_entries,
    reprocess_stored_draftkings_markdown_import,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R6_9-4-26.md"
AS_OF = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    schema = "\n".join(line for line in schema.splitlines() if "journal_mode" not in line)
    conn.executescript(schema)
    return conn


def _validated_card():
    card = parse_draftkings_markdown(FIXTURE, as_of=AS_OF)
    validation = validate_draftkings_markdown_card(card)
    assert validation.passed, validation.errors
    return card, validation


def test_valid_markdown_persists_entries_connections_history_and_export_fields():
    conn = _conn()
    card, validation = _validated_card()

    result = persist_validated_draftkings_markdown(
        conn, card, validation, source_filename=FIXTURE.name, source_path=str(FIXTURE),
    )
    rows = exportable_markdown_card_entries(conn, result.card_id)

    assert result.persisted_runner_count == 10
    assert len(rows) == 10
    assert all(row["Trainer"] for row in rows)
    assert all(row["Jockey"] for row in rows)
    assert all(row["Morning Line"] is not None for row in rows)
    assert all(row["ML-Implied Probability"] is not None for row in rows)
    assert all(row["Model Probability"] is None for row in rows)
    assert result.persisted_past_performance_count == len(card.past_performances)
    assert result.persisted_workout_count == len(card.workouts)
    first = card.entries[0]
    assert first.owner == "Lessell Edward R and Mary Jo"
    assert first.horse_profile.sire == "Summer Front"
    assert first.record_splits["life"].starts == 8
    assert first.record_splits["dirt"].earnings == 24035
    profile = conn.execute(
        "SELECT owner_name, breeder_name, record_splits_json, source_as_of FROM dk_horse_profile_snapshots"
    ).fetchone()
    assert profile[0] == first.owner
    assert profile[1]
    assert '"life"' in profile[2]
    assert profile[3] == AS_OF.isoformat()
    canonical = conn.execute(
        "SELECT career_starts, career_wins, career_earnings, dirt_starts, dist_starts FROM entries ORDER BY post_position LIMIT 1"
    ).fetchone()
    assert tuple(canonical) == (8, 1, 26547, 4, 3)
    assert conn.execute("SELECT COUNT(*) FROM horse_starts WHERE trip_comment IS NOT NULL").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM workouts WHERE raw_time IS NOT NULL AND rank_denominator IS NOT NULL AND time_seconds IS NOT NULL").fetchone()[0] > 0
    ranked_workout = conn.execute(
        "SELECT source_rank, rank_denominator FROM workouts "
        "WHERE source_rank IS NOT NULL AND rank_denominator IS NOT NULL "
        "ORDER BY workout_id LIMIT 1"
    ).fetchone()
    assert tuple(ranked_workout) == (95, 115)
    readiness = markdown_card_score_readiness(conn, result.card_id)
    assert readiness.score_eligible is True
    conn.close()


def test_reimport_repairs_only_missing_workout_rank_denominator():
    conn = _conn()
    card, validation = _validated_card()
    persisted = persist_validated_draftkings_markdown(
        conn, card, validation, source_filename=FIXTURE.name, source_path=str(FIXTURE),
    )
    target = conn.execute(
        "SELECT workout_id, source_row_id FROM workouts "
        "WHERE source_rank=95 AND rank_denominator=115 LIMIT 1"
    ).fetchone()
    assert target is not None

    # Simulate the pre-fix persisted row.  The repeated import reaches the
    # source_row_id conflict path rather than inserting a duplicate.
    conn.execute("UPDATE workouts SET rank_denominator=NULL WHERE workout_id=?", (target[0],))
    persist_validated_draftkings_markdown(
        conn, card, validation, source_filename=FIXTURE.name, source_path=str(FIXTURE),
    )

    repaired = conn.execute(
        "SELECT rank_denominator FROM workouts WHERE workout_id=?", (target[0],)
    ).fetchone()
    assert repaired[0] == 115
    assert conn.execute(
        "SELECT COUNT(*) FROM workouts WHERE source_row_id=?", (target[1],)
    ).fetchone()[0] == 1
    assert persisted.card_id
    conn.close()


def test_existing_partial_card_is_repaired_and_identical_sha_is_idempotent():
    conn = _conn()
    card, validation = _validated_card()
    conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('Saratoga', 'SAR')")
    track_id = conn.execute("SELECT track_id FROM tracks WHERE abbrev='SAR'").fetchone()[0]
    conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface) VALUES (?, ?, 6, 1760, 'dirt')",
        (track_id, card.race.race_date.isoformat()),
    )
    card_id = conn.execute("SELECT card_id FROM race_cards").fetchone()[0]
    conn.execute("INSERT INTO horses (name) VALUES (?)", (card.entries[0].horse_name,))
    horse_id = conn.execute("SELECT horse_id FROM horses").fetchone()[0]
    conn.execute(
        "INSERT INTO entries (card_id, horse_id, post_position, morning_line_odds) VALUES (?, ?, ?, 20)",
        (card_id, horse_id, card.entries[0].post_position),
    )
    conn.commit()

    repaired = persist_validated_draftkings_markdown(conn, card, validation, source_filename=FIXTURE.name)
    repeated = persist_validated_draftkings_markdown(conn, card, validation, source_filename=FIXTURE.name)
    hydrated = exportable_markdown_card_entries(conn, card_id)

    assert repaired.card_id == card_id
    assert repaired.repaired_existing_card is True
    assert repaired.materially_different_from_existing is True
    assert len(hydrated) == 10
    assert all(row["Trainer"] and row["Jockey"] for row in hydrated)
    assert repeated.already_imported is True
    assert repeated.materially_different_from_existing is False
    assert conn.execute("SELECT COUNT(*) FROM dk_markdown_imports").fetchone()[0] == 1
    conn.close()


def test_score_readiness_has_visible_precise_blockers_for_incomplete_persistence():
    conn = _conn()
    card, validation = _validated_card()
    result = persist_validated_draftkings_markdown(conn, card, validation, source_filename=FIXTURE.name)
    conn.execute("UPDATE entries SET trainer_id=NULL WHERE card_id=? AND post_position=1", (result.card_id,))
    conn.commit()

    readiness = markdown_card_score_readiness(conn, result.card_id)

    assert readiness.score_eligible is False
    assert any(blocker.code == "MISSING_TRAINER" and "Magnum's Macrobrst" in blocker.message for blocker in readiness.blockers)
    assert readiness.active_runner_count == 10
    conn.close()


def test_failed_validation_cannot_be_persisted_or_become_score_ready():
    conn = _conn()
    card = parse_draftkings_markdown("Saratoga\nRACE 6\n", source_path="bad.md", as_of=AS_OF)
    validation = validate_draftkings_markdown_card(card)

    with pytest.raises(ValueError, match="validation did not pass"):
        persist_validated_draftkings_markdown(conn, card, validation, source_filename="bad.md")
    assert conn.execute("SELECT COUNT(*) FROM race_cards").fetchone()[0] == 0
    conn.close()


def test_same_sha_new_parser_revision_reprocesses_without_duplicate_history_or_workouts():
    conn = _conn()
    card, validation = _validated_card()
    first = persist_validated_draftkings_markdown(
        conn, card, validation, source_filename=FIXTURE.name, source_path=str(FIXTURE)
    )
    starts_before = first.persisted_past_performance_count
    workouts_before = first.persisted_workout_count
    # Pretend an older source revision exists; current parser version must
    # create one auditable replacement extraction, not duplicate natural rows.
    conn.execute(
        "UPDATE dk_markdown_import_revisions SET parser_version='1.0.0' WHERE revision_id=?",
        (first.import_id,),
    )
    conn.commit()
    reprocessed = reprocess_stored_draftkings_markdown_import(conn, card.source_sha256)
    assert reprocessed.already_imported is False
    assert reprocessed.persisted_past_performance_count == starts_before
    assert reprocessed.persisted_workout_count == workouts_before
    assert conn.execute("SELECT COUNT(*) FROM dk_markdown_import_revisions").fetchone()[0] == 2
    conn.close()
