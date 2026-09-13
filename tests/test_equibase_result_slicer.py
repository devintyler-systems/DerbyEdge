"""Read-only Equibase full-card result slicing / race-level indexing.

The slicer never writes split PDFs, never rewrites source files, and never opens
SQLite. It only turns retained ``eqb_*_fullcard.pdf`` charts into race-level
candidate reference rows, every one labelled ``RECON_ONLY_NOT_ELIGIBLE``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.services.equibase_result_slicer import (
    RESULT_RECON_LABEL,
    ResultSliceRootError,
    scan_result_roots,
    slice_result_text,
    write_result_index_artifacts,
)

_TWO_RACE_TEXT = """GULFSTREAM PARK - January 3, 2026 - Race 1
MAIDEN SPECIAL WEIGHT - Thoroughbred
Purse: $84,000
Off at: 12:26 Start: Good for all
--- 3 Doctrine(Zayas,Edgard) 122 L 3 8 6 5 4 2 1 24.10 hitgate
Fractional Times:23.57 46.97 1:09.92 Final Time:1:34.07
Winner: Doctrine, Bay Colt, by Constitution out of Traffic Blimp, by Medaglia d'Oro.
Total WPS Pool: $237,498
Copyright 2026 Equibase Company LLC. All Rights Reserved.
GULFSTREAM PARK - January 3, 2026 - Race 2
CLAIMING - Thoroughbred
Winner: Hillbilly Bob, Bay Gelding, by Always Dreaming out of Mankai Blossom.
Total WPS Pool: $150,000
Fractional Times:22.10 45.30 Final Time:1:10.20
Copyright 2026 Equibase Company LLC. All Rights Reserved.
"""

_NO_NUMBER_TEXT = """GULFSTREAM PARK - January 3, 2026 - Race
MAIDEN SPECIAL WEIGHT - Thoroughbred
Winner: Doctrine, Bay Colt, by Constitution.
Fractional Times:23.57 Final Time:1:34.07
Copyright 2026 Equibase Company LLC. All Rights Reserved.
"""

_NO_OFFICIAL_TEXT = """CHURCHILL DOWNS - April 25, 2026 - Race 4
ALLOWANCE - Thoroughbred
Pgm Horse Name (Jockey) Wgt PP Odds Comments
2 Somehorse (Smith, J) 122 2 3.10
Copyright 2026 Equibase Company LLC. All Rights Reserved.
"""


def _ref(refs, race_number):
    matches = [r for r in refs if r.race_number == race_number]
    assert matches, f"race {race_number} not sliced"
    return matches[0]


def test_identifies_per_race_sections() -> None:
    refs = slice_result_text(
        _TWO_RACE_TEXT, source_file_path="eqb_GP_2026-01-03_fullcard.pdf",
        source_file_sha256="0" * 64, filename_track="GP", filename_date="2026-01-03",
    )
    assert [r.race_number for r in refs] == [1, 2]
    assert all(r.status == RESULT_RECON_LABEL for r in refs)


def test_race_number_yields_deterministic_candidate_key() -> None:
    refs = slice_result_text(
        _TWO_RACE_TEXT, source_file_path="eqb_GP_2026-01-03_fullcard.pdf",
        source_file_sha256="0" * 64, filename_track="GP", filename_date="2026-01-03",
    )
    ref = _ref(refs, 1)
    assert ref.candidate_key == "GP|2026-01-03|R1"
    assert ref.race_date == "2026-01-03"
    assert ref.winner_detected is True
    assert ref.official_status_detected is True
    assert ref.uncertainty_code == ""
    assert ref.extraction_confidence == "HIGH"


def test_missing_race_number_yields_uncertainty_code() -> None:
    refs = slice_result_text(
        _NO_NUMBER_TEXT, source_file_path="eqb_GP_2026-01-03_fullcard.pdf",
        source_file_sha256="0" * 64, filename_track="GP", filename_date="2026-01-03",
    )
    assert len(refs) == 1
    assert refs[0].race_number is None
    assert refs[0].candidate_key == ""
    assert "MISSING_RACE_NUMBER" in refs[0].uncertainty_code


def test_missing_official_status_and_winner_yield_codes() -> None:
    refs = slice_result_text(
        _NO_OFFICIAL_TEXT, source_file_path="eqb_CD_2026-04-25_fullcard.pdf",
        source_file_sha256="0" * 64, filename_track="CD", filename_date="2026-04-25",
    )
    assert len(refs) == 1
    ref = refs[0]
    assert ref.winner_detected is False
    assert ref.official_status_detected is False
    assert "WINNER_NOT_FOUND" in ref.uncertainty_code
    assert "OFFICIAL_STATUS_NOT_FOUND" in ref.uncertainty_code
    # Race identity is still deterministic from the header.
    assert ref.candidate_key == "CD|2026-04-25|R4"


def test_no_race_sections_found() -> None:
    refs = slice_result_text(
        "just some prose with no equibase race headers at all\n",
        source_file_path="eqb_GP_2026-01-03_fullcard.pdf", source_file_sha256="0" * 64,
        filename_track="GP", filename_date="2026-01-03",
    )
    assert len(refs) == 1
    assert refs[0].uncertainty_code == "NO_RACE_SECTIONS_FOUND"
    assert refs[0].candidate_key == ""


def test_ambiguous_track_identity_suppresses_key() -> None:
    text = _TWO_RACE_TEXT.replace("GULFSTREAM PARK", "MYSTERY OVAL DOWNS")
    refs = slice_result_text(
        text, source_file_path="eqb_GP_2026-01-03_fullcard.pdf",
        source_file_sha256="0" * 64, filename_track="GP", filename_date="2026-01-03",
    )
    assert "AMBIGUOUS_TRACK_IDENTITY" in refs[0].uncertainty_code
    assert refs[0].candidate_key == ""


def test_scan_result_roots_is_sha_read_only_and_touches_no_db(tmp_path: Path) -> None:
    root = tmp_path / "historical_results" / "2026" / "01" / "03"
    root.mkdir(parents=True)
    pdf = root / "eqb_GP_2026-01-03_fullcard.pdf"
    pdf.write_bytes(b"%PDF-1.4 not-really-a-valid-pdf-body")
    before = hashlib.sha256(pdf.read_bytes()).hexdigest()

    db = tmp_path / "db" / "derbyedge.db"
    db.parent.mkdir()
    db.write_bytes(b"SQLite format 3\x00 sentinel")
    db_before = hashlib.sha256(db.read_bytes()).hexdigest()

    index = scan_result_roots([tmp_path / "historical_results"])
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == before
    assert hashlib.sha256(db.read_bytes()).hexdigest() == db_before

    rows = [r for r in index.rows if r.source_file_path.endswith("_fullcard.pdf")]
    assert rows and rows[0].source_file_sha256 == before
    assert "PDF_TEXT_EXTRACTION_FAILED" in rows[0].uncertainty_code
    assert not list((tmp_path / "historical_results").rglob("*.db"))
    assert not list((tmp_path / "historical_results").rglob("*_R*.pdf"))  # no split PDFs


def test_scan_only_inventories_fullcard_pdfs(tmp_path: Path) -> None:
    root = tmp_path / "hr"
    root.mkdir()
    (root / "eqb_GP_2026-01-03_fullcard.pdf").write_bytes(b"%PDF-1.4 broken")
    (root / "eqb_GP_2026-01-03_race1.pdf").write_bytes(b"%PDF-1.4 broken")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    index = scan_result_roots([root])
    assert {Path(r.source_file_path).name for r in index.rows} == {"eqb_GP_2026-01-03_fullcard.pdf"}


def test_write_result_index_artifacts_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "hr"
    root.mkdir()
    (root / "eqb_GP_2026-01-03_fullcard.pdf").write_bytes(b"%PDF-1.4 broken")
    index = scan_result_roots([root])
    out = tmp_path / "out"
    first = write_result_index_artifacts(index, out)
    blobs = {k: Path(v).read_bytes() for k, v in first.items()}
    second = write_result_index_artifacts(index, out)
    for k, v in second.items():
        assert Path(v).read_bytes() == blobs[k]
    assert set(first) == {"race_index_csv", "summary_json"}
    summary = json.loads(Path(first["summary_json"]).read_text(encoding="utf-8"))
    assert summary["fullcard_pdf_count"] == 1


def test_scan_result_roots_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ResultSliceRootError):
        scan_result_roots([tmp_path / "nope"])
