"""What kind of forecast a score run is, from ``score_runs.model_type`` (schema: xgboost | fallback | derby_override | seed_only_baseline).

A seed-only baseline has no audited calibration, so the app's eligibility gate withholds its Win % / odds / bets: its
probabilities are DIAGNOSTIC and are only ever graded or paper-traded as a separately labelled cohort.  Only an xgboost run is
called trained; fallback / derby_override / unknown are kept apart as OTHER rather than being passed off as trained.
Shared by walk-forward grading, ``training.status`` and paper trading so the three can never disagree.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

DIAGNOSTIC_SEED = "DIAGNOSTIC_SEED_BASELINE"
TRAINED = "TRAINED_MODEL"
OTHER = "OTHER_MODEL"


def forecast_class(model_type: str | None) -> str:
    kind = (model_type or "").strip().lower()
    return DIAGNOSTIC_SEED if kind == "seed_only_baseline" else TRAINED if kind == "xgboost" else OTHER


def forecast_classes_for_runs(conn: sqlite3.Connection, run_ids: Iterable[str]) -> dict[str, str]:
    """{run_id: class}.  A run that is missing, or a ``score_runs`` without ``model_type``, is OTHER."""
    ids = sorted({str(r) for r in run_ids if r is not None})
    if not ids:
        return {}
    has_type = "model_type" in {r[1] for r in conn.execute("PRAGMA table_info(score_runs)")}
    found: dict[str, str | None] = {}
    if has_type:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            found.update({r[0]: r[1] for r in conn.execute(
                f"SELECT run_id, model_type FROM score_runs WHERE run_id IN ({marks})", chunk)})
    return {run_id: forecast_class(found.get(run_id)) for run_id in ids}
