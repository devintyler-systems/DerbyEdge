"""The single permitted upload flow for race PDFs.

    uploaded_file.getvalue()
      -> immutable pdf_bytes
      -> upload_sha256 = sha256(pdf_bytes)
      -> parser_pipeline_version
      -> parse_race_pdf(pdf_bytes)
      -> persist IngestionRun atomically
      -> create or re-sync card
      -> bind card.ingestion_run_id exactly
      -> return only ingestion_run_id (+ trace values)

Rendering and scoring read the immutable run back by ``ingestion_run_id`` via
``src.services.run_mode.get_card_run_state``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from src.ingest.firstbet_pdf import ingest_firstbet_pdf, to_legacy_race_result
from src.ingest.ingestion_run import (
    IngestionRun,
    bind_card_to_ingestion_run,
    build_ingestion_run,
    persist_ingestion_run,
)
from src.services.pdf_ingest import PARSER_PIPELINE_VERSION, parse_race_pdf
from src.services.race_card_builder import (
    find_or_create_race,
    norm_surface,
    parse_distance_yards,
)
from src.utils.ingest_trace import audit_trace_fields, trace_ingest

_RUNS_ROOT_DEFAULT = Path("data/runs")


def _resync_card(
    conn: sqlite3.Connection,
    parse_result: dict[str, Any],
) -> tuple[int | None, list[str]]:
    """Create or re-sync the race card from the parse result. No binding here."""
    track_code = parse_result.get("track_code") or parse_result.get("track_code_resolved")
    race_date = parse_result.get("race_date")
    race_number = parse_result.get("race_number")
    if not (track_code and race_date and race_number is not None):
        return None, ["Race identity incomplete; card not created."]

    runners = parse_result.get("runners") or []
    card_id, _created, _n, warnings = find_or_create_race(
        conn,
        track_code,
        race_date,
        int(race_number),
        runners,
        distance_yards=parse_distance_yards(parse_result.get("distance_text")),
        surface=norm_surface(parse_result.get("surface") or ""),
        stakes_name=parse_result.get("race_type") or None,
        race_class=parse_result.get("race_type") or None,
        purse=parse_result.get("purse_usd") or None,
        conditions=parse_result.get("going") or parse_result.get("conditions") or None,
        field_size=parse_result.get("field_size") or None,
    )
    return card_id, warnings


def ingest_uploaded_race_pdf(
    pdf_bytes: bytes,
    *,
    filename: str,
    conn: sqlite3.Connection,
    runs_root: Path | str = _RUNS_ROOT_DEFAULT,
    parser_pipeline_version: str | None = None,
    create_card: bool = True,
) -> dict[str, Any]:
    """Run the full immutable ingestion flow for one uploaded PDF.

    Returns a dict carrying ``ingestion_run_id`` (the only value a caller
    should keep), ``card_id``, and the forensic-trace counters.
    """
    pipeline_version = parser_pipeline_version or PARSER_PIPELINE_VERSION
    import hashlib

    upload_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    trace_ingest(
        "upload",
        upload_sha256=upload_sha256,
        parser_pipeline_version=pipeline_version,
        filename=filename,
    )

    parse_result = parse_race_pdf(pdf_bytes, filename=filename)

    # Native 1/ST BET migrates through the same contract: run the existing
    # 1/ST ingester so the immutable run carries a real audit + normalized
    # payload, then reuse its run id so exactly one run dir exists.
    reuse_run_id: str | None = None
    if parse_result.get("is_1stbet") and not parse_result.get("is_draftkings"):
        fb = ingest_firstbet_pdf(pdf_bytes, filename=filename, runs_root=runs_root)
        # Keep parse_race_pdf's race identity (its 1/ST track resolver is the
        # one the app relies on); attach the 1/ST ingester's real audit +
        # normalized payload for the immutable run.
        legacy = to_legacy_race_result(fb["payload"], fb["feature_audit"])
        for key in ("track_code", "race_date", "race_number", "distance_text",
                    "surface", "race_type", "purse_usd", "field_size"):
            if not legacy.get(key) and parse_result.get(key):
                legacy[key] = parse_result[key]
        legacy["feature_audit"] = fb["feature_audit"]
        legacy["race"] = fb["payload"]
        legacy["is_1stbet"] = True
        parse_result = legacy
        reuse_run_id = fb["run_id"]

    diagnostics = parse_result.get("parser_diagnostics") or {}
    trace_ingest("parse", **{
        **audit_trace_fields(diagnostics or parse_result.get("feature_audit")),
        "upload_sha256": upload_sha256,
        "parser_pipeline_version": pipeline_version,
        "parser_selected": (parse_result.get("parser") or {}).get("adapter_selected"),
    })

    run: IngestionRun = build_ingestion_run(
        pdf_bytes,
        filename=filename,
        parse_result=parse_result,
        parser_pipeline_version=pipeline_version,
        ingestion_run_id=reuse_run_id,
    )
    paths = persist_ingestion_run(
        run, runs_root=runs_root, allow_existing_dir=reuse_run_id is not None
    )
    # Snapshot the parsed DK race object into the immutable run dir so DK
    # enrichment consumes exactly this run's parse result, by id.
    if parse_result.get("is_draftkings") and parse_result.get("parsed_race") is not None:
        from src.services.dk_enrichment import persist_dk_parsed_race
        persist_dk_parsed_race(run.ingestion_run_id, parse_result["parsed_race"], runs_root=runs_root)
    trace_ingest("persist", **{
        **audit_trace_fields(run.feature_audit),
        "ingestion_run_id": run.ingestion_run_id,
        "upload_sha256": run.upload_sha256,
        "parser_pipeline_version": run.parser_pipeline_version,
        "source_format": run.source_format,
        "parser_selected": run.parser_selected,
        "race_key": run.race_key,
        "audit_sha256": paths["audit_sha256"],
        "payload_sha256": paths["payload_sha256"],
    })

    card_id: int | None = None
    warnings: list[str] = []
    if create_card:
        card_id, warnings = _resync_card(conn, parse_result)
        if card_id is not None:
            bind_card_to_ingestion_run(conn, card_id, run.ingestion_run_id)
            trace_ingest("bind", **{
                **audit_trace_fields(run.feature_audit),
                "ingestion_run_id": run.ingestion_run_id,
                "card_id": card_id,
                "upload_sha256": run.upload_sha256,
                "parser_pipeline_version": run.parser_pipeline_version,
                "source_format": run.source_format,
                "race_key": run.race_key,
                "audit_sha256": paths["audit_sha256"],
                "payload_sha256": paths["payload_sha256"],
            })

    return {
        "ingestion_run_id": run.ingestion_run_id,
        "card_id": card_id,
        "upload_sha256": run.upload_sha256,
        "parser_pipeline_version": run.parser_pipeline_version,
        "source_format": run.source_format,
        "parser_selected": run.parser_selected,
        "parse_status": run.parse_status,
        "race_key": run.race_key,
        "audit_sha256": paths["audit_sha256"],
        "payload_sha256": paths["payload_sha256"],
        "warnings": warnings,
        **audit_trace_fields(run.feature_audit),
    }
