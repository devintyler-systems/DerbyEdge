"""Immutable ingestion-run contract.

An :class:`IngestionRun` is the single authoritative record of one uploaded
file's parse result. The exact result produced at parse time is what a card
binds to, what the renderer reads, and what scoring consumes — looked up by
``ingestion_run_id`` only, never by race key, filename, date, track, or
"latest card".

The DraftKings parser and the data-quality gate are frozen producer behaviour.
This module only *persists*, *binds*, and *reads back* their output; it never
re-parses or adjusts parser results.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

ParseStatus = Literal["parsed", "blocked", "failed"]

_RUNS_ROOT_DEFAULT = Path("data/runs")


@dataclass(frozen=True)
class IngestionRun:
    ingestion_run_id: str
    upload_sha256: str
    parser_pipeline_version: str
    source_format: str | None
    parser_selected: str | None
    parse_status: ParseStatus
    race_key: str | None
    created_at_utc: str
    feature_audit: dict
    normalized_race_payload: dict
    error: dict | None = None


class IngestionRunBindingInvalid(RuntimeError):
    """Raised when a card cannot be matched to a valid immutable ingestion run."""

    reason = "ingestion_run_binding_invalid"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.reason}: {detail}")
        self.detail = detail


# ── Derivation ────────────────────────────────────────────────────────────────

def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_sha256(payload: dict) -> str:
    return _sha256_str(_canonical_json(payload))


def audit_sha256(audit: dict) -> str:
    return _sha256_str(_canonical_json(audit))


def _dget(container: Any, key: str, default: Any = None) -> Any:
    if container is None:
        return default
    if isinstance(container, dict):
        return container.get(key, default)
    return getattr(container, key, default)


def build_ingestion_run(
    pdf_bytes: bytes,
    *,
    filename: str,
    parse_result: dict[str, Any],
    parser_pipeline_version: str | None = None,
    ingestion_run_id: str | None = None,
) -> IngestionRun:
    """Derive an :class:`IngestionRun` from an already-computed parse result.

    Pure. No parser calls, no persistence.
    """
    from src.services.pdf_ingest import PARSER_PIPELINE_VERSION, build_dk_upload_audit

    pipeline_version = parser_pipeline_version or PARSER_PIPELINE_VERSION
    upload_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    diagnostics = parse_result.get("parser_diagnostics") or {}
    parser_block = parse_result.get("parser") or {}

    source_format = (
        _dget(diagnostics, "source_format")
        or _dget(parser_block, "source_format")
    )
    parser_selected = _dget(parser_block, "adapter_selected") or (
        "draftkings_pdf" if parse_result.get("is_draftkings")
        else "firstbet_pdf" if parse_result.get("is_1stbet")
        else None
    )

    ok = bool(parse_result.get("ok"))
    run_mode = str(_dget(diagnostics, "run_mode") or parse_result.get("run_mode") or "")
    if not ok:
        parse_status: ParseStatus = "failed"
    elif run_mode == "BLOCKED":
        parse_status = "blocked"
    else:
        parse_status = "parsed"

    track_code = parse_result.get("track_code")
    race_date = parse_result.get("race_date")
    race_number = parse_result.get("race_number")
    race_key = (
        f"{track_code}|{race_date}|R{race_number}"
        if track_code and race_date and race_number is not None
        else None
    )

    if parse_result.get("is_draftkings"):
        feature_audit = build_dk_upload_audit(
            parse_result, pdf_bytes=pdf_bytes, filename=filename
        )
    else:
        feature_audit = dict(parse_result.get("feature_audit") or {})
        feature_audit.setdefault("source_format", source_format)
        feature_audit.setdefault(
            "block_reasons", list(diagnostics.get("block_reasons") or [])
        )

    normalized_race_payload = dict(parse_result.get("race") or {})

    error: dict | None = None
    if not ok:
        error = {
            "message": parse_result.get("error") or "PDF parse failed.",
            "source_format": source_format,
        }

    run_id = ingestion_run_id or (
        f"ing-{upload_sha256[:12]}-{uuid.uuid4().hex[:10]}"
    )

    return IngestionRun(
        ingestion_run_id=run_id,
        upload_sha256=upload_sha256,
        parser_pipeline_version=pipeline_version,
        source_format=source_format,
        parser_selected=parser_selected,
        parse_status=parse_status,
        race_key=race_key,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        feature_audit=feature_audit,
        normalized_race_payload=normalized_race_payload,
        error=error,
    )


# ── Persistence (immutable, by id) ────────────────────────────────────────────

def _run_dir(run_id: str, runs_root: Path | str) -> Path:
    return Path(runs_root) / run_id


def persist_ingestion_run(
    run: IngestionRun,
    *,
    runs_root: Path | str = _RUNS_ROOT_DEFAULT,
    allow_existing_dir: bool = False,
) -> dict[str, str]:
    """Write the ingestion run atomically.

    Fails if the run dir already exists, unless ``allow_existing_dir`` is set
    (used when an upstream ingester — e.g. 1/ST — already created the run dir
    and its own artefacts; the immutable ``ingestion_run.json`` is still never
    overwritten).
    """
    run_dir = _run_dir(run.ingestion_run_id, runs_root)
    run_dir.mkdir(parents=True, exist_ok=allow_existing_dir)
    run_path = run_dir / "ingestion_run.json"
    if run_path.exists():
        raise FileExistsError(f"ingestion_run.json already exists for {run.ingestion_run_id}")

    record = asdict(run)
    record["audit_sha256"] = audit_sha256(run.feature_audit)
    record["payload_sha256"] = payload_sha256(run.normalized_race_payload)

    run_path.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    # Back-compat artefacts for tooling that still reads the split files. Never
    # clobber an upstream ingester's own artefacts when reusing its dir.
    audit_path = run_dir / "feature_audit.json"
    parsed_path = run_dir / "parsed_pp.json"
    if not audit_path.exists():
        audit_path.write_text(
            json.dumps(run.feature_audit, indent=2, default=str) + "\n", encoding="utf-8"
        )
    if not parsed_path.exists():
        parsed_path.write_text(
            json.dumps(run.normalized_race_payload, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    return {
        "ingestion_run": str(run_path),
        "feature_audit": str(run_dir / "feature_audit.json"),
        "parsed_pp": str(run_dir / "parsed_pp.json"),
        "audit_sha256": record["audit_sha256"],
        "payload_sha256": record["payload_sha256"],
    }


def load_ingestion_run(
    run_id: str,
    *,
    runs_root: Path | str = _RUNS_ROOT_DEFAULT,
) -> IngestionRun:
    """Read one immutable ingestion run by id. Raises if it is not found."""
    run_path = _run_dir(run_id, runs_root) / "ingestion_run.json"
    if not run_path.exists():
        raise IngestionRunBindingInvalid(
            f"no persisted ingestion run for id {run_id!r}"
        )
    try:
        record = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestionRunBindingInvalid(
            f"ingestion run {run_id!r} is unreadable: {exc}"
        ) from exc
    known = {f for f in IngestionRun.__dataclass_fields__}
    return IngestionRun(**{k: v for k, v in record.items() if k in known})


def validate_ingestion_run(
    run: IngestionRun | None,
    *,
    upload_sha256: str | None = None,
    parser_pipeline_version: str | None = None,
) -> IngestionRun:
    """Fail closed unless the run carries a usable audit + payload for its hash/version."""
    if run is None:
        raise IngestionRunBindingInvalid("ingestion run is missing")
    if not isinstance(run.feature_audit, dict) or not run.feature_audit:
        raise IngestionRunBindingInvalid("ingestion run has no feature audit")
    if not isinstance(run.normalized_race_payload, dict) or not run.normalized_race_payload:
        raise IngestionRunBindingInvalid("ingestion run has no normalized race payload")
    if upload_sha256 is not None and run.upload_sha256 != upload_sha256:
        raise IngestionRunBindingInvalid(
            "bound upload hash does not match the ingestion run"
        )
    if (
        parser_pipeline_version is not None
        and run.parser_pipeline_version != parser_pipeline_version
    ):
        raise IngestionRunBindingInvalid(
            "bound parser pipeline version does not match the ingestion run"
        )
    return run


# ── Card binding (exact pointer, never "latest") ──────────────────────────────

def ensure_ingestion_run_column(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(race_cards)")}
    if "ingestion_run_id" not in cols:
        conn.execute("ALTER TABLE race_cards ADD COLUMN ingestion_run_id TEXT")
        conn.commit()


def bind_card_to_ingestion_run(
    conn: sqlite3.Connection, card_id: int, ingestion_run_id: str
) -> None:
    ensure_ingestion_run_column(conn)
    conn.execute(
        "UPDATE race_cards SET ingestion_run_id=? WHERE card_id=?",
        (ingestion_run_id, int(card_id)),
    )
    conn.commit()


def card_ingestion_run_id(conn: sqlite3.Connection, card_id: int) -> str | None:
    ensure_ingestion_run_column(conn)
    row = conn.execute(
        "SELECT ingestion_run_id FROM race_cards WHERE card_id=?", (int(card_id),)
    ).fetchone()
    if not row:
        return None
    return row[0]


def load_card_ingestion_run(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    runs_root: Path | str = _RUNS_ROOT_DEFAULT,
) -> IngestionRun | None:
    """Return the exact ingestion run a card is bound to, or None if unbound.

    Raises :class:`IngestionRunBindingInvalid` when a binding exists but the
    run cannot be loaded.
    """
    run_id = card_ingestion_run_id(conn, card_id)
    if not run_id:
        return None
    return load_ingestion_run(run_id, runs_root=runs_root)
