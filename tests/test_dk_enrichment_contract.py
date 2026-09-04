"""End-to-end tests for DK enrichment bound to the immutable ingestion run.

Uses the real ingest_uploaded_race_pdf entry point, real persistence, real
enrichment, real feature build, and the real run-mode/scoring flow.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "draftkings_racedata_pdfs" / "fixtures"
SAR = FIX / "SAR_DK_Horse_R10_9-3-26.pdf"
IND = FIX / "IND_DK_Horse_R5_9-3-26.pdf"
DMR = FIX / "DMR_DK_Horse_R4_9-3-26.pdf"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript("\n".join(l for l in schema.splitlines() if "journal_mode" not in l))
    from src.services.results_intake import _ensure_table, ensure_race_review_view
    _ensure_table(conn)
    ensure_race_review_view(conn)
    return conn


def _upload(conn, pdf: Path, tmp_path):
    from src.services.ingest_upload import ingest_uploaded_race_pdf
    return ingest_uploaded_race_pdf(pdf.read_bytes(), filename=pdf.name, conn=conn, runs_root=tmp_path)


# ── Test 1: SAR DK fixture — full pipeline to MODEL_READY_LIMITED ─────────────

def test_1_sar_dk_enrichment_reaches_model_ready_limited(tmp_path):
    from src.services.dk_enrichment import enrich_card_from_ingestion_run
    from src.services.run_mode import get_card_run_state
    from src.ingest.run_state import RunMode

    conn = _conn()
    out = _upload(conn, SAR, tmp_path)
    card_id, run_id = out["card_id"], out["ingestion_run_id"]

    result = enrich_card_from_ingestion_run(conn, card_id, runs_root=tmp_path)
    assert result.state == "ENRICHED"
    assert result.ingestion_run_id == run_id
    assert result.horse_starts_written > 0
    assert result.linked_history > 0

    # canonical rows carry the same ingestion run id
    rows = conn.execute(
        "SELECT DISTINCT ingestion_run_id FROM horse_starts WHERE card_id=? AND source_provider='draftkings'",
        (card_id,),
    ).fetchall()
    assert [r[0] for r in rows] == [run_id]
    frows = conn.execute(
        "SELECT DISTINCT ingestion_run_id FROM feature_store WHERE card_id=?", (card_id,)
    ).fetchall()
    assert [r[0] for r in frows] == [run_id]

    state = get_card_run_state(conn, card_id, runs_root=tmp_path)
    # Model-family separation: DK PP has no speed/pace/form/trip and no proxy
    # model is registered, so the card is feature-limited and not scored.
    assert state.scoring_state == "FEATURE_LIMITED_NO_SCORING"
    assert state.scoring_eligible is False
    assert state.audit["betting_eligibility"] is False
    assert state.audit["model_family_selected"] is None

    # second enrichment / render must not duplicate rows
    before = conn.execute("SELECT COUNT(*) FROM horse_starts WHERE card_id=?", (card_id,)).fetchone()[0]
    enrich_card_from_ingestion_run(conn, card_id, runs_root=tmp_path)
    get_card_run_state(conn, card_id, runs_root=tmp_path)
    after = conn.execute("SELECT COUNT(*) FROM horse_starts WHERE card_id=?", (card_id,)).fetchone()[0]
    assert before == after
    conn.close()


# ── Test 2: IND and DMR use their own run IDs, never borrow SAR data ─────────

def test_2_ind_and_dmr_isolate_their_own_ingestion_runs(tmp_path):
    from src.services.dk_enrichment import enrich_card_from_ingestion_run

    conn = _conn()
    out_sar = _upload(conn, SAR, tmp_path)
    enrich_card_from_ingestion_run(conn, out_sar["card_id"], runs_root=tmp_path)

    for pdf in (IND, DMR):
        out = _upload(conn, pdf, tmp_path)
        res = enrich_card_from_ingestion_run(conn, out["card_id"], runs_root=tmp_path)
        assert res.state == "ENRICHED"
        assert res.ingestion_run_id == out["ingestion_run_id"]
        assert res.ingestion_run_id != out_sar["ingestion_run_id"]
        # every canonical + feature row for this card carries only this run id
        hs = conn.execute(
            "SELECT DISTINCT ingestion_run_id FROM horse_starts WHERE card_id=?",
            (out["card_id"],),
        ).fetchall()
        assert [r[0] for r in hs] == [out["ingestion_run_id"]]
        # source data actually contains linked history -> enrichment carries it
        assert res.linked_history > 0
        assert res.horse_starts_written > 0
    conn.close()


# ── Test 3: resolved-no-history runner keeps missingness, no zero imputation ──

def test_3_resolved_no_history_runner_preserves_missingness(tmp_path):
    from src.services.dk_enrichment import enrich_card_from_ingestion_run

    conn = _conn()
    out = _upload(conn, SAR, tmp_path)
    enrich_card_from_ingestion_run(conn, out["card_id"], runs_root=tmp_path)

    rows = conn.execute(
        """SELECT horse_name, runner_data_status, no_history_reason,
                  has_completed_start_history, workout_forward_low_history,
                  speed_figure_available, pace_figure_available,
                  recent_finish_percentile_w, distance_fit_eb, class_delta_last_to_today
           FROM feature_store WHERE card_id=? AND runner_data_status='resolved_no_history'""",
        (out["card_id"],),
    ).fetchall()
    assert rows, "SAR R10 has 3 resolved_no_history runners"
    for r in rows:
        assert r["has_completed_start_history"] == 0
        assert r["no_history_reason"] in ("explicit_no_races", "scratches_only")
        assert r["workout_forward_low_history"] == 1
        assert r["speed_figure_available"] == 0
        assert r["pace_figure_available"] == 0
        # PP-derived features stay NULL — never numeric zero
        assert r["recent_finish_percentile_w"] is None
        assert r["distance_fit_eb"] is None
        assert r["class_delta_last_to_today"] is None
    conn.close()


# ── Test 4: enrichment failure fails closed with DK-specific messaging ────────

def test_4_enrichment_failure_fails_closed_dk_specific(tmp_path, monkeypatch):
    from src.services import dk_enrichment
    from src.services.dk_enrichment import enrich_card_from_ingestion_run
    from src.services.run_mode import get_card_run_state
    from src.ingest.run_state import RunMode
    from src.app.board_formatting import blocked_state_guidance, GENERIC_BLOCKED_GUIDANCE

    conn = _conn()
    out = _upload(conn, SAR, tmp_path)
    card_id = out["card_id"]

    def _boom(_conn, _parsed):
        raise RuntimeError("simulated enrichment failure")

    monkeypatch.setattr(dk_enrichment, "ingest_draftkings_to_canonical", _boom, raising=False)
    # patch the name used inside the function's local import
    import src.services.draftkings_enrich as de
    monkeypatch.setattr(de, "ingest_draftkings_to_canonical", _boom, raising=True)

    res = enrich_card_from_ingestion_run(conn, card_id, runs_root=tmp_path)
    assert res.state == "FAILED"
    assert res.failure_reason and "simulated enrichment failure" in res.failure_reason

    state = get_card_run_state(conn, card_id, runs_root=tmp_path)
    assert state.mode == RunMode.BLOCKED
    guidance = blocked_state_guidance(state.audit)
    assert "pre-race feature enrichment failed" in guidance
    assert guidance != GENERIC_BLOCKED_GUIDANCE
    assert "1/ST" not in guidance
    # no unrelated DB starts leaked in
    assert conn.execute(
        "SELECT COUNT(*) FROM horse_starts WHERE card_id=?", (card_id,)
    ).fetchone()[0] == 0
    conn.close()


# ── Test 5: version / reprocess isolation ───────────────────────────────────

def test_5_reprocess_new_version_isolates_derived_data(tmp_path):
    from src.services.ingest_upload import ingest_uploaded_race_pdf
    from src.services.dk_enrichment import (
        enrich_card_from_ingestion_run, get_dk_enrichment_state,
    )
    from src.ingest.ingestion_run import load_ingestion_run

    conn = _conn()
    v1 = ingest_uploaded_race_pdf(SAR.read_bytes(), filename=SAR.name, conn=conn,
                                  runs_root=tmp_path, parser_pipeline_version="dkhorse_sections_v1")
    r1 = enrich_card_from_ingestion_run(conn, v1["card_id"], runs_root=tmp_path)
    assert r1.state == "ENRICHED"

    v2 = ingest_uploaded_race_pdf(SAR.read_bytes(), filename=SAR.name, conn=conn,
                                  runs_root=tmp_path, parser_pipeline_version="dkhorse_sections_v2")
    assert v2["card_id"] == v1["card_id"]
    assert v2["ingestion_run_id"] != v1["ingestion_run_id"]
    r2 = enrich_card_from_ingestion_run(conn, v2["card_id"], runs_root=tmp_path)
    assert r2.state == "ENRICHED"

    # historical v1 immutable run still reproducible
    old = load_ingestion_run(v1["ingestion_run_id"], runs_root=tmp_path)
    assert old.parser_pipeline_version == "dkhorse_sections_v1"
    # each run has its own enrichment record
    s1 = get_dk_enrichment_state(conn, v1["card_id"], v1["ingestion_run_id"])
    s2 = get_dk_enrichment_state(conn, v2["card_id"], v2["ingestion_run_id"])
    assert s1["enrichment_version"] == s2["enrichment_version"]
    assert s1["ingestion_run_id"] != s2["ingestion_run_id"]
    # feature_store now bound to the current (v2) run
    frows = conn.execute(
        "SELECT DISTINCT ingestion_run_id FROM feature_store WHERE card_id=?", (v1["card_id"],)
    ).fetchall()
    assert [r[0] for r in frows] == [v2["ingestion_run_id"]]
    conn.close()


# ── Test 6: live browser regression — the exact values captured live ─────────
#
# Values captured from live headless-Chromium uploads through the running
# Streamlit app (Market Intake -> Create/Re-sync -> Build features), 2026-09-03.
# This test reproduces them through the same service entry point so the live
# numbers are pinned as a regression baseline.

_LIVE_BASELINE = {
    "SAR_DK_Horse_R10_9-3-26.pdf": dict(
        sha12="c780c57faa12", active=12, found=63, linked=46, idres=1.0,
        hs_written=63, wo_written=206, linked_history=9, resolved_no_history=3,
    ),
    "IND_DK_Horse_R5_9-3-26.pdf": dict(
        sha12="12b268f4c77f", active=12, found=93, linked=71, idres=1.0,
        hs_written=93, wo_written=281, linked_history=12, resolved_no_history=0,
    ),
    "DMR_DK_Horse_R4_9-3-26.pdf": dict(
        sha12="60311f168ff1", active=8, found=86, linked=81, idres=1.0,
        hs_written=86, wo_written=319, linked_history=8, resolved_no_history=0,
    ),
}


@pytest.mark.parametrize("fname", list(_LIVE_BASELINE))
def test_6_live_browser_values_reproduce(fname, tmp_path):
    from src.services.ingest_upload import ingest_uploaded_race_pdf
    from src.services.dk_enrichment import enrich_card_from_ingestion_run
    from src.services.run_mode import get_card_run_state
    from src.ingest.run_state import RunMode

    exp = _LIVE_BASELINE[fname]
    conn = _conn()
    out = ingest_uploaded_race_pdf(
        (FIX / fname).read_bytes(), filename=fname, conn=conn, runs_root=tmp_path,
    )
    assert out["upload_sha256"][:12] == exp["sha12"]
    assert out["source_format"] == "dkhorse_program_pdf"
    assert out["parser_selected"] == "draftkings_pdf"
    assert out["active_entry_count"] == exp["active"]
    assert out["total_pp_records_found"] == exp["found"]
    assert out["total_pp_records_linked"] == exp["linked"]
    assert out["identity_resolution_rate"] == exp["idres"]

    res = enrich_card_from_ingestion_run(conn, out["card_id"], runs_root=tmp_path)
    assert res.state == "ENRICHED"
    assert res.horse_starts_written == exp["hs_written"]
    assert res.workouts_written == exp["wo_written"]
    assert res.linked_history == exp["linked_history"]
    assert res.resolved_no_history == exp["resolved_no_history"]

    state = get_card_run_state(conn, out["card_id"], runs_root=tmp_path)
    assert state.scoring_state == "FEATURE_LIMITED_NO_SCORING"
    assert state.audit["total_pp_records_linked"] == exp["linked"]
    assert state.audit["ingestion_run_id"] == out["ingestion_run_id"]
    conn.close()
