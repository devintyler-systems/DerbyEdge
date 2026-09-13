"""Runtime fail-closed containment for persisted score-board delivery."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.models.trainer import load_model_artifact
from src.services.score_eligibility import (
    evaluate_score_eligibility, resolve_race_context, resolve_scoring_context,
)


ACTIONABLE_COLUMNS = (
    "win_probability", "place_probability", "show_probability", "model_win_prob_pct",
    "fair_odds", "value_score", "model_edge", "edge_vs_live_market", "bet_tag",
    "kelly_frac", "raw_stake", "stake_dollar", "playable_stake", "stake_reason",
)


def eligibility_cache_key(result: dict[str, Any]) -> tuple[Any, ...]:
    """Context identity only; callers must not reuse a result across these changes."""
    artifact, calibration, race = result["artifact"], result["calibration"], result["race_context"]
    return (race["card_id"], race["entry_id"], race.get("build_ts"), artifact.get("model_id"),
            artifact.get("version"), calibration.get("identifier"), calibration.get("status"),
            calibration.get("audit_timestamp"), result.get("decision_timestamp"))


def runtime_score_eligibility(db_path: Path, card_id: int, entry_id: int) -> dict[str, Any]:
    """Evaluate the exact persisted context used for serving a legacy score."""
    race = resolve_race_context(db_path, card_id, entry_id)
    feature, decision, model = resolve_scoring_context(db_path, card_id, entry_id)
    artifact = load_model_artifact(Path(str(model["artifact_path"])))
    return evaluate_score_eligibility(race_context=race, feature_row=feature, decision=decision, model=model, artifact=artifact)


def contain_ineligible_board(board: pd.DataFrame, db_path: Path, card_id: int) -> tuple[pd.DataFrame, dict[int, dict[str, Any]]]:
    """Return a non-actionable board for any entry whose persisted context fails."""
    out = board.copy()
    audits: dict[int, dict[str, Any]] = {}
    for index, row in out.iterrows():
        entry_id = int(row["entry_id"])
        try:
            result = runtime_score_eligibility(db_path, card_id, entry_id)
        except Exception as exc:
            result = {
                "score_valid": False, "reason_codes": ["score_eligibility_context_unresolved", str(exc)],
                "artifact": {}, "calibration": {}, "race_context": {}, "decision_timestamp": None,
            }
        audits[entry_id] = result
        out.loc[index, "score_valid"] = bool(result["score_valid"])
        out.loc[index, "fair_odds_valid"] = bool(result["score_valid"])
        out.loc[index, "market_comparison_valid"] = bool(result["score_valid"])
        out.loc[index, "wager_valid"] = bool(result["score_valid"])
        out.loc[index, "score_eligibility_reason_codes"] = ";".join(result["reason_codes"])
        out.loc[index, "artifact_id"] = result["artifact"].get("model_id")
        out.loc[index, "artifact_version"] = result["artifact"].get("version")
        out.loc[index, "calibration_identifier"] = result["calibration"].get("identifier")
        out.loc[index, "calibration_status"] = result["calibration"].get("status")
        out.loc[index, "calibration_audit_timestamp"] = result["calibration"].get("audit_timestamp")
        out.loc[index, "decision_as_of_timestamp"] = result.get("decision_timestamp")
        out.loc[index, "feature_build_timestamp"] = result["race_context"].get("build_ts")
        out.loc[index, "output_status"] = "ELIGIBLE" if result["score_valid"] else "BLOCKED_INELIGIBLE"
        if not result["score_valid"]:
            for column in ACTIONABLE_COLUMNS:
                if column in out.columns:
                    out.loc[index, column] = None
    return out, audits
