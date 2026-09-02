"""
training/schema_check.py

Validate required columns exist before pipeline steps run.
Call from CLI entry-points so failures surface immediately with
a clear migration message rather than a confusing KeyError later.

Usage
-----
    from training.schema_check import (
        check_starter_observations,
        check_race_cards,
        check_shadow_log_csv,
        SchemaError,
    )
    check_starter_observations(conn)   # raises SchemaError if columns missing
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Required column sets
# ---------------------------------------------------------------------------

# race_cards uses "race_number" — NOT "race_no" (that alias lives only in views
# and SELECT queries).  Never write code that does SELECT race_no FROM race_cards.
_RACE_CARDS_REQUIRED = [
    "card_id", "card_date", "race_number", "surface", "distance_yards",
]

# starter_observations stores race_number as race_no (written by observations.py)
_STARTER_OBS_REQUIRED = [
    "race_id", "race_date", "track", "race_no",
    "horse", "post",
    "pred_win_prob", "win_flag", "finish_pos", "off_odds",
]

# horse_norm is added by the 2026-05-11 migration; listed here so the check
# tells operators to run the migration if it is absent.
_STARTER_OBS_NORM_COLS = ["horse_norm"]

_SHADOW_LOG_REQUIRED = [
    "race_id", "horse", "post",
    "heuristic_win_prob", "served_win_prob", "served_rank",
    "race_no",
]

_SHADOW_LOG_NORM_COLS = ["horse_norm"]

_MIGRATION_HINT = (
    "\n  Run the migration to fix this:\n"
    "    python -m training.migrate_horse_norm\n"
    "  or apply db/migrations/2026_05_11_horse_norm.sql manually."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class SchemaError(Exception):
    """Raised when a required column is missing from a table or CSV."""


def _missing_db_cols(conn: sqlite3.Connection, table: str, required: list[str]) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    return [c for c in required if c not in existing]


def _missing_csv_cols(path: Path, required: list[str]) -> list[str]:
    if not path.exists():
        return required[:]
    df = pd.read_csv(path, nrows=0)
    return [c for c in required if c not in df.columns]


# ---------------------------------------------------------------------------
# Public validators
# ---------------------------------------------------------------------------

def check_race_cards(conn: sqlite3.Connection) -> None:
    """Raise SchemaError if race_cards is missing required columns."""
    missing = _missing_db_cols(conn, "race_cards", _RACE_CARDS_REQUIRED)
    if missing:
        raise SchemaError(
            f"race_cards is missing columns: {missing}\n"
            "  Note: the column is 'race_number', not 'race_no'."
        )


def check_starter_observations(conn: sqlite3.Connection) -> None:
    """Raise SchemaError if starter_observations is missing required columns."""
    missing = _missing_db_cols(conn, "starter_observations", _STARTER_OBS_REQUIRED)
    if missing:
        raise SchemaError(
            f"starter_observations is missing columns: {missing}\n"
            "  Run backfill_observations or results_intake to populate the table."
        )
    # horse_norm is a migration column — warn separately so the hint is clear
    norm_missing = _missing_db_cols(conn, "starter_observations", _STARTER_OBS_NORM_COLS)
    if norm_missing:
        raise SchemaError(
            f"starter_observations is missing join-hardening columns: {norm_missing}"
            + _MIGRATION_HINT
        )


def check_shadow_log_csv(path: Path) -> None:
    """Raise SchemaError if shadow_log.csv is missing required columns."""
    missing = _missing_csv_cols(path, _SHADOW_LOG_REQUIRED)
    if missing:
        raise SchemaError(
            f"shadow_log.csv is missing columns: {missing}\n"
            "  Re-score races with DERBYEDGE_ML_MODE=shadow or live to regenerate."
        )
    norm_missing = _missing_csv_cols(path, _SHADOW_LOG_NORM_COLS)
    if norm_missing:
        raise SchemaError(
            f"shadow_log.csv is missing join-hardening columns: {norm_missing}\n"
            "  Re-score races after applying db/migrations/2026_05_11_horse_norm.sql."
            + _MIGRATION_HINT
        )
