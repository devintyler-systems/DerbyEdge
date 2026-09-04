"""DraftKings pre-race enrichment bound to the immutable ingestion run.

Chain:

    immutable DK ingestion run
      -> validated normalized DK PP payload (parsed-race snapshot in the run dir)
      -> DK enrichment  (canonical horse_starts / entries / workouts)
      -> feature build  (feature_store rows, stamped with the ingestion run id)
      -> data-quality validation
      -> MODEL_READY_LIMITED or correctly BLOCKED

Design constraints honoured here:

* Enrichment consumes only the parsed-race snapshot from the exact
  ``ingestion_run_id`` bound to the card. It never re-reads a different card,
  race key, latest artifact, transient object, or external post-race source.
* Output is provenance-bound: every canonical / feature row written for the card
  carries the ``ingestion_run_id``; a ``dk_card_enrichment`` row records the
  upload sha, parser pipeline version, and enrichment version.
* Idempotent: re-running enrichment, re-rendering, or double-clicking score
  cannot duplicate ``horse_starts`` rows (the canonical writer dedups by
  ``source_row_id``; feature rows are rebuilt in place).
* Reprocessing under a new parser/enrichment version produces a new immutable
  run and its own enrichment record; historical runs are never overwritten.
"""
from __future__ import annotations

import pickle
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ingest.ingestion_run import (
    IngestionRunBindingInvalid,
    card_ingestion_run_id,
    load_ingestion_run,
    validate_ingestion_run,
)

DK_ENRICHMENT_VERSION = "dk_enrich_v1"

NOT_STARTED = "NOT_STARTED"
ENRICHING = "ENRICHING"
ENRICHED = "ENRICHED"
FAILED = "FAILED"

DK_ENRICHMENT_FAILED_GUIDANCE = (
    "DK PP data was parsed and persisted, but pre-race feature enrichment failed."
)

_RUNS_ROOT_DEFAULT = Path("data/runs")


@dataclass(frozen=True)
class DKEnrichmentResult:
    state: str
    card_id: int
    ingestion_run_id: str | None
    upload_sha256: str | None
    parser_pipeline_version: str | None
    enrichment_version: str
    entries_written: int = 0
    horse_starts_written: int = 0
    workouts_written: int = 0
    linked_history: int = 0
    resolved_no_history: int = 0
    failure_reason: str | None = None


# ── parsed-race snapshot (lives in the immutable run dir) ─────────────────────

def _snapshot_path(run_id: str, runs_root: Path | str) -> Path:
    return Path(runs_root) / run_id / "dk_parsed_race.pkl"


def persist_dk_parsed_race(run_id: str, parsed: Any, *, runs_root: Path | str = _RUNS_ROOT_DEFAULT) -> str:
    """Snapshot the parsed DraftKings race object into its immutable run dir."""
    path = _snapshot_path(run_id, runs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():  # immutable: never overwrite an existing snapshot
        path.write_bytes(pickle.dumps(parsed))
    return str(path)


def load_dk_parsed_race(run_id: str, *, runs_root: Path | str = _RUNS_ROOT_DEFAULT) -> Any:
    path = _snapshot_path(run_id, runs_root)
    if not path.exists():
        raise IngestionRunBindingInvalid(
            f"no DK parsed-race snapshot for ingestion run {run_id!r}"
        )
    return pickle.loads(path.read_bytes())


# ── enrichment state table ───────────────────────────────────────────────────

def ensure_dk_enrichment_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dk_card_enrichment (
               card_id                INTEGER NOT NULL,
               ingestion_run_id       TEXT    NOT NULL,
               upload_sha256          TEXT,
               parser_pipeline_version TEXT,
               enrichment_version     TEXT,
               state                  TEXT    NOT NULL,
               failure_reason         TEXT,
               entries_written        INTEGER DEFAULT 0,
               horse_starts_written   INTEGER DEFAULT 0,
               workouts_written       INTEGER DEFAULT 0,
               linked_history         INTEGER DEFAULT 0,
               resolved_no_history    INTEGER DEFAULT 0,
               updated_at             TEXT,
               PRIMARY KEY (card_id, ingestion_run_id)
           )"""
    )
    conn.commit()


_FEATURE_AVAILABILITY_COLUMNS = {
    "ingestion_run_id": "TEXT",
    "has_completed_start_history": "INTEGER",
    "no_history_reason": "TEXT",
    "runner_data_status": "TEXT",
    "speed_figure_available": "INTEGER",
    "pace_figure_available": "INTEGER",
    "form_cycle_available": "INTEGER",
    "trip_feature_available": "INTEGER",
    "workout_forward_low_history": "INTEGER",
}


def _ensure_provenance_columns(conn: sqlite3.Connection) -> None:
    for table in ("horse_starts", "workouts"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "ingestion_run_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN ingestion_run_id TEXT")
    fs_cols = {r[1] for r in conn.execute("PRAGMA table_info(feature_store)")}
    if fs_cols:
        for name, kind in _FEATURE_AVAILABILITY_COLUMNS.items():
            if name not in fs_cols:
                conn.execute(f"ALTER TABLE feature_store ADD COLUMN {name} {kind}")
    conn.commit()


# PP-derived feature columns that must stay NULL (never 0) for a runner with no
# usable completed-start history from the DK source.
_PP_DERIVED_NULLABLE = (
    "recent_finish_percentile_w",
    "surface_distance_finish_percentile_w",
    "distance_fit_eb",
    "surface_fit_eb",
    "distance_fit",
    "surface_fit",
    "class_delta_last_to_today",
    "speed_best_3",
    "speed_last",
    "beyer_last",
    "form_cycle_idx",
    "pace_fit_score",
)


def _stamp_feature_availability(
    conn: sqlite3.Connection, card_id: int, run_id: str, audit: dict[str, Any]
) -> None:
    """Record per-runner feature availability; preserve missingness (never 0)."""
    by_key = {
        str(r.get("horse_name_key") or "").lower(): r
        for r in (audit.get("runners") or [])
    }
    from src.ingest.draftkings_pdf import canonical_horse_name

    rows = conn.execute(
        "SELECT fs.rowid, fs.horse_name FROM feature_store fs WHERE fs.card_id=?",
        (int(card_id),),
    ).fetchall()
    for rowid, horse_name in rows:
        diag = by_key.get(canonical_horse_name(horse_name or ""), {})
        status = diag.get("runner_data_status") or "unresolved_history"
        linked = int(diag.get("past_performances_linked") or 0)
        has_history = status == "linked_history" and linked > 0
        no_hist_reason = diag.get("no_history_reason")
        workout_forward = status == "resolved_no_history"
        conn.execute(
            "UPDATE feature_store SET ingestion_run_id=?, runner_data_status=?, "
            "has_completed_start_history=?, no_history_reason=?, "
            "speed_figure_available=0, pace_figure_available=0, "
            "form_cycle_available=?, trip_feature_available=0, "
            "workout_forward_low_history=? WHERE rowid=?",
            (
                run_id, status,
                1 if has_history else 0,
                no_hist_reason,
                1 if has_history else 0,
                1 if workout_forward else 0,
                rowid,
            ),
        )
        if not has_history:
            sets = ", ".join(f"{c}=NULL" for c in _PP_DERIVED_NULLABLE)
            conn.execute(f"UPDATE feature_store SET {sets} WHERE rowid=?", (rowid,))
    conn.commit()


def card_is_dk_bound(
    conn: sqlite3.Connection, card_id: int, *, runs_root: Path | str = _RUNS_ROOT_DEFAULT
) -> bool:
    """True when the card is bound to an ingestion run with a DraftKings source."""
    run_id = card_ingestion_run_id(conn, card_id)
    if not run_id:
        return False
    try:
        run = load_ingestion_run(run_id, runs_root=runs_root)
    except IngestionRunBindingInvalid:
        return False
    return str(run.source_format or "").startswith("dkhorse")


def get_dk_enrichment_state(
    conn: sqlite3.Connection, card_id: int, ingestion_run_id: str | None = None
) -> dict[str, Any]:
    ensure_dk_enrichment_table(conn)
    if ingestion_run_id is None:
        ingestion_run_id = card_ingestion_run_id(conn, card_id)
    if not ingestion_run_id:
        return {"state": NOT_STARTED, "card_id": card_id, "ingestion_run_id": None}
    row = conn.execute(
        "SELECT * FROM dk_card_enrichment WHERE card_id=? AND ingestion_run_id=?",
        (int(card_id), ingestion_run_id),
    ).fetchone()
    if not row:
        return {"state": NOT_STARTED, "card_id": card_id, "ingestion_run_id": ingestion_run_id}
    return dict(row)


def _write_state(conn: sqlite3.Connection, **fields: Any) -> None:
    ensure_dk_enrichment_table(conn)
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    keys = list(fields)
    conn.execute(
        f"INSERT INTO dk_card_enrichment ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)}) "
        f"ON CONFLICT(card_id, ingestion_run_id) DO UPDATE SET "
        + ",".join(f"{k}=excluded.{k}" for k in keys if k not in ("card_id", "ingestion_run_id")),
        [fields[k] for k in keys],
    )
    conn.commit()


# ── the enrichment entry point ───────────────────────────────────────────────

def enrich_card_from_ingestion_run(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    runs_root: Path | str = _RUNS_ROOT_DEFAULT,
    build: bool = True,
) -> DKEnrichmentResult:
    """Enrich one card strictly from the DK ingestion run it is bound to."""
    from src.services.draftkings_enrich import ingest_draftkings_to_canonical

    ensure_dk_enrichment_table(conn)
    _ensure_provenance_columns(conn)

    run_id = card_ingestion_run_id(conn, card_id)
    if not run_id:
        raise IngestionRunBindingInvalid(f"card {card_id} is not bound to an ingestion run")
    run = validate_ingestion_run(load_ingestion_run(run_id, runs_root=runs_root))
    if not str(run.source_format or "").startswith("dkhorse"):
        raise ValueError(
            f"card {card_id} ingestion run is {run.source_format!r}, not a DraftKings source"
        )

    base = dict(
        card_id=int(card_id),
        ingestion_run_id=run_id,
        upload_sha256=run.upload_sha256,
        parser_pipeline_version=run.parser_pipeline_version,
        enrichment_version=DK_ENRICHMENT_VERSION,
    )
    _write_state(conn, **base, state=ENRICHING, failure_reason=None)

    try:
        parsed = load_dk_parsed_race(run_id, runs_root=runs_root)
        cid2, _is_new = ingest_draftkings_to_canonical(conn, parsed)
        if int(cid2) != int(card_id):
            raise RuntimeError(
                f"ingestion run resolves to card {cid2}, not the bound card {card_id}"
            )

        _stamp_provenance(conn, card_id, run_id)

        counts = _canonical_counts(conn, card_id)
        audit = run.feature_audit
        rdsc = audit.get("runner_data_status_counts") or {}
        linked = int(rdsc.get("linked_history") or 0)
        resolved = int(rdsc.get("resolved_no_history") or 0)

        if build:
            from src.features.builder import build_features
            build_features(card_id, conn=conn)
            conn.execute(
                "UPDATE feature_store SET ingestion_run_id=? WHERE card_id=?",
                (run_id, int(card_id)),
            )
            _stamp_feature_availability(conn, card_id, run_id, audit)
            conn.commit()

        _write_state(
            conn, **base, state=ENRICHED, failure_reason=None,
            entries_written=counts["entries"],
            horse_starts_written=counts["horse_starts"],
            workouts_written=counts["workouts"],
            linked_history=linked, resolved_no_history=resolved,
        )
        return DKEnrichmentResult(
            state=ENRICHED, card_id=int(card_id), ingestion_run_id=run_id,
            upload_sha256=run.upload_sha256,
            parser_pipeline_version=run.parser_pipeline_version,
            enrichment_version=DK_ENRICHMENT_VERSION,
            entries_written=counts["entries"],
            horse_starts_written=counts["horse_starts"],
            workouts_written=counts["workouts"],
            linked_history=linked, resolved_no_history=resolved,
        )
    except Exception as exc:  # fail closed; never fall back to stale data
        _write_state(conn, **base, state=FAILED, failure_reason=str(exc)[:500])
        return DKEnrichmentResult(
            state=FAILED, card_id=int(card_id), ingestion_run_id=run_id,
            upload_sha256=run.upload_sha256,
            parser_pipeline_version=run.parser_pipeline_version,
            enrichment_version=DK_ENRICHMENT_VERSION,
            failure_reason=str(exc)[:500],
        )


def _stamp_provenance(conn: sqlite3.Connection, card_id: int, run_id: str) -> None:
    conn.execute(
        "UPDATE horse_starts SET ingestion_run_id=? "
        "WHERE card_id=? AND source_provider='draftkings' AND ingestion_run_id IS NULL",
        (run_id, int(card_id)),
    )
    conn.execute(
        "UPDATE workouts SET ingestion_run_id=? WHERE ingestion_run_id IS NULL AND horse_id IN "
        "(SELECT horse_id FROM entries WHERE card_id=?) AND source_provider='draftkings'",
        (run_id, int(card_id)),
    )
    conn.commit()


def _canonical_counts(conn: sqlite3.Connection, card_id: int) -> dict[str, int]:
    hs = conn.execute(
        "SELECT COUNT(*) FROM horse_starts WHERE card_id=? AND source_provider='draftkings'",
        (int(card_id),),
    ).fetchone()[0]
    wo = conn.execute(
        "SELECT COUNT(*) FROM workouts WHERE source_provider='draftkings' AND horse_id IN "
        "(SELECT horse_id FROM entries WHERE card_id=?)", (int(card_id),),
    ).fetchone()[0]
    en = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE card_id=?", (int(card_id),)
    ).fetchone()[0]
    return {"horse_starts": int(hs), "workouts": int(wo), "entries": int(en)}
