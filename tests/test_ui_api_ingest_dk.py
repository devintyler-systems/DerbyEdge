"""
tests/test_ui_api_ingest_dk.py

Integration and regression test suite verifying:
  1. POST /api/ingest/pdf routes DraftKings PDFs to draftkings_pdf adapter
  2. UI response and ingestion manifest include exact debug payload:
     upload, parser, and race_resolution
  3. Fixture invariants:
     - size_bytes == 254_922
     - page_count == 39
     - "SARATOGA", "RACE", "CLM35000", "1 1/16 M", "TURF" in raw_text
     - parsed.race_number == 9
     - parsed.runners_count == 10
  4. Content-based DraftKings detection works even without canonical filename
  5. Multi-race text contamination prevention (header detection confined to page 1)
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.ingest.draftkings_pdf import parse_draftkings_pdf, is_draftkings_pdf
from src.services.pdf_ingest import parse_race_pdf

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R9_9-2-26.pdf"
FIXTURE_FILENAME = "SAR_DK_Horse_R9_9-2-26.pdf"


@pytest.fixture(scope="module")
def fixture_bytes() -> bytes:
    if not FIXTURE_PATH.exists():
        pytest.skip(f"Golden fixture missing: {FIXTURE_PATH}")
    return FIXTURE_PATH.read_bytes()


def test_ui_ingest_dk_r9_golden_fixture_uses_dk_adapter(tmp_path, client):
    """Exact regression test specified in user diagnostic directive."""
    fixture = Path(
        "draftkings_racedata_pdfs/fixtures/"
        "SAR_DK_Horse_R9_9-2-26.pdf"
    )

    response = client.post(
        "/api/ingest/pdf",
        files={"file": ("SAR_DK_Horse_R9_9-2-26.pdf", fixture.read_bytes())},
    )

    assert response.status_code == 200, f"API error: {response.text}"
    payload = response.json()

    assert payload["parser"]["adapter_selected"] == "draftkings_pdf"
    assert payload["upload"]["size_bytes"] == 254_922
    assert payload["parser"]["page_count"] == 39
    assert payload["race"]["track_code"] == "SAR"
    assert payload["race"]["race_date"] == "2026-09-02"
    assert payload["race"]["race_number"] == 9
    assert payload["race"]["distance_text"] == "1 1/16 M"
    assert payload["race"]["surface"] == "Turf"
    assert payload["race"]["runners_count"] == 10


def test_fixture_invariants(fixture_bytes):
    """Verify exact fixture invariants specified in user diagnostic directive."""
    res = parse_race_pdf(fixture_bytes, filename=FIXTURE_FILENAME)

    uploaded = res["upload"]
    parser = res["parser"]
    parsed = res["parsed_race"]

    assert uploaded.size_bytes == 254_922
    assert parser.page_count == 39
    assert "SARATOGA" in parser.raw_text
    assert "RACE" in parser.raw_text
    assert "CLM35000" in parser.raw_text
    assert "1 1/16 M" in parser.raw_text
    assert "TURF" in parser.raw_text
    assert parsed.race_number == 9
    assert parsed.runners_count == 10


def test_debug_payload_exact_keys(fixture_bytes):
    """Verify the exact debug payload structure is in the response and manifest."""
    res = parse_race_pdf(fixture_bytes, filename=FIXTURE_FILENAME)

    # Upload section
    upload = res["upload"]
    assert upload["original_filename"] == FIXTURE_FILENAME
    assert upload["size_bytes"] == 254_922
    assert upload["sha256"] == hashlib.sha256(fixture_bytes).hexdigest()
    assert "stored_path" in upload
    assert "uploaded_at" in upload

    # Parser section
    parser = res["parser"]
    assert parser["adapter_selected"] == "draftkings_pdf"
    assert parser["adapter_version"] == "1.0.0"
    assert parser["page_count"] == 39
    assert len(parser["first_500_chars"]) <= 500
    assert parser["raw_text_sha256"] == hashlib.sha256(parser["raw_text"].encode("utf-8")).hexdigest()

    # Race resolution section
    race_res = res["race_resolution"]
    assert race_res["filename_race_number"] == 9
    assert race_res["header_race_number"] == 9
    assert race_res["selected_race_number"] == 9
    assert "SAR" in race_res["track_candidates"]
    assert 9 in race_res["race_candidates"]
    assert race_res["header_pages_scanned"] == [1]

    # Ingestion manifest has matching debug payload
    manifest = res["manifest"]
    assert manifest["upload"]["size_bytes"] == 254_922
    assert manifest["parser"]["adapter_selected"] == "draftkings_pdf"
    assert manifest["race_resolution"]["selected_race_number"] == 9


def test_content_based_detection_without_dk_filename(fixture_bytes):
    """DraftKings adapter is selected even if uploaded under a generic filename like 'upload.pdf'."""
    res = parse_race_pdf(fixture_bytes, filename="upload.pdf")
    assert res["is_draftkings"] is True
    assert res["parser"]["adapter_selected"] == "draftkings_pdf"
    assert res["race"]["track_code"] == "SAR"
    assert res["race"]["race_number"] == 9
    assert res["race"]["runners_count"] == 10


def test_header_detection_confined_to_page_one(fixture_bytes):
    """Target race header resolution must scan Page 1 only to prevent multi-race contamination."""
    parsed = parse_draftkings_pdf(fixture_bytes, filename=FIXTURE_FILENAME)
    assert parsed.manifest["race_resolution"]["header_pages_scanned"] == [1]
    # Header race number must agree with target race 9, not prior-performance race numbers (e.g. R1, R2)
    assert parsed.header_race_number == 9
    assert parsed.filename_race_number == 9
