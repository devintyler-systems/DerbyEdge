"""Card-level run-mode lookup and the hard pre-model scoring guard."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.ingest.firstbet_pdf import load_latest_card_audit
from src.ingest.run_state import DataQuality, RunMode, resolve_mode_with_feature_checks
from src.services.feature_state import FeatureVerification, verify_card_features
from src.services.odds_intake import load_live_odds_by_pp


@dataclass(frozen=True)
class CardRunState:
    mode: RunMode
    reasons: list[str]
    audit: dict | None
    quality: DataQuality | None = None
    feature_verification: FeatureVerification | None = None

    @property
    def scoring_eligible(self) -> bool:
        return self.mode in (RunMode.MODEL_READY_LIMITED, RunMode.MODEL_READY)


class ScoringBlockedError(RuntimeError):
    pass


def get_card_run_state(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    runs_root: Path | str = Path("data/runs"),
) -> CardRunState:
    """Recompute effective state from immutable ingest and current DB facts."""
    audit = load_latest_card_audit(card_id, runs_root=runs_root)
    expected_entries = (
        int(audit.get("active_entries", audit.get("entries_parsed")) or 0)
        if audit else _active_entry_count(conn, card_id)
    )
    verification = verify_card_features(
        conn,
        card_id,
        expected_entries=expected_entries,
        require_pp_backed_features=bool(
            audit and audit.get("source_provider") == "1stbet"
        ),
    )
    quality = data_quality_from_card(
        conn,
        card_id,
        audit=audit,
        required_model_features_complete=verification.passed,
    )
    mode, reasons = resolve_mode_with_feature_checks(
        quality, verification.core_rows
    )
    reasons = list(dict.fromkeys(reasons + list(verification.warnings)))
    return CardRunState(mode, reasons, audit, quality, verification)


def ensure_scoring_eligible(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    runs_root: Path | str = Path("data/runs"),
) -> CardRunState:
    """Raise before any model call unless the card is forecast-eligible."""
    state = get_card_run_state(conn, card_id, runs_root=runs_root)
    if not state.scoring_eligible:
        reason = "; ".join(state.reasons) or "Data-quality gate rejected the card."
        raise ScoringBlockedError(f"SCORING BLOCKED [{state.mode.value}]: {reason}")
    return state


def data_quality_from_card(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    audit: dict[str, Any] | None = None,
    required_model_features_complete: bool = False,
) -> DataQuality:
    race = conn.execute(
        """SELECT rc.field_size, rc.card_date, rc.race_number,
                  rc.distance_yards, rc.surface, t.abbrev
           FROM race_cards rc
           JOIN tracks t ON t.track_id = rc.track_id
           WHERE rc.card_id=?""",
        (card_id,),
    ).fetchone()
    entries = conn.execute(
        "SELECT entry_id, morning_line_odds FROM entries "
        "WHERE card_id=? AND scratch_flag=0",
        (card_id,),
    ).fetchall()
    entry_ids = {int(row[0]) for row in entries}
    live_entry_ids: set[int] = set()
    if _table_exists(conn, "live_odds"):
        for quote in load_live_odds_by_pp(conn, card_id).values():
            try:
                entry_id = int(quote["entry_id"])
                decimal_odds = float(quote["decimal_odds"])
            except (KeyError, TypeError, ValueError):
                continue
            if entry_id in entry_ids and decimal_odds > 1.0:
                live_entry_ids.add(entry_id)

    if audit:
        parsed = int(audit.get("active_entries", audit.get("entries_parsed")) or 0)
        entries_with_pp = int(audit.get("entries_with_pp_history") or 0)
        match_rate = float(audit.get("starter_match_rate") or 0.0)
        declared = audit.get("field_size_declared")
        scratches = int(audit.get("scratches") or 0)
        metadata_complete = bool(audit.get("race_metadata_complete", race is not None))
        has_morning_lines = bool(audit.get("has_morning_lines", entries))
        blocking_errors = list(audit.get("blocking_errors") or [])
    else:
        pp_entry_ids: set[int] = set()
        for table in ("firstbet_pp_starts", "horse_starts"):
            if not _table_exists(conn, table):
                continue
            rows = conn.execute(
                f"SELECT DISTINCT entry_id FROM {table} WHERE card_id=?", (card_id,)
            ).fetchall()
            pp_entry_ids.update(int(row[0]) for row in rows if int(row[0]) in entry_ids)
        parsed = len(entries)
        entries_with_pp = len(pp_entry_ids)
        match_rate = entries_with_pp / parsed if parsed else 0.0
        declared = int(race[0]) if race and race[0] else None
        metadata_complete = bool(
            race and race[1] and race[2] is not None and race[3] and race[4] and race[5]
        )
        has_morning_lines = bool(entries) and all(row[1] is not None for row in entries)
        blocking_errors = [] if race else [f"No race card found for card_id={card_id}."]
        scratches = 0
    return DataQuality(
        entries_parsed=parsed,
        field_size_declared=int(declared) if declared else None,
        entries_with_pp_history=entries_with_pp,
        starter_match_rate=match_rate,
        race_metadata_complete=metadata_complete,
        has_morning_lines=has_morning_lines,
        has_live_odds=(
            bool(parsed)
            and len(entries) == parsed
            and live_entry_ids == entry_ids
        ),
        required_model_features_complete=required_model_features_complete,
        blocking_errors=blocking_errors,
        entries_scratched=scratches,
    )


def quality_with_verified_features(
    quality: DataQuality,
    verification: FeatureVerification,
) -> DataQuality:
    """Apply verification from the exact post-construction feature frame."""
    return replace(
        quality, required_model_features_complete=verification.passed
    )


def _active_entry_count(conn: sqlite3.Connection, card_id: int) -> int:
    if not _table_exists(conn, "entries"):
        return 0
    return int(conn.execute(
        "SELECT COUNT(*) FROM entries WHERE card_id=? AND scratch_flag=0", (card_id,)
    ).fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None
