"""Read-only reconnaissance over local candidate source artifacts.

These tests pin the contract that reconnaissance never ingests, never parses into
canonical tables, never opens SQLite, and never treats a filename or filesystem
timestamp as source-as-of proof.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.services.historical_source_recon import (
    RECON_LABEL,
    ReconRootError,
    scan_roots,
    write_recon_artifacts,
)

ROOT = Path(__file__).resolve().parents[1]

_DK_MARKDOWN = """Saratoga
RACE 6
1:03
PM
$20K CLAIMING
Purse: $42K
 3YO+
 1 M
Dirt: Fast
More
PROGRAM
POOLS
PPs
RESULTS
#
ODDS
ML
Runner
1
20
20
Some Horse
L122
A Jockey
A Trainer
"""

_SIMD_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<EntryRaceCard xsi:noNamespaceSchemaLocation='
    '"http://ifd.equibase.com/schema/simulcast.xsd" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
    "  <Race><RaceNumber>1</RaceNumber></Race>\n"
    "</EntryRaceCard>\n"
)

_PDF_HEAD = b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\n1 0 obj\n<</Title (Equibase Chart)>>\n"


def _make_corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "SAR_DK_Horse_R6_9-4-26.md").write_text(_DK_MARKDOWN, encoding="utf-8")
    (root / "eqb_SAR_2026-09-04_R6_chart.pdf").write_bytes(_PDF_HEAD + b"Saratoga race 6 chart\n")
    (root / "SIMD20260904SAR_USA.xml").write_text(_SIMD_XML, encoding="utf-8")
    (root / "SAR_DK_Horse_R7.md").write_text(_DK_MARKDOWN.replace("RACE 6", "RACE 7"), encoding="utf-8")
    (root / "loose_notes.md").write_text("random handicapping notes, nothing structured\n", encoding="utf-8")
    (root / "archive.pkl").write_bytes(b"\x80\x04not-a-source")
    return root


def _item(report, name):
    matches = [c for c in report.inventory if Path(c.path).name == name]
    assert matches, f"{name} not in inventory"
    return matches[0]


def test_identifies_dk_pre_race_card_candidate(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    report = scan_roots([root])
    card = _item(report, "SAR_DK_Horse_R6_9-4-26.md")
    assert card.classification == "PRE_RACE_CARD_CANDIDATE"
    assert card.provider_candidate == "draftkings_markdown"
    assert card.track_code == "SAR"
    assert card.race_number == 6
    assert card.race_date == "2026-09-04"
    assert card.filename_date_status == "PARSED_FROM_FILENAME"
    assert card.scheduled_post_time_raw == "1:03 PM"
    assert card.scheduled_post_time_status == "WALL_CLOCK_NO_DATE_NO_TZ"
    assert card.candidate_key == "SAR|2026-09-04|R6"
    assert card.recon_label == RECON_LABEL


def test_identifies_result_candidate(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    report = scan_roots([root])
    result = _item(report, "eqb_SAR_2026-09-04_R6_chart.pdf")
    assert result.classification == "RESULT_CANDIDATE"
    assert result.provider_candidate == "equibase_results_pdf"
    assert result.track_code == "SAR"
    assert result.race_date == "2026-09-04"
    assert result.race_number == 6
    assert result.candidate_key == "SAR|2026-09-04|R6"


def test_simd_xml_is_a_pre_race_card_candidate(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    report = scan_roots([root])
    simd = _item(report, "SIMD20260904SAR_USA.xml")
    assert simd.classification == "PRE_RACE_CARD_CANDIDATE"
    assert simd.provider_candidate == "equibase_simd_xml"
    assert simd.race_date == "2026-09-04"
    assert simd.race_number is None
    # A full-card file with no race number cannot form an exact candidate key.
    assert simd.candidate_key is None


def test_unrecognized_and_unsupported_files(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    report = scan_roots([root])
    assert _item(report, "loose_notes.md").classification == "UNKNOWN_CANDIDATE"
    assert _item(report, "archive.pkl").classification == "UNSUPPORTED"


def test_pairs_exact_track_date_race_deterministically(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    report = scan_roots([root])
    pairs = [p for p in report.pairs if p.candidate_key == "SAR|2026-09-04|R6"]
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.pair_status == "DETERMINISTIC_PAIR"
    assert any(name.endswith("R6_9-4-26.md") for name in pair.pre_race_paths)
    assert any(name.endswith("R6_chart.pdf") for name in pair.result_paths)
    assert pair.recon_label == RECON_LABEL


def test_does_not_pair_ambiguous_or_missing_identity(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    report = scan_roots([root])
    dateless = _item(report, "SAR_DK_Horse_R7.md")
    assert dateless.race_date is None
    assert dateless.filename_date_status == "ABSENT"
    assert dateless.candidate_key is None
    assert all(p.candidate_key is not None for p in report.pairs)
    assert not any("R7" in n for p in report.pairs for n in p.pre_race_paths)


def test_never_treats_mtime_or_filename_date_as_source_as_of(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    report = scan_roots([root])
    assert report.inventory
    for card in report.inventory:
        assert card.source_as_of_status == "REQUIRES_MANIFEST_EVIDENCE"
    date_rows = [
        m for m in report.missing_evidence
        if m.candidate_key == "SAR|2026-09-04|R6" and m.schema_field == "target_race_date"
    ]
    assert date_rows and date_rows[0].status == "FILENAME_DERIVED_NOT_PROVEN"
    as_of_rows = [
        m for m in report.missing_evidence
        if m.candidate_key == "SAR|2026-09-04|R6" and m.schema_field == "source_as_of_timestamp"
    ]
    assert as_of_rows and as_of_rows[0].status == "REQUIRES_MANIFEST"


def test_missing_evidence_classifications_and_no_contract_complete(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    report = scan_roots([root])
    key = "SAR|2026-09-04|R6"
    by_field = {
        m.schema_field: m.status
        for m in report.missing_evidence
        if m.candidate_key == key
    }
    assert by_field["sha256"] == "AVAILABLE"
    assert by_field["raw_artifact_retained"] == "AVAILABLE"
    assert by_field["target_track_code"] == "AVAILABLE"
    assert by_field["target_race_number"] == "AVAILABLE"
    assert by_field["target_surface"] == "REQUIRES_PARSE"
    assert by_field["feature_vector_status"] == "REQUIRES_PARSE"
    assert by_field["target_scheduled_post_timestamp"] == "REQUIRES_MANIFEST"
    assert by_field["outcome_provenance_status"] == "REQUIRES_MANIFEST"
    # This key has a paired result candidate, so the reference location exists.
    assert by_field["outcome_reference"] == "AVAILABLE"
    assert report.summary["contract_complete_candidate_count"] == 0


def test_card_only_key_marks_outcome_reference_missing(tmp_path: Path) -> None:
    root = tmp_path / "cards_only"
    root.mkdir()
    (root / "DMR_DK_Horse_R4_9-3-26.md").write_text(_DK_MARKDOWN.replace("Saratoga", "Del Mar"), encoding="utf-8")
    report = scan_roots([root])
    key = "DMR|2026-09-03|R4"
    pair = [p for p in report.pairs if p.candidate_key == key]
    assert pair and pair[0].pair_status == "CARD_ONLY"
    outcome = [
        m for m in report.missing_evidence
        if m.candidate_key == key and m.schema_field == "outcome_reference"
    ]
    assert outcome and outcome[0].status == "REQUIRES_RESULT_ARTIFACT"


def test_write_recon_artifacts_is_deterministic(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    report = scan_roots([root])
    out = tmp_path / "out"
    first = write_recon_artifacts(report, out)
    blobs = {name: Path(p).read_bytes() for name, p in first.items()}
    second = write_recon_artifacts(report, out)
    for name, path in second.items():
        assert Path(path).read_bytes() == blobs[name]
    assert set(first) == {
        "inventory_csv", "pairs_csv", "missing_evidence_csv", "summary_json"
    }
    summary = json.loads(Path(first["summary_json"]).read_text(encoding="utf-8"))
    assert summary["candidate_card_count"] == 3
    assert summary["candidate_result_count"] == 1
    assert summary["deterministic_pair_count"] == 1
    assert summary["contract_complete_candidate_count"] == 0


def test_scan_roots_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ReconRootError):
        scan_roots([tmp_path / "does-not-exist"])


def test_scan_does_not_create_or_modify_any_database(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    sentinel = db_dir / "derbyedge.db"
    sentinel.write_bytes(b"SQLite format 3\x00 sentinel bytes")
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    report = scan_roots([root])
    write_recon_artifacts(report, tmp_path / "out")
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == before
    created_dbs = [
        p for p in tmp_path.rglob("*.db") if p != sentinel
    ] + list(tmp_path.rglob("*.sqlite")) + list(tmp_path.rglob("*.sqlite3"))
    assert created_dbs == []


def test_cli_exit_codes(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    db = ROOT / "db" / "derbyedge.db"
    db_before = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
    out_dir = tmp_path / "acceptance"
    ok = subprocess.run(
        [sys.executable, "scripts/recon_historical_sources.py", "--root", str(root),
         "--output-dir", str(out_dir)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert ok.returncode == 0, ok.stderr + ok.stdout
    assert "candidate-card" in ok.stdout.lower()
    assert (out_dir / "historical_source_recon_summary.json").is_file()

    bad = subprocess.run(
        [sys.executable, "scripts/recon_historical_sources.py", "--root", str(tmp_path / "nope")],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert bad.returncode == 2

    if db_before is not None:
        assert hashlib.sha256(db.read_bytes()).hexdigest() == db_before
