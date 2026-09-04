"""One-line forensic trace for the upload -> render -> score ingestion path.

Emits a single JSON record at each stage boundary so it is impossible to have
valid parser output at parse time and a 0/0 audit at render time without an
explicit, traceable mismatch. Never logs licensed PDF content.
"""
from __future__ import annotations

import json
import logging
from typing import Any

_log = logging.getLogger("derbyedge.ingest.trace")

_EVENTS = ("upload", "parse", "persist", "bind", "render", "score")

_FIELDS = (
    "ingestion_run_id",
    "card_id",
    "upload_sha256",
    "parser_pipeline_version",
    "source_format",
    "parser_selected",
    "race_key",
    "active_entry_count",
    "total_pp_records_found",
    "total_pp_records_linked",
    "identity_resolution_rate",
    "audit_sha256",
    "payload_sha256",
)


def trace_ingest(event: str, **fields: Any) -> dict[str, Any]:
    """Emit and return one trace record. ``event`` must be a known stage."""
    if event not in _EVENTS:
        raise ValueError(f"unknown ingest trace event {event!r}")
    record: dict[str, Any] = {"event": event}
    for key in _FIELDS:
        record[key] = fields.get(key)
    for key, value in fields.items():
        if key not in record:
            record[key] = value
    _log.info(json.dumps(record, default=str))
    return record


def audit_trace_fields(audit: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the trace-relevant counters out of a feature audit."""
    safe = audit if isinstance(audit, dict) else {}
    return {
        "source_format": safe.get("source_format"),
        "active_entry_count": safe.get("active_entry_count", safe.get("active_entries")),
        "total_pp_records_found": safe.get("total_pp_records_found", safe.get("total_pp_starts_parsed")),
        "total_pp_records_linked": safe.get("total_pp_records_linked"),
        "identity_resolution_rate": safe.get("identity_resolution_rate"),
    }
