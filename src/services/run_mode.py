"""Card-level run-mode lookup and the hard pre-model scoring guard."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.ingest.firstbet_pdf import load_latest_card_audit
from src.ingest.run_state import DataQuality, RunMode, resolve_run_mode


@dataclass(frozen=True)
class CardRunState:
    mode: RunMode
    reasons: list[str]
    audit: dict | None
    quality: DataQuality | None = None

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
    """Resolve source audit first, falling back to deterministic DB quality."""
    audit = load_latest_card_audit(card_id, runs_root=runs_root)
    if audit:
        mode = RunMode(audit["run_mode"])
        reasons = (
            list(audit.get("blocking_errors") or [])
            if mode == RunMode.BLOCKED
            else list(audit.get("warnings") or [])
        )
        return CardRunState(mode, reasons, audit)

    quality = data_quality_from_card(conn, card_id)
    mode, reasons = resolve_run_mode(quality)
    return CardRunState(mode, reasons, None, quality)


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


def data_quality_from_card(conn: sqlite3.Connection, card_id: int) -> DataQuality:
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
    pp_entry_ids: set[int] = set()
    for table in ("firstbet_pp_starts", "horse_starts"):
        if not _table_exists(conn, table):
            continue
        rows = conn.execute(
            f"SELECT DISTINCT entry_id FROM {table} WHERE card_id=?", (card_id,)
        ).fetchall()
        pp_entry_ids.update(int(row[0]) for row in rows if int(row[0]) in entry_ids)

    live_count = 0
    if _table_exists(conn, "live_odds"):
        columns = _table_columns(conn, "live_odds")
        predicate = " AND is_morning_line=0" if "is_morning_line" in columns else ""
        live_count = conn.execute(
            "SELECT COUNT(DISTINCT entry_id) FROM live_odds WHERE card_id=?" + predicate,
            (card_id,),
        ).fetchone()[0]

    parsed = len(entries)
    metadata_complete = bool(
        race
        and race[1]
        and race[2] is not None
        and race[3]
        and race[4]
        and race[5]
    )
    return DataQuality(
        entries_parsed=parsed,
        field_size_declared=int(race[0]) if race and race[0] else None,
        entries_with_pp_history=len(pp_entry_ids),
        starter_match_rate=len(pp_entry_ids) / parsed if parsed else 0.0,
        race_metadata_complete=metadata_complete,
        has_morning_lines=bool(entries) and all(row[1] is not None for row in entries),
        has_live_odds=bool(parsed) and live_count == parsed,
        # Step 4 will promote this only after the full feature contract exists.
        required_model_features_complete=False,
        blocking_errors=[] if race else [f"No race card found for card_id={card_id}."],
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

