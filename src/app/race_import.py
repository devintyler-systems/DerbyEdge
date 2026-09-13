"""Source-aware race-card upload dispatch, deliberately independent of Streamlit.

PDF parsing remains delegated to the established PDF service.  DraftKings
Markdown follows the isolated raw-card parser/validator lane and is blocked
before any caller can request feature staging when validation fails.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.derbyedge.tracks import resolve_track
from src.ingest.draftkings_markdown import (
    DraftKingsMarkdownCard,
    ExcelReconciliationResult,
    ValidationResult,
    parse_draftkings_markdown,
    reconcile_draftkings_excel,
    validate_draftkings_markdown_card,
)
from src.services.pdf_ingest import parse_race_pdf


PRIMARY_UPLOAD_LABEL = "Race Card Import"
PRIMARY_HELPER_TEXT = (
    "Upload a DraftKings Markdown race card (.md) for canonical pre-race ingestion, "
    "or a text-based PDF race page/sportsbook printout/Equibase card. Markdown is the "
    "preferred DK source. PDFs remain supported for legacy and non-DK sources. "
    "Scanned/image-only PDFs require Screenshot Ingest below."
)
PRIMARY_UPLOAD_FORMAT_TEXT = "200MB per file • Markdown, TXT, PDF"
EXCEL_QC_LABEL = "Optional Excel QC Reconciliation"
EXCEL_QC_HELPER_TEXT = (
    "Upload the matching DK Excel export only to compare recognizable runner, "
    "past-performance, and workout counts against the Markdown import. This file never "
    "feeds feature generation, scoring, calibration, or wagering decisions."
)
PRIMARY_EXTENSIONS = frozenset({".pdf", ".md", ".markdown", ".txt"})
MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})
PRIMARY_UPLOAD_TYPES = ("pdf", "md", "markdown", "txt")
EXCEL_QC_UPLOAD_TYPES = ("xlsx",)


def primary_uploader_config() -> dict[str, Any]:
    """The source-aware primary uploader contract, shared by UI and tests."""
    return {
        "label": PRIMARY_UPLOAD_LABEL,
        "help_text": PRIMARY_HELPER_TEXT,
        "accepted_types": PRIMARY_UPLOAD_TYPES,
        "format_text": PRIMARY_UPLOAD_FORMAT_TEXT,
        "max_size_mb": 200,
    }


def excel_qc_uploader_config() -> dict[str, Any]:
    """The deliberately isolated Excel QC uploader contract."""
    return {
        "label": EXCEL_QC_LABEL,
        "help_text": EXCEL_QC_HELPER_TEXT,
        "accepted_types": EXCEL_QC_UPLOAD_TYPES,
    }


class RaceImportDispatchError(ValueError):
    """A source selection error that can be shown directly in the import UI."""


@dataclasses.dataclass
class RaceImportDispatch:
    source_kind: str
    filename: str
    raw_sha256: str
    card: DraftKingsMarkdownCard | None = None
    validation: ValidationResult | None = None
    pdf_result: dict[str, Any] | None = None
    excel_result: ExcelReconciliationResult | None = None

    @property
    def feature_staging_allowed(self) -> bool:
        return self.source_kind == "markdown" and bool(self.validation and self.validation.passed)


def _extension(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _require_nonempty(filename: str, raw_file_bytes: bytes) -> None:
    if not filename or not filename.strip():
        raise RaceImportDispatchError("Upload is missing an original filename.")
    if not raw_file_bytes:
        raise RaceImportDispatchError("Upload is empty; select a non-empty race-card source file.")


def dispatch_primary_race_card_import(
    filename: str,
    raw_file_bytes: bytes,
    *,
    as_of: datetime | None = None,
    source_metadata: dict[str, Any] | None = None,
    pdf_parser: Callable[..., dict[str, Any]] = parse_race_pdf,
) -> RaceImportDispatch:
    """Classify primary upload bytes and route them to the only valid parser.

    This function performs no database writes, feature construction, scoring,
    calibration, fair-odds, or wager calculations.
    """
    _require_nonempty(filename, raw_file_bytes)
    suffix = _extension(filename)
    if suffix == ".xlsx":
        raise RaceImportDispatchError(
            "Excel is QC-only. Upload .xlsx files through Optional Excel QC Reconciliation; "
            "they are not primary model inputs."
        )
    if suffix not in PRIMARY_EXTENSIONS:
        raise RaceImportDispatchError(
            "Unsupported primary source type. Supported primary source types: .md, .markdown, .txt, .pdf."
        )

    import hashlib
    raw_sha256 = hashlib.sha256(raw_file_bytes).hexdigest()
    if suffix in MARKDOWN_EXTENSIONS:
        try:
            raw_text = raw_file_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RaceImportDispatchError(
                "Markdown/TXT upload is not valid UTF-8; export the DraftKings raw card as UTF-8 text."
            ) from exc
        if not raw_text.strip():
            raise RaceImportDispatchError("Markdown/TXT upload contains no readable text.")
        effective_as_of = as_of or datetime.now(timezone.utc)
        card = parse_draftkings_markdown(raw_text, source_path=filename, as_of=effective_as_of)
        validation = validate_draftkings_markdown_card(card)
        return RaceImportDispatch(
            source_kind="markdown", filename=filename, raw_sha256=raw_sha256,
            card=card, validation=validation,
        )

    metadata = source_metadata or {}
    # Preserve the established PDF service and its image-only/PDF diagnostics.
    pdf_result = pdf_parser(
        raw_file_bytes, filename=filename, stored_path=metadata.get("stored_path"),
    )
    return RaceImportDispatch(
        source_kind="pdf", filename=filename, raw_sha256=raw_sha256, pdf_result=pdf_result,
    )


def dispatch_excel_qc_reconciliation(filename: str, raw_file_bytes: bytes) -> RaceImportDispatch:
    """Run optional layout QC only; its result is permanently ineligible for staging."""
    _require_nonempty(filename, raw_file_bytes)
    if _extension(filename) != ".xlsx":
        raise RaceImportDispatchError("Optional Excel QC Reconciliation only accepts .xlsx files.")
    import hashlib
    excel_result = reconcile_draftkings_excel(raw_file_bytes, source_filename=filename)
    return RaceImportDispatch(
        source_kind="excel_qc", filename=filename,
        raw_sha256=hashlib.sha256(raw_file_bytes).hexdigest(), excel_result=excel_result,
    )


def markdown_import_summary(dispatch: RaceImportDispatch) -> dict[str, Any]:
    """Build the compact, auditable Markdown import summary used by Streamlit."""
    if dispatch.source_kind != "markdown" or dispatch.card is None or dispatch.validation is None:
        raise ValueError("Markdown import summary requires a Markdown dispatch result.")
    card, race, validation = dispatch.card, dispatch.card.race, dispatch.validation
    condition = f"{race.surface or '?'}: {race.surface_condition or '?'}"
    return {
        "source_filename": dispatch.filename,
        "source_sha256": dispatch.raw_sha256,
        "parser_version": card.parser_version,
        "validation_status": "PASS" if validation.passed else "FAIL",
        "track": race.track or "?",
        "target_race_date": race.race_date.isoformat() if race.race_date else "?",
        "race_number": race.race_number,
        "surface_and_condition": condition,
        "distance": race.distance or "?",
        "parsed_runner_count": validation.parsed_unique_runner_count,
        "past_performance_count": validation.past_performance_row_count,
        "workout_count": validation.workout_row_count,
        "as_of": card.as_of.isoformat(),
    }


def markdown_dispatch_to_legacy_ui_payload(dispatch: RaceImportDispatch) -> dict[str, Any]:
    """Adapt a validated Markdown card to the existing pre-race UI preview shape.

    This does not persist records or invoke feature staging.  The UI uses it
    only after the validator has passed, preserving the existing card-create
    flow while preventing invalid Markdown from reaching it.
    """
    if not dispatch.feature_staging_allowed or dispatch.card is None:
        raise RaceImportDispatchError("Markdown card validation must pass before it can enter the race-card workflow.")
    card = dispatch.card
    race = card.race
    track_resolution = resolve_track(track_name=race.track or "")
    runners = [
        {
            "program_number": entry.program_number,
            "post_position": entry.post_position,
            "horse_name": entry.horse_name,
            "jockey": entry.jockey,
            "trainer": entry.trainer,
            "ml": entry.morning_line,
            "morning_line": entry.morning_line,
            "medication_weight_equipment": entry.medication_weight_equipment,
            "weight": entry.weight,
            "is_scratched": False,
        }
        for entry in card.entries
    ]
    return {
        "ok": True,
        "error": None,
        "warnings": list(dispatch.validation.warnings if dispatch.validation else []),
        "track_code": track_resolution.get("track_code"),
        "track_code_resolved": track_resolution.get("track_code"),
        "track_name": race.track,
        "track_name_canonical": track_resolution.get("track_name_canonical"),
        "track_resolution_source": track_resolution.get("resolution_source"),
        "race_date": race.race_date.isoformat() if race.race_date else None,
        "race_number": race.race_number,
        "distance_text": race.distance,
        "surface": race.surface.title() if race.surface else None,
        "going": race.surface_condition,
        "race_type": race.class_code,
        "purse_usd": race.purse,
        "field_size": len(runners),
        "runners": runners,
        "is_draftkings_markdown": True,
        "markdown_dispatch": dispatch,
    }
