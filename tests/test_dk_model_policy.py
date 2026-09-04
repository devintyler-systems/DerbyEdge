"""Model-family separation regression tests for DraftKings-sourced cards."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "draftkings_racedata_pdfs" / "fixtures"
SAR = FIX / "SAR_DK_Horse_R10_9-3-26.pdf"
IND = FIX / "IND_DK_Horse_R5_9-3-26.pdf"


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


class _V:
    """Minimal FeatureVerification stand-in for policy unit tests."""
    def __init__(self, warnings=(), pace_state=None):
        self.warnings = tuple(warnings)
        self.pace_state = pace_state


def _seed_card(conn, surface="dirt", yards=1870):
    tid = conn.execute("INSERT INTO tracks (name, abbrev) VALUES ('T','SAR')").lastrowid
    return conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size) "
        "VALUES (?, '2026-09-03', 10, ?, ?, 12)", (tid, yards, surface),
    ).lastrowid


# 1. A field with real speed/pace/form signal selects the standard model family.

def test_complete_signal_selects_standard_model_family():
    from src.ingest.run_state import RunMode
    from src.services.dk_model_policy import decide_dk_model_policy

    conn = _conn()
    card_id = _seed_card(conn)
    v = _V(warnings=(), pace_state="PACE_OK")  # nothing degenerate
    d = decide_dk_model_policy(conn, card_id, RunMode.MODEL_READY_LIMITED, v)
    assert d.scoring_state is None
    assert d.model_family_selected == "dirt_route_stakes_v1"
    assert d.scoring_eligibility is True
    conn.close()


# 2. DK enriched card missing speed/pace/form/trip cannot select the standard model.

def test_dk_missing_core_families_cannot_select_standard_model():
    from src.ingest.run_state import RunMode
    from src.services.dk_model_policy import decide_dk_model_policy, FEATURE_LIMITED_NO_SCORING

    conn = _conn()
    card_id = _seed_card(conn)
    v = _V(
        warnings=(
            "FEATURE_DEGENERACY_WARNING: pace_fit is constant across entries.",
            "FEATURE_DEGENERACY_WARNING: form is constant across entries.",
        ),
        pace_state="PACE_UNAVAILABLE",
    )
    d = decide_dk_model_policy(conn, card_id, RunMode.MODEL_READY_LIMITED, v)
    assert d.scoring_state == FEATURE_LIMITED_NO_SCORING
    assert d.model_family_selected is None
    assert d.scoring_eligibility is False
    conn.close()


# 3 + 4. Proxy is chosen only when the artifact exists; otherwise no scoring.

def test_proxy_model_selected_only_when_registered():
    from src.ingest.run_state import RunMode
    from src.services.dk_model_policy import (
        decide_dk_model_policy, LIMITED_HISTORY_PROXY_FAMILY, FEATURE_LIMITED_NO_SCORING,
    )
    from src.utils.db import ensure_model_registry_columns

    conn = _conn()
    card_id = _seed_card(conn)
    v = _V(
        warnings=(
            "FEATURE_DEGENERACY_WARNING: pace_fit is constant across entries.",
            "FEATURE_DEGENERACY_WARNING: form is constant across entries.",
        ),
        pace_state="PACE_UNAVAILABLE",
    )

    # no proxy registered -> no scoring, no probabilities
    d0 = decide_dk_model_policy(conn, card_id, RunMode.MODEL_READY_LIMITED, v)
    assert d0.scoring_state == FEATURE_LIMITED_NO_SCORING
    assert "win_probabilities" in d0.disabled_capability_reasons
    assert d0.calibration_version is None

    # register a calibrated proxy -> it is selected, wagering still off
    ensure_model_registry_columns(conn)
    # (model_family CHECK constraint does not yet include the proxy family;
    #  register under an allowed family with the proxy name until it is widened)
    conn.execute(
        "INSERT INTO model_registry (model_name, model_family, version, "
        "feature_schema_version, calibration_artifact_path) VALUES "
        "('limited_history_proxy_v1', 'fallback', '1.0.0', 'lh_proxy_v1', 'cal/lh_proxy_v1.json')",
    )
    conn.commit()
    d1 = decide_dk_model_policy(conn, card_id, RunMode.MODEL_READY_LIMITED, v)
    assert d1.scoring_state is None
    assert d1.model_family_selected == LIMITED_HISTORY_PROXY_FAMILY
    assert d1.model_version == "1.0.0"
    assert d1.calibration_version == "cal/lh_proxy_v1.json"
    assert d1.confidence_tier == "limited_data_proxy"
    assert d1.scoring_eligibility is True
    assert d1.betting_eligibility is False
    conn.close()


# 5. DK cards always have betting_eligibility = false.

@pytest.mark.parametrize("mode_name", ["MODEL_READY_LIMITED", "MODEL_READY"])
def test_dk_betting_eligibility_always_false(mode_name):
    from src.ingest.run_state import RunMode
    from src.services.dk_model_policy import decide_dk_model_policy

    conn = _conn()
    card_id = _seed_card(conn)
    v = _V(warnings=(), pace_state="PACE_OK")
    d = decide_dk_model_policy(
        conn, card_id, getattr(RunMode, mode_name), v, has_live_odds=True,
    )
    assert d.betting_eligibility is False
    assert "betting" in d.disabled_capability_reasons
    conn.close()


# 6. Re-render preserves model selection + feature availability from the exact run.

def test_rerender_preserves_policy_from_bound_run(tmp_path):
    from src.services.ingest_upload import ingest_uploaded_race_pdf
    from src.services.dk_enrichment import enrich_card_from_ingestion_run
    from src.services.run_mode import get_card_run_state

    conn = _conn()
    out = ingest_uploaded_race_pdf(SAR.read_bytes(), filename=SAR.name, conn=conn, runs_root=tmp_path)
    enrich_card_from_ingestion_run(conn, out["card_id"], runs_root=tmp_path)

    s1 = get_card_run_state(conn, out["card_id"], runs_root=tmp_path)
    s2 = get_card_run_state(conn, out["card_id"], runs_root=tmp_path)
    assert s1.scoring_state == s2.scoring_state == "FEATURE_LIMITED_NO_SCORING"
    assert s1.audit["feature_availability_mask"] == s2.audit["feature_availability_mask"]
    assert s1.audit["model_family_selected"] == s2.audit["model_family_selected"]
    assert s1.audit["ingestion_run_id"] == s2.audit["ingestion_run_id"] == out["ingestion_run_id"]
    assert s1.audit["betting_eligibility"] is False
    assert s1.audit["scoring_eligibility"] is False
    conn.close()


# 7. Feature-missing values stay null with availability flags; never numeric zero.

def test_missing_features_stay_null_with_flags_not_zero(tmp_path):
    from src.services.ingest_upload import ingest_uploaded_race_pdf
    from src.services.dk_enrichment import enrich_card_from_ingestion_run

    conn = _conn()
    out = ingest_uploaded_race_pdf(SAR.read_bytes(), filename=SAR.name, conn=conn, runs_root=tmp_path)
    enrich_card_from_ingestion_run(conn, out["card_id"], runs_root=tmp_path)

    rows = conn.execute(
        """SELECT recent_finish_percentile_w, distance_fit_eb, class_delta_last_to_today,
                  speed_last, beyer_last, has_completed_start_history,
                  speed_figure_available, pace_figure_available
           FROM feature_store WHERE card_id=? AND runner_data_status='resolved_no_history'""",
        (out["card_id"],),
    ).fetchall()
    assert rows
    for r in rows:
        assert r["has_completed_start_history"] == 0
        assert r["speed_figure_available"] == 0
        assert r["pace_figure_available"] == 0
        for col in ("recent_finish_percentile_w", "distance_fit_eb",
                    "class_delta_last_to_today", "speed_last", "beyer_last"):
            assert r[col] is None, f"{col} should be NULL, got {r[col]!r}"
    conn.close()
