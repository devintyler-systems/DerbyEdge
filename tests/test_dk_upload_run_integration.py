"""Integration coverage for the same DK audit persistence path used by app.py."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.ingest.firstbet_pdf import bind_run_to_card
from src.services.pdf_ingest import pdf_upload_cache_key, persist_dk_upload_run
from src.services.run_mode import get_card_run_state


ROOT = Path(__file__).resolve().parents[1]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript("\n".join(line for line in schema.splitlines() if "journal_mode" not in line))
    return conn


def _dk_result() -> dict:
    runners = [{"horse_name": f"Horse {n}", "post_position": n, "morning_line": "5/1"} for n in range(1, 12)]
    runner_diagnostics = [
        {"horse_name_raw": runner["horse_name"], "past_performances_linked": 1}
        for runner in runners
    ]
    return {
        "ok": True,
        "track_code": "IND", "race_date": "2026-09-03", "race_number": 5,
        "raw_text": "dkhorse.com/bet/program/classic PROGRAM WORKOUTS",
        "runners": runners,
        "race": {"track_code": "IND", "race_number": 5},
        "parser": {"adapter_selected": "draftkings_pdf", "source_format": "dkhorse_program_pdf", "source_detection_signals": ["dkhorse_classic_url", "dk_program_heading"]},
        "parser_diagnostics": {
            "source_format": "dkhorse_program_pdf",
            "source_detection_signals": ["dkhorse_classic_url", "dk_program_heading"],
            "declared_field_size": 12, "active_entry_count": 11,
            "field_reconciliation_status": "unexplained",
            "runners": runner_diagnostics,
            "total_pp_records_found": 22, "total_pp_records_linked": 22,
            "starter_match_rate": 1.0, "block_reasons": [],
        },
    }


def test_dk_upload_persists_audit_and_card_run_state(tmp_path):
    result = persist_dk_upload_run(
        _dk_result(), pdf_bytes=b"dk-upload", filename="IND_DK_Horse_R5_9-3-26.pdf", runs_root=tmp_path,
    )
    assert result["feature_audit"]["source_format"] == "dkhorse_program_pdf"
    assert result["feature_audit"]["active_entry_count"] == 11
    assert result["feature_audit"]["total_pp_records_linked"] == 22

    conn = _conn()
    track_id = conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('Horseshoe Indianapolis', 'IND')").lastrowid
    card_id = conn.execute("INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size) VALUES (?, '2026-09-03', 5, 1320, 'dirt', 11)", (track_id,)).lastrowid
    bind_run_to_card(result["ingest_run_id"], card_id, runs_root=tmp_path)
    state = get_card_run_state(conn, card_id, runs_root=tmp_path)
    assert state.audit["source_format"] == "dkhorse_program_pdf"
    assert state.audit["active_entry_count"] == 11
    assert state.audit["total_pp_records_linked"] == 22
    conn.close()


def test_dk_cache_key_invalidates_when_pipeline_version_changes():
    old = pdf_upload_cache_key(b"same-file", "upload.pdf", pipeline_version="legacy_generic_v1")
    current = pdf_upload_cache_key(b"same-file", "upload.pdf", pipeline_version="dkhorse_sections_v1")
    assert old != current
