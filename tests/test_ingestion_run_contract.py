"""End-to-end contract tests for the immutable ingestion-run pipeline.

These tests drive the real upload service entry point
(``ingest_uploaded_race_pdf``), the real persistence layer, real card
creation/re-sync, session-state selection, and the blocked/model-ready render
source. The DraftKings parser is treated as frozen producer behaviour — a
mismatch between parse-time and render-time values must fail the test, never be
compensated by editing the parser.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "draftkings_racedata_pdfs" / "fixtures"
SAR_R10 = FIXTURES / "SAR_DK_Horse_R10_9-3-26.pdf"
IND_R5 = FIXTURES / "IND_DK_Horse_R5_9-3-26.pdf"
DMR_R4 = FIXTURES / "DMR_DK_Horse_R4_9-3-26.pdf"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript("\n".join(l for l in schema.splitlines() if "journal_mode" not in l))
    return conn


# ── build_ingestion_run: pure derivation from a real parse result ──────────────

def test_build_ingestion_run_from_real_sar_parse_preserves_parse_values():
    from src.services.pdf_ingest import parse_race_pdf, PARSER_PIPELINE_VERSION
    from src.ingest.ingestion_run import build_ingestion_run

    pdf_bytes = SAR_R10.read_bytes()
    parse_result = parse_race_pdf(pdf_bytes, filename=SAR_R10.name)

    run = build_ingestion_run(pdf_bytes, filename=SAR_R10.name, parse_result=parse_result)

    import hashlib
    assert run.upload_sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert run.parser_pipeline_version == PARSER_PIPELINE_VERSION
    assert run.source_format == "dkhorse_program_pdf"
    assert run.parse_status == "parsed"
    assert run.race_key == "SAR|2026-09-03|R10"
    assert run.feature_audit["active_entry_count"] == 12
    assert run.feature_audit["total_pp_records_linked"] == 46
    assert run.feature_audit["identity_resolution_rate"] >= 0.90
    assert run.normalized_race_payload["runners_count"] == 12
    assert run.error is None


def test_persist_and_load_ingestion_run_round_trips_by_id(tmp_path):
    from src.services.pdf_ingest import parse_race_pdf
    from src.ingest.ingestion_run import (
        build_ingestion_run,
        persist_ingestion_run,
        load_ingestion_run,
    )

    pdf_bytes = SAR_R10.read_bytes()
    parse_result = parse_race_pdf(pdf_bytes, filename=SAR_R10.name)
    run = build_ingestion_run(pdf_bytes, filename=SAR_R10.name, parse_result=parse_result)

    paths = persist_ingestion_run(run, runs_root=tmp_path)
    loaded = load_ingestion_run(run.ingestion_run_id, runs_root=tmp_path)

    assert loaded == run
    assert paths["audit_sha256"] and paths["payload_sha256"]
    # second persist of the same id must not silently overwrite
    with pytest.raises(FileExistsError):
        persist_ingestion_run(run, runs_root=tmp_path)


def test_validate_ingestion_run_fails_closed_on_hash_or_version_mismatch(tmp_path):
    from src.services.pdf_ingest import parse_race_pdf
    from src.ingest.ingestion_run import (
        build_ingestion_run,
        validate_ingestion_run,
        IngestionRunBindingInvalid,
    )

    pdf_bytes = SAR_R10.read_bytes()
    parse_result = parse_race_pdf(pdf_bytes, filename=SAR_R10.name)
    run = build_ingestion_run(pdf_bytes, filename=SAR_R10.name, parse_result=parse_result)

    validate_ingestion_run(run, upload_sha256=run.upload_sha256,
                           parser_pipeline_version=run.parser_pipeline_version)
    with pytest.raises(IngestionRunBindingInvalid):
        validate_ingestion_run(run, upload_sha256="deadbeef")
    with pytest.raises(IngestionRunBindingInvalid):
        validate_ingestion_run(run, parser_pipeline_version="dkhorse_sections_v99")
    with pytest.raises(IngestionRunBindingInvalid):
        validate_ingestion_run(None)


# ── Spec Test 1: SAR valid DK run survives to render ──────────────────────────

def test_1_sar_valid_dk_run_survives_to_render(tmp_path):
    from src.services.pdf_ingest import parse_race_pdf
    from src.services.ingest_upload import ingest_uploaded_race_pdf
    from src.ingest.ingestion_run import load_ingestion_run
    from src.services.run_mode import get_card_run_state

    conn = _conn()
    pdf_bytes = SAR_R10.read_bytes()

    # Producer baseline: the frozen parser's parse-time values.
    parse_result = parse_race_pdf(pdf_bytes, filename=SAR_R10.name)
    d = parse_result["parser_diagnostics"]
    assert d["source_format"] == "dkhorse_program_pdf"
    assert d["active_entry_count"] == 12
    assert d["total_pp_records_linked"] > 0
    assert d["identity_resolution_rate"] >= 0.90

    out = ingest_uploaded_race_pdf(
        pdf_bytes, filename=SAR_R10.name, conn=conn, runs_root=tmp_path,
    )
    session_state: dict = {}
    session_state["ingestion_run_id"] = out["ingestion_run_id"]

    persisted = load_ingestion_run(session_state["ingestion_run_id"], runs_root=tmp_path)
    assert persisted.source_format == "dkhorse_program_pdf"
    assert persisted.feature_audit["active_entry_count"] == 12
    assert persisted.feature_audit["total_pp_records_linked"] == d["total_pp_records_linked"]
    assert persisted.feature_audit["identity_resolution_rate"] >= 0.90

    # Card is bound to the exact run id
    row = conn.execute(
        "SELECT ingestion_run_id FROM race_cards WHERE card_id=?", (out["card_id"],)
    ).fetchone()
    assert row[0] == persisted.ingestion_run_id

    # Render-time state reads the immutable run by id and equals parse-time values
    state = get_card_run_state(conn, out["card_id"], runs_root=tmp_path)
    assert state.audit["source_format"] == "dkhorse_program_pdf"
    assert state.audit["active_entry_count"] == 12
    assert state.audit["total_pp_records_linked"] == d["total_pp_records_linked"]
    assert state.audit["identity_resolution_rate"] >= 0.90
    assert state.audit["total_pp_records_linked"] != 0
    # not the generic 1/ST guidance, and not a false 0-PP claim
    from src.app.board_formatting import blocked_state_guidance, GENERIC_BLOCKED_GUIDANCE
    guidance = blocked_state_guidance(state.audit)
    assert guidance != GENERIC_BLOCKED_GUIDANCE
    conn.close()


# ── Spec Test 2: same race key, different uploads ─────────────────────────────

def test_2_same_race_key_render_uses_only_latest_ingestion_run(tmp_path):
    from src.services.ingest_upload import ingest_uploaded_race_pdf
    from src.ingest.ingestion_run import (
        build_ingestion_run, persist_ingestion_run, bind_card_to_ingestion_run,
        card_ingestion_run_id,
    )
    from src.services.run_mode import get_card_run_state

    conn = _conn()
    pdf_bytes = SAR_R10.read_bytes()

    # First upload: a stale/failed parse for this race key with 0 linked PPs.
    stale_parse = {
        "ok": True, "error": None, "is_draftkings": True,
        "track_code": "SAR", "race_date": "2026-09-03", "race_number": 10,
        "distance_text": "1 1/16 M", "surface": "Dirt",
        "runners": [{"horse_name": f"H{n}", "post_position": n, "morning_line": "5"} for n in range(1, 13)],
        "race": {"runners_count": 12, "runners": []},
        "parser": {"adapter_selected": "draftkings_pdf"},
        "parser_diagnostics": {
            "source_format": "dkhorse_program_pdf", "run_mode": "BLOCKED",
            "active_entry_count": 12, "declared_field_size": 12,
            "total_pp_records_found": 0, "total_pp_records_linked": 0,
            "identity_resolution_rate": 0.0, "starter_match_rate": 0.0,
            "runners": [], "block_reasons": ["No usable past-performance rows are linked in an experienced field."],
            "field_reconciliation_status": "exact",
        },
    }
    stale_run = build_ingestion_run(pdf_bytes, filename=SAR_R10.name, parse_result=stale_parse)
    persist_ingestion_run(stale_run, runs_root=tmp_path)

    track_id = conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('Saratoga','SAR')").lastrowid
    card_id = conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size) "
        "VALUES (?, '2026-09-03', 10, 1870, 'dirt', 12)", (track_id,),
    ).lastrowid
    conn.commit()
    bind_card_to_ingestion_run(conn, card_id, stale_run.ingestion_run_id)

    # Second upload: the real, valid DK parse for the same race key.
    out = ingest_uploaded_race_pdf(pdf_bytes, filename=SAR_R10.name, conn=conn, runs_root=tmp_path)
    assert out["card_id"] == card_id  # re-sync, same race key

    bound = card_ingestion_run_id(conn, card_id)
    assert bound == out["ingestion_run_id"]
    assert bound != stale_run.ingestion_run_id

    state = get_card_run_state(conn, card_id, runs_root=tmp_path)
    assert state.audit["ingestion_run_id"] == out["ingestion_run_id"]
    assert state.audit["total_pp_records_linked"] > 0
    conn.close()


# ── Spec Test 3: same upload bytes, changed parser version ────────────────────

def test_3_same_bytes_new_parser_version_creates_separate_run(tmp_path):
    from src.services.ingest_upload import ingest_uploaded_race_pdf
    from src.ingest.ingestion_run import load_ingestion_run

    conn = _conn()
    pdf_bytes = SAR_R10.read_bytes()

    v1 = ingest_uploaded_race_pdf(
        pdf_bytes, filename=SAR_R10.name, conn=conn, runs_root=tmp_path,
        parser_pipeline_version="dkhorse_sections_v1",
    )
    v2 = ingest_uploaded_race_pdf(
        pdf_bytes, filename=SAR_R10.name, conn=conn, runs_root=tmp_path,
        parser_pipeline_version="dkhorse_sections_v2",
    )

    assert v1["ingestion_run_id"] != v2["ingestion_run_id"]
    assert v1["upload_sha256"] == v2["upload_sha256"]

    r1 = load_ingestion_run(v1["ingestion_run_id"], runs_root=tmp_path)
    r2 = load_ingestion_run(v2["ingestion_run_id"], runs_root=tmp_path)
    assert r1.parser_pipeline_version == "dkhorse_sections_v1"
    assert r2.parser_pipeline_version == "dkhorse_sections_v2"

    # Separate immutable directories — the newer run never reuses the old files.
    assert (tmp_path / v1["ingestion_run_id"] / "ingestion_run.json").exists()
    assert (tmp_path / v2["ingestion_run_id"] / "ingestion_run.json").exists()
    assert v1["audit_sha256"] == v2["audit_sha256"]  # same parse -> same audit content
    # ...but they are physically distinct records, each stamped with its version
    assert r1.created_at_utc != r2.created_at_utc or r1.ingestion_run_id != r2.ingestion_run_id
    conn.close()


# ── Spec Test 4: card binding mismatch fails closed ──────────────────────────

def test_4_card_bound_to_missing_run_fails_closed(tmp_path):
    from src.ingest.ingestion_run import bind_card_to_ingestion_run
    from src.services.run_mode import get_card_run_state
    from src.app.board_formatting import blocked_state_guidance

    conn = _conn()
    track_id = conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('Saratoga','SAR')").lastrowid
    card_id = conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size) "
        "VALUES (?, '2026-09-03', 10, 1870, 'dirt', 12)", (track_id,),
    ).lastrowid
    conn.commit()
    bind_card_to_ingestion_run(conn, card_id, "ing-doesnotexist-000000")

    state = get_card_run_state(conn, card_id, runs_root=tmp_path)
    from src.ingest.run_state import RunMode
    assert state.mode == RunMode.BLOCKED
    assert any("ingestion_run_binding_invalid" in r for r in state.reasons)
    assert state.audit.get("binding_invalid") is True

    guidance = blocked_state_guidance(state.audit)
    assert "Upload state mismatch" in guidance
    assert "not bound to the current immutable ingestion result" in guidance
    # must NOT present false zero-PP parser diagnostics
    assert "past-performance sections could not yet be linked" not in guidance
    conn.close()


# ── Spec Test 5: DK blocked messaging ────────────────────────────────────────

def test_5_dk_blocked_run_shows_dk_guidance_not_1stbet(tmp_path):
    from src.ingest.ingestion_run import (
        build_ingestion_run, persist_ingestion_run, bind_card_to_ingestion_run,
    )
    from src.services.run_mode import get_card_run_state
    from src.app.board_formatting import (
        blocked_state_guidance, GENERIC_BLOCKED_GUIDANCE, DK_BLOCKED_GUIDANCE,
    )

    conn = _conn()
    pdf_bytes = b"%PDF-1.4 fake dk bytes"
    blocked_parse = {
        "ok": True, "error": None, "is_draftkings": True,
        "track_code": "IND", "race_date": "2026-09-03", "race_number": 5,
        "distance_text": "6 F", "surface": "Dirt",
        "runners": [{"horse_name": f"H{n}", "post_position": n, "morning_line": "5"} for n in range(1, 11)],
        "race": {"runners_count": 10, "runners": []},
        "parser": {"adapter_selected": "draftkings_pdf"},
        "parser_diagnostics": {
            "source_format": "dkhorse_program_pdf", "run_mode": "BLOCKED",
            "active_entry_count": 10, "declared_field_size": 10,
            "total_pp_records_found": 0, "total_pp_records_linked": 0,
            "identity_resolution_rate": 1.0, "starter_match_rate": 1.0,
            "experienced_field_pp_coverage": 0.0,
            "runners": [], "field_reconciliation_status": "exact",
            "block_reasons": ["No usable past-performance rows are linked in an experienced field."],
        },
    }
    run = build_ingestion_run(pdf_bytes, filename="IND_DK_Horse_R5_9-3-26.pdf", parse_result=blocked_parse)
    persist_ingestion_run(run, runs_root=tmp_path)

    track_id = conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('Horseshoe Indianapolis','IND')").lastrowid
    card_id = conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size) "
        "VALUES (?, '2026-09-03', 5, 1320, 'dirt', 10)", (track_id,),
    ).lastrowid
    conn.commit()
    bind_card_to_ingestion_run(conn, card_id, run.ingestion_run_id)

    state = get_card_run_state(conn, card_id, runs_root=tmp_path)
    from src.ingest.run_state import RunMode
    assert state.mode == RunMode.BLOCKED
    assert state.audit["source_format"] == "dkhorse_program_pdf"
    assert state.audit["total_pp_records_linked"] == 0

    guidance = blocked_state_guidance(state.audit)
    assert guidance != GENERIC_BLOCKED_GUIDANCE
    assert "1/ST" not in guidance
    assert "DraftKings" in guidance
    conn.close()


# ── Spec Test 6: existing non-DK behaviour unchanged ─────────────────────────

FIRSTBET_PDF = ROOT / "1stbet_racedata_pdfs" / "New Race" / "Belmont_R3_5-10-26.pdf"


@pytest.mark.skipif(not FIRSTBET_PDF.exists(), reason="1/ST fixture not present")
def test_6_native_1stbet_flow_unchanged(tmp_path):
    from src.services.ingest_upload import ingest_uploaded_race_pdf
    from src.services.run_mode import get_card_run_state

    conn = _conn()
    pdf_bytes = FIRSTBET_PDF.read_bytes()

    out = ingest_uploaded_race_pdf(pdf_bytes, filename=FIRSTBET_PDF.name, conn=conn, runs_root=tmp_path)
    assert out["parser_selected"] in ("firstbet_pdf", "draftkings_pdf", None)
    assert out["source_format"] != "dkhorse_program_pdf"
    assert out["card_id"] is not None

    state = get_card_run_state(conn, out["card_id"], runs_root=tmp_path)
    # native 1/ST card resolves to a real run-mode without raising
    assert state.mode is not None
    conn.close()
