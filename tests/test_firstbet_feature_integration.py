from __future__ import annotations

import sqlite3
import inspect
from pathlib import Path

from src.features.builder import build_features
from src.models.scorer import score_race
from src.ingest.firstbet_pdf import parse_firstbet_text, to_legacy_race_result
from src.ingest.run_state import DataQuality, RunMode, resolve_mode_with_feature_checks
from src.services.feature_state import model_config_for_card, verify_feature_frame
from src.services.firstbet_enrich import enrich_entries_from_1stbet
from src.services.race_card_builder import find_or_create_race


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "Saratoga_R8_9-2-26.txt"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_saratoga_pps_feed_real_builder_and_nonconstant_core_features(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "saratoga.sqlite"
    conn = _connect(db_path)
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))

    payload, audit = parse_firstbet_text(
        FIXTURE.read_text(encoding="utf-8"),
        filename="Saratoga_R8_9-2-26.pdf",
        sha256="fixture",
        uploaded_at_utc="2026-09-02T20:24:00Z",
    )
    legacy = to_legacy_race_result(payload, audit)
    card_id, _, _, warnings = find_or_create_race(
        conn,
        legacy["track_code"],
        legacy["race_date"],
        legacy["race_number"],
        legacy["runners"],
        distance_yards=1430,
        surface="dirt",
        stakes_name="AOC",
        race_class="AOC",
        purse=110000,
        conditions="muddy",
        field_size=9,
    )
    assert not warnings
    enrichment = enrich_entries_from_1stbet(
        conn,
        card_id,
        legacy["runners"],
        race_date=legacy["race_date"],
        race_distance_yards=1430,
    )
    assert enrichment["ok"] is True
    assert enrichment["n_pp_rows"] == 42
    conn.close()

    monkeypatch.setattr("src.utils.db.get_connection", lambda: _connect(db_path))
    feat_df = build_features(card_id=card_id)

    check_conn = _connect(db_path)
    try:
        assert check_conn.execute(
            "SELECT COUNT(*) FROM firstbet_pp_starts WHERE card_id=?", (card_id,)
        ).fetchone()[0] == 42
        config = model_config_for_card(check_conn, card_id)
    finally:
        check_conn.close()

    verification = verify_feature_frame(
        feat_df,
        config,
        expected_entries=9,
        require_pp_backed_features=True,
    )
    varying_core = {
        name
        for name in ("pace_fit", "form", "surface_distance_fit")
        if len({row[name] for row in verification.core_rows}) > 1
    }
    pp_backed_columns = ("horses_beaten_pct_last", "form_cycle_idx", "distance_fit", "surface_fit")

    assert verification.schema_complete is True
    assert verification.entry_coverage_complete is True
    assert verification.passed is True
    assert verification.pp_backed_features_nonconstant is True
    assert varying_core
    assert any(feat_df[column].nunique(dropna=True) > 1 for column in pp_backed_columns)

    quality = DataQuality(
        entries_parsed=9,
        field_size_declared=9,
        entries_with_pp_history=9,
        starter_match_rate=1.0,
        race_metadata_complete=True,
        has_morning_lines=True,
        has_live_odds=False,
        required_model_features_complete=verification.passed,
    )
    mode, _ = resolve_mode_with_feature_checks(quality, verification.core_rows)
    assert mode == RunMode.MODEL_READY_LIMITED


def test_score_race_feature_gate_precedes_every_model_call():
    source = inspect.getsource(score_race)
    gate = source.index("resolve_mode_with_feature_checks(")
    assert source.index("build_seed_baseline(") > gate
    assert source.index("train_or_build(") > gate
