"""Runtime score-generation preflight tests; no browser or score writes."""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from src.models import scorer
from src.services import runtime_score_preflight as preflight
from src.utils.db import DB_PATH


def _context(entry_id: int) -> dict:
    return {
        "card_id": 73,
        "entry_id": entry_id,
        "card_date": "2026-09-04",
        "race_number": 6,
        "build_ts": "2026-09-04T10:00:00Z",
    }


def _eligibility(entry_id: int, *, valid: bool) -> dict:
    reasons = [] if valid else [
        "calibration_unavailable_or_unaudited",
        "active_input_unavailable_or_defaulted_without_audited_policy",
    ]
    return {
        "score_valid": valid,
        "reason_codes": reasons,
        "artifact": {"model_id": 6, "version": "1.0.0"},
        "calibration": {"identifier": "temperature_softmax", "status": "valid", "audit_timestamp": "2026-09-04T09:00:00Z"},
        "decision_timestamp": "2026-09-04T10:00:00Z",
        "race_context": _context(entry_id),
    }


def _patch_field(monkeypatch, validity: dict[int, bool]) -> None:
    monkeypatch.setattr(preflight, "active_runtime_entry_ids", lambda *_: list(validity))
    monkeypatch.setattr("src.services.score_eligibility.resolve_race_context", lambda _db, _card, entry: _context(entry))
    monkeypatch.setattr(
        "src.services.score_eligibility.resolve_scoring_context",
        lambda _db, _card, entry: ({"entry_id": entry}, {"run_timestamp": "2026-09-04T10:00:00Z"}, {"model_id": 6, "artifact_path": "fixture.pkl"}),
    )
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    monkeypatch.setattr("src.models.trainer.load_model_artifact", lambda _: {"fixture": True})
    monkeypatch.setattr(
        "src.services.score_eligibility.evaluate_score_eligibility",
        lambda **kwargs: _eligibility(kwargs["race_context"]["entry_id"], valid=validity[kwargs["race_context"]["entry_id"]]),
    )


def test_runtime_preflight_blocks_entire_field_when_one_active_entry_fails(monkeypatch, tmp_path):
    _patch_field(monkeypatch, {101: True, 102: False})

    result = preflight.preflight_runtime_race_score(card_id=73, db_path=tmp_path / "unused.db")

    assert not result.race_score_valid
    assert result.output_status == "BLOCKED_INELIGIBLE"
    assert result.score_generated is False and result.score_persisted is False and result.artifact_written is False
    assert result.entry_level_reason_codes[101] == ()
    assert "calibration_unavailable_or_unaudited" in result.entry_level_reason_codes[102]
    assert "calibration_unavailable_or_unaudited" in result.race_level_reason_codes


def test_runtime_preflight_valid_field_retains_passing_context(monkeypatch, tmp_path):
    _patch_field(monkeypatch, {101: True, 102: True})

    result = preflight.preflight_runtime_race_score(card_id=73, db_path=tmp_path / "unused.db")

    assert result.race_score_valid
    assert result.output_status == "PREFLIGHT_PASSED"
    assert result.active_runner_count == 2
    assert result.artifact_id == 6 and result.artifact_version == "1.0.0"
    assert result.feature_build_timestamp == "2026-09-04T10:00:00Z"


def test_runtime_preflight_is_opt_in(monkeypatch):
    monkeypatch.delenv("DERBYEDGE_STRICT_PREFLIGHT", raising=False)
    assert scorer._strict_preflight_enabled() is False
    monkeypatch.setenv("DERBYEDGE_STRICT_PREFLIGHT", "on")
    assert scorer._strict_preflight_enabled() is True


def test_score_race_blocks_before_connection_inference_or_persistence_when_opted_in(monkeypatch):
    blocked = preflight.RaceScorePreflightResult(
        card_id=73, race_identifier="2026-09-04#6", execution_mode="RUNTIME_PRE_RACE",
        race_score_valid=False, race_level_reason_codes=("calibration_unavailable_or_unaudited",),
    )
    monkeypatch.setenv("DERBYEDGE_STRICT_PREFLIGHT", "on")
    monkeypatch.setattr(scorer, "preflight_runtime_race_score", lambda **_: blocked)
    monkeypatch.setattr(scorer, "get_connection", lambda: (_ for _ in ()).throw(AssertionError("mutable scorer connection opened")))
    monkeypatch.setattr(scorer, "save_artifact", lambda *_: (_ for _ in ()).throw(AssertionError("artifact write attempted")))

    result = scorer.score_race(card_id=73)

    assert result is blocked
    assert result.output_status == "BLOCKED_INELIGIBLE"
    assert result.score_generated is False and result.score_persisted is False and result.artifact_written is False


def test_card_73_runtime_entrypoint_preserves_score_rows_and_artifacts_when_blocked(monkeypatch):
    """Integration coverage for the maintained Saratoga runtime fixture.

    This intentionally uses the real public scorer rather than the delivery
    containment adapter.  Environments without the maintained acceptance card
    skip rather than manufacturing a model, feature vector, or score context.
    """
    if not DB_PATH.is_file():
        pytest.skip("maintained card-73 database fixture is unavailable")
    monkeypatch.setenv("DERBYEDGE_STRICT_PREFLIGHT", "on")
    conn = sqlite3.connect(DB_PATH)
    try:
        exists = conn.execute("SELECT 1 FROM race_cards WHERE card_id=73").fetchone()
        before_runs = conn.execute("SELECT COUNT(*) FROM score_runs WHERE card_id=73").fetchone()[0]
    finally:
        conn.close()
    if not exists:
        pytest.skip("maintained card-73 database fixture is unavailable")
    before_bytes = DB_PATH.read_bytes()
    models_dir = DB_PATH.parents[1] / "saved_models"
    before_artifacts = sorted((path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in models_dir.glob("*") if path.is_file())

    result = scorer.score_race(card_id=73)

    assert isinstance(result, preflight.RaceScorePreflightResult)
    assert result.output_status == "BLOCKED_INELIGIBLE"
    assert result.score_generated is False and result.score_persisted is False and result.artifact_written is False
    conn = sqlite3.connect(DB_PATH)
    try:
        after_runs = conn.execute("SELECT COUNT(*) FROM score_runs WHERE card_id=73").fetchone()[0]
    finally:
        conn.close()
    after_artifacts = sorted((path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in models_dir.glob("*") if path.is_file())
    assert after_runs == before_runs
    assert after_artifacts == before_artifacts
    assert DB_PATH.read_bytes() == before_bytes


def test_unknown_execution_mode_fails_closed_without_runtime_preflight(monkeypatch):
    monkeypatch.setattr(scorer, "preflight_runtime_race_score", lambda **_: (_ for _ in ()).throw(AssertionError("unexpected preflight")))
    monkeypatch.setattr(scorer, "get_connection", lambda: (_ for _ in ()).throw(AssertionError("mutable scorer connection opened")))

    result = scorer.score_race(card_id=73, execution_mode="not-a-declared-mode")

    assert result.output_status == "BLOCKED_INELIGIBLE"
    assert result.race_level_reason_codes == ("runtime_execution_mode_unresolved",)


def test_nonruntime_preflight_is_explicitly_non_actionable_and_not_runtime_evaluated(tmp_path):
    result = preflight.preflight_runtime_race_score(
        card_id=73, db_path=tmp_path / "unused.db", execution_mode=preflight.ExecutionMode.BACKTEST,
    )

    assert result.execution_mode == "BACKTEST"
    assert result.output_status == "BLOCKED_INELIGIBLE"
    assert result.race_level_reason_codes == ("runtime_preflight_not_applicable_to_execution_mode",)
