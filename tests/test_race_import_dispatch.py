"""Source-aware race-card import routing independent of Streamlit rendering."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.app.race_import import (
    EXCEL_QC_HELPER_TEXT,
    PRIMARY_HELPER_TEXT,
    PRIMARY_UPLOAD_LABEL,
    PRIMARY_UPLOAD_FORMAT_TEXT,
    RaceImportDispatchError,
    dispatch_excel_qc_reconciliation,
    dispatch_primary_race_card_import,
    markdown_import_summary,
    primary_uploader_config,
    excel_qc_uploader_config,
)


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_FIXTURE = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R6_9-4-26.md"
EXCEL_FIXTURE = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R6_9-4-26.xlsx"
AS_OF = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("extension", ["md", "markdown", "txt"])
def test_primary_dispatch_accepts_markdown_extensions(extension):
    result = dispatch_primary_race_card_import(
        f"SAR_DK_Horse_R6_9-4-26.{extension}", MARKDOWN_FIXTURE.read_bytes(), as_of=AS_OF,
    )

    assert result.source_kind == "markdown"
    assert result.validation is not None and result.validation.passed


def test_valid_markdown_reaches_existing_parser_validator_and_summary():
    result = dispatch_primary_race_card_import(
        MARKDOWN_FIXTURE.name, MARKDOWN_FIXTURE.read_bytes(), as_of=AS_OF,
    )
    summary = markdown_import_summary(result)

    assert result.card is not None
    assert result.validation is not None and result.validation.passed
    assert summary["validation_status"] == "PASS"
    assert summary["track"] == "Saratoga"
    assert summary["parsed_runner_count"] == 10
    assert summary["past_performance_count"] > 0
    assert summary["workout_count"] > 0


def test_truncated_markdown_is_blocked_before_feature_staging():
    source = b"""Saratoga
RACE 6
Purse: $42K
3YO+
1 M
Dirt: Fast
PROGRAM
1
20
20
Horse One
L122
Jockey One
Trainer One
ALL RACES DIST SURF-COND PRG (PP) ODDS FIN BL JOCKEY COMMENT
Aug 1, '26
SARATOGA
CLM20000
1 M DIRT-Fast 1 2 1 0.5 Rider Saved ground
"""
    result = dispatch_primary_race_card_import("truncated.md", source, as_of=AS_OF)

    assert result.source_kind == "markdown"
    assert result.validation is not None and not result.validation.passed
    assert result.feature_staging_allowed is False
    assert any("boundary" in error.lower() for error in result.validation.errors)


def test_pdf_route_stays_pdf_and_never_calls_markdown_parser():
    calls: list[tuple[bytes, str]] = []

    def fake_pdf_parser(raw: bytes, *, filename: str, stored_path: str | None = None):
        calls.append((raw, filename))
        return {"ok": True, "runners": [], "warnings": [], "route": "pdf"}

    result = dispatch_primary_race_card_import(
        "legacy-card.pdf", b"%PDF-1.7\nexample", as_of=AS_OF, pdf_parser=fake_pdf_parser,
    )

    assert result.source_kind == "pdf"
    assert result.pdf_result == {"ok": True, "runners": [], "warnings": [], "route": "pdf"}
    assert calls == [(b"%PDF-1.7\nexample", "legacy-card.pdf")]


def test_primary_route_rejects_excel_and_unknown_extensions():
    with pytest.raises(RaceImportDispatchError, match="Excel QC"):
        dispatch_primary_race_card_import("card.xlsx", b"not a model input", as_of=AS_OF)
    with pytest.raises(RaceImportDispatchError, match="Supported primary source types"):
        dispatch_primary_race_card_import("card.csv", b"x", as_of=AS_OF)


def test_primary_dispatch_rejects_empty_and_non_utf8_markdown_content():
    with pytest.raises(RaceImportDispatchError, match="empty"):
        dispatch_primary_race_card_import("card.md", b"", as_of=AS_OF)
    with pytest.raises(RaceImportDispatchError, match="valid UTF-8"):
        dispatch_primary_race_card_import("card.md", b"\xff\xfe", as_of=AS_OF)


def test_excel_is_accepted_only_by_qc_dispatch_and_cannot_stage_features():
    result = dispatch_excel_qc_reconciliation(EXCEL_FIXTURE.name, EXCEL_FIXTURE.read_bytes())

    assert result.source_kind == "excel_qc"
    assert result.feature_staging_allowed is False
    assert result.excel_result is not None
    with pytest.raises(RaceImportDispatchError, match="only accepts .xlsx"):
        dispatch_excel_qc_reconciliation("card.md", b"markdown")


def test_source_aware_ui_copy_advertises_markdown_and_pdf_without_pdf_only_label():
    primary = primary_uploader_config()
    excel_qc = excel_qc_uploader_config()
    assert PRIMARY_UPLOAD_LABEL == "Race Card Import"
    assert "DraftKings Markdown" in PRIMARY_HELPER_TEXT
    assert "Markdown, TXT, PDF" in PRIMARY_UPLOAD_FORMAT_TEXT
    assert "PDF Race Import" not in PRIMARY_UPLOAD_LABEL
    assert "never feeds feature generation" in EXCEL_QC_HELPER_TEXT
    assert primary["accepted_types"] == ("pdf", "md", "markdown", "txt")
    assert primary["max_size_mb"] == 200
    assert excel_qc["accepted_types"] == ("xlsx",)
