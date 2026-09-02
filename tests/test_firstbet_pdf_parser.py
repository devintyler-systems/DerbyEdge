from __future__ import annotations

from pathlib import Path

from src.ingest.firstbet_pdf import (
    bind_run_to_card,
    ingest_firstbet_pdf,
    parse_firstbet_text,
)
from src.ingest.run_state import RunMode
from src.utils.horse_norm import horse_key


FIXTURE = Path(__file__).parent / "fixtures" / "Saratoga_R8_9-2-26.txt"
STACKED_FIXTURE = Path(__file__).parent / "fixtures" / "Saratoga_R9_9-2-26.txt"


def _parse(text: str | None = None):
    return parse_firstbet_text(
        text if text is not None else FIXTURE.read_text(encoding="utf-8"),
        filename="Saratoga_R8_9-2-26.pdf",
        sha256="fixture-sha256",
        uploaded_at_utc="2026-09-02T20:24:00Z",
    )


def _parse_stacked():
    return parse_firstbet_text(
        STACKED_FIXTURE.read_text(encoding="utf-8"),
        filename="Saratoga_R9_9-2-26.pdf",
        sha256="stacked-fixture-sha256",
        uploaded_at_utc="2026-09-02T20:24:00Z",
    )


def test_saratoga_race_and_all_nine_entries_parse():
    payload, audit = _parse()
    race = payload["race"]
    entries = payload["entries"]

    assert race == {
        "track_code": "SAR",
        "track_name": "Saratoga",
        "race_number": 8,
        "race_date": "2026-09-02",
        "post_time_local": "14:12",
        "class_family": "AOC",
        "purse_usd": 110000,
        "distance_furlongs": 6.5,
        "surface": "dirt",
        "going": "muddy",
        "field_size_declared": 9,
    }
    assert len(entries) == 9
    assert [entry["post"] for entry in entries] == list(range(1, 10))
    assert all(
        entry["horse_raw"] and entry["trainer"] and entry["jockey"]
        and entry["morning_line_decimal"]
        for entry in entries
    )
    assert audit["entries_parsed"] == 9
    assert audit["field_size_declared"] == 9


def test_pps_attach_to_every_starter_and_preserve_source_fields():
    payload, audit = _parse()
    assert audit["entries_with_pp_history"] == 9
    assert audit["starter_match_rate"] == 1.0
    assert audit["total_pp_starts_parsed"] == 42

    grace = next(entry for entry in payload["entries"] if entry["horse_key"] == "GRACE_AND_GRIT")
    start = grace["past_performances"][0]
    assert start == {
        "start_date": "2026-08-02",
        "track_name": "Saratoga",
        "finish_position": 2,
        "field_size": 6,
        "class_family": "ALLOWANCE",
        "purse_usd": 110000,
        "distance_furlongs": 6.5,
        "surface": "dirt",
        "going": "fast",
        "odds_fractional": "7/2",
        "trip_comment": "bmp brk,dropped back,settled at rear,3p turn,2p1/4,up for 2nd",
    }


def test_ml_source_and_normalized_values_remain_separate():
    payload, _ = _parse()
    kay = payload["entries"][0]
    assert kay["morning_line_source_text"] == "4"
    assert kay["morning_line_text"] == "4-1"
    assert kay["morning_line_decimal"] == 5.0


def test_name_normalization_removes_apostrophes_without_splitting_token():
    assert horse_key("Rina's Revenge") == "RINAS_REVENGE"
    payload, _ = _parse()
    assert payload["entries"][-1]["horse_key"] == "RINAS_REVENGE"


def test_stacked_saratoga_r9_header_and_entries_are_normalized():
    payload, audit = _parse_stacked()

    assert payload["race"] == {
        "track_code": "SAR",
        "track_name": "Saratoga",
        "race_number": 9,
        "race_date": "2026-09-02",
        "post_time_local": "14:46",
        "class_family": "CLM",
        "purse_usd": 55000,
        "distance_furlongs": 8.5,
        "surface": "turf",
        "going": "yielding",
        "field_size_declared": 13,
    }
    assert len(payload["entries"]) == 13
    assert [entry["post"] for entry in payload["entries"]] == list(range(1, 14))
    assert audit["diagnostics"]["entry_parser_strategy"] == "stacked"
    assert audit["entries_parsed"] == 13
    assert audit["field_size_declared_raw"] == 13
    assert audit["active_entries"] == 10
    assert audit["scratches"] == 3
    assert audit["run_mode"] == RunMode.PP_PARSED_FEATURES_PENDING.value


def test_stacked_saratoga_r9_retains_source_scratches_but_audits_active_only():
    payload, audit = _parse_stacked()
    by_post = {entry["post"]: entry for entry in payload["entries"]}

    assert {by_post[post]["horse_raw"] for post in (11, 12, 13)} == {
        "DUCKY MEDWICK", "HOT PROPERTY", "CULPRIT"
    }
    assert all(by_post[post]["is_scratched"] is True for post in (11, 12, 13))
    assert all(by_post[post]["scratch_source"] == "1stbet_pdf_scr" for post in (11, 12, 13))
    assert all(by_post[post]["is_scratched"] is False for post in range(1, 11))
    assert audit["entries_with_pp_history"] == 10
    assert audit["starter_match_rate"] == 1.0
    assert audit["feature_coverage"]["recent_form"] == 1.0


def test_every_upload_attempt_persists_both_audit_artifacts(tmp_path):
    result = ingest_firstbet_pdf(
        b"not-a-pdf",
        filename="bad.pdf",
        runs_root=tmp_path,
        uploaded_at_utc="2026-09-02T20:24:00Z",
        run_id="bad-upload",
    )
    assert result["ok"] is False
    assert (tmp_path / "bad-upload" / "parsed_pp.json").exists()
    assert (tmp_path / "bad-upload" / "feature_audit.json").exists()


def test_card_binding_does_not_mutate_immutable_ingest_audit(tmp_path):
    result = ingest_firstbet_pdf(
        b"not-a-pdf",
        filename="bad.pdf",
        runs_root=tmp_path,
        uploaded_at_utc="2026-09-02T20:24:00Z",
        run_id="immutable-upload",
    )
    audit_path = tmp_path / result["run_id"] / "feature_audit.json"
    before = audit_path.read_bytes()
    bind_run_to_card(result["run_id"], 8, runs_root=tmp_path)
    assert audit_path.read_bytes() == before
    assert (audit_path.parent / "card_binding.json").exists()
