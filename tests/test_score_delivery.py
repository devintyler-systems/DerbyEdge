"""Runtime containment contract for score/fair-odds/wager delivery."""
from __future__ import annotations

import pandas as pd

from src.services import score_delivery


def _board():
    return pd.DataFrame([{"entry_id": 654, "win_probability": .2, "fair_odds": 4.0, "value_score": .03,
                          "bet_tag": "bet", "playable_stake": 12.0}])


def test_ineligible_persisted_score_is_blocked_not_actionable(monkeypatch, tmp_path):
    monkeypatch.setattr(score_delivery, "runtime_score_eligibility", lambda *_: {
        "score_valid": False, "reason_codes": ["calibration_unavailable_or_unaudited", "active_input_unavailable_or_defaulted_without_audited_policy"],
        "artifact": {"model_id": 6, "version": "1"}, "calibration": {"identifier": "temperature_softmax", "status": "not_available", "audit_timestamp": None},
        "race_context": {"build_ts": "b1"}, "decision_timestamp": "d1",
    })
    out, audits = score_delivery.contain_ineligible_board(_board(), tmp_path / "db", 73)
    row = out.iloc[0]
    assert row["output_status"] == "BLOCKED_INELIGIBLE"
    assert row["score_valid"] is False and row["fair_odds_valid"] is False
    assert pd.isna(row["win_probability"]) and pd.isna(row["fair_odds"]) and pd.isna(row["value_score"])
    assert pd.isna(row["bet_tag"]) and pd.isna(row["playable_stake"])
    assert audits[654]["reason_codes"]


def test_eligible_path_preserves_numeric_values(monkeypatch, tmp_path):
    monkeypatch.setattr(score_delivery, "runtime_score_eligibility", lambda *_: {
        "score_valid": True, "reason_codes": [], "artifact": {"model_id": 6, "version": "1"},
        "calibration": {"identifier": "temperature_softmax", "status": "valid", "audit_timestamp": "t"},
        "race_context": {"build_ts": "b1"}, "decision_timestamp": "d1",
    })
    board = _board(); out, _ = score_delivery.contain_ineligible_board(board, tmp_path / "db", 73)
    assert out.loc[0, "output_status"] == "ELIGIBLE"
    assert out.loc[0, "win_probability"] == board.loc[0, "win_probability"]
    assert out.loc[0, "fair_odds"] == board.loc[0, "fair_odds"]


def test_cache_key_changes_with_artifact_calibration_build_or_as_of():
    result = {"race_context": {"card_id": 73, "entry_id": 654, "build_ts": "b1"}, "artifact": {"model_id": 6, "version": "1"}, "calibration": {"identifier": "c", "status": "valid", "audit_timestamp": "t"}, "decision_timestamp": "d1"}
    baseline = score_delivery.eligibility_cache_key(result)
    for path, value in (("build_ts", "b2"),):
        changed = {**result, "race_context": {**result["race_context"], path: value}}
        assert score_delivery.eligibility_cache_key(changed) != baseline
    changed = {**result, "artifact": {"model_id": 7, "version": "2"}}
    assert score_delivery.eligibility_cache_key(changed) != baseline
