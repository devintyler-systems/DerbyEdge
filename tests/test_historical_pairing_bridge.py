"""Read-only exact-key pairing between recon card inventory and the result index.

Pairing here is advisory reconnaissance only. No row it produces is training
eligible; every row is labelled ``RECON_ONLY_NOT_ELIGIBLE`` and still carries the
contract evidence that remains missing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.services.historical_pairing_bridge import (
    BridgeInputError,
    build_bridge,
    write_bridge_artifacts,
)

_CARD_ROWS = [
    {"candidate_key": "SAR|2026-09-04|R6", "classification": "PRE_RACE_CARD_CANDIDATE",
     "track_code": "SAR", "race_date": "2026-09-04", "race_number": "6",
     "path": "/c/x/SAR_DK_Horse_R6_9-4-26.md", "provider_candidate": "draftkings_markdown"},
    {"candidate_key": "DMR|2026-09-03|R4", "classification": "PRE_RACE_CARD_CANDIDATE",
     "track_code": "DMR", "race_date": "2026-09-03", "race_number": "4",
     "path": "/c/x/DMR_DK_Horse_R4_9-3-26.pdf", "provider_candidate": "draftkings_pdf"},
    {"candidate_key": "", "classification": "UNKNOWN_CANDIDATE", "track_code": "",
     "race_date": "", "race_number": "", "path": "/c/x/loose_notes.md", "provider_candidate": ""},
]

_RESULT_ROWS = [
    {"candidate_key": "SAR|2026-09-04|R6", "track_code_or_name": "SAR", "race_date": "2026-09-04",
     "race_number": "6", "source_file_path": "/c/hr/eqb_SAR_2026-09-04_fullcard.pdf",
     "official_status_detected": "True", "winner_detected": "True", "uncertainty_code": ""},
    {"candidate_key": "GP|2026-01-03|R1", "track_code_or_name": "GP", "race_date": "2026-01-03",
     "race_number": "1", "source_file_path": "/c/hr/eqb_GP_2026-01-03_fullcard.pdf",
     "official_status_detected": "True", "winner_detected": "True", "uncertainty_code": ""},
]


def _by_key(report):
    return {r.candidate_key: r for r in report.rows}


def test_exact_key_pairing() -> None:
    report = build_bridge(_CARD_ROWS, _RESULT_ROWS)
    row = _by_key(report)["SAR|2026-09-04|R6"]
    assert row.pair_status == "PAIRED_EXACT_KEY"
    assert row.recon_label == "RECON_ONLY_NOT_ELIGIBLE"
    assert any("SAR_DK_Horse_R6" in p for p in row.card_paths)
    assert any("eqb_SAR_2026-09-04_fullcard" in p for p in row.result_paths)
    # A retained result reference is not outcome provenance sufficiency.
    assert "outcome_provenance_status" in row.missing_evidence
    assert row.missing_evidence["outcome_provenance_status"] == "REQUIRES_MANIFEST"


def test_card_only_and_result_only_rows() -> None:
    report = build_bridge(_CARD_ROWS, _RESULT_ROWS)
    by_key = _by_key(report)
    assert by_key["DMR|2026-09-03|R4"].pair_status == "CARD_ONLY"
    assert by_key["GP|2026-01-03|R1"].pair_status == "RESULT_ONLY"
    assert all(r.recon_label == "RECON_ONLY_NOT_ELIGIBLE" for r in report.rows)


def test_ambiguous_result_key_does_not_pair() -> None:
    dup_results = _RESULT_ROWS + [
        {"candidate_key": "SAR|2026-09-04|R6", "track_code_or_name": "SAR",
         "race_date": "2026-09-04", "race_number": "6",
         "source_file_path": "/c/hr/other_eqb_SAR_2026-09-04_fullcard.pdf",
         "official_status_detected": "True", "winner_detected": "True", "uncertainty_code": ""},
    ]
    report = build_bridge(_CARD_ROWS, dup_results)
    row = _by_key(report)["SAR|2026-09-04|R6"]
    assert row.pair_status == "AMBIGUOUS_RESULT_KEY"


def test_key_mismatch_when_same_track_date_different_race() -> None:
    results = [
        {"candidate_key": "DMR|2026-09-03|R7", "track_code_or_name": "DMR",
         "race_date": "2026-09-03", "race_number": "7",
         "source_file_path": "/c/hr/eqb_DMR_2026-09-03_fullcard.pdf",
         "official_status_detected": "True", "winner_detected": "True", "uncertainty_code": ""},
    ]
    report = build_bridge(_CARD_ROWS, results)
    row = _by_key(report)["DMR|2026-09-03|R4"]
    assert row.pair_status == "KEY_MISMATCH"


def test_outputs_are_deterministic(tmp_path: Path) -> None:
    report = build_bridge(_CARD_ROWS, _RESULT_ROWS)
    out = tmp_path / "out"
    first = write_bridge_artifacts(report, out)
    blobs = {k: Path(v).read_bytes() for k, v in first.items()}
    second = write_bridge_artifacts(report, out)
    for k, v in second.items():
        assert Path(v).read_bytes() == blobs[k]
    summary = json.loads(Path(first["summary_json"]).read_text(encoding="utf-8"))
    assert summary["paired_exact_key_count"] == 1
    assert summary["card_only_count"] == 1
    assert summary["result_only_count"] == 1
    assert summary["recon_label"] == "RECON_ONLY_NOT_ELIGIBLE"


def test_build_bridge_rejects_non_row_inputs() -> None:
    with pytest.raises(BridgeInputError):
        build_bridge(None, _RESULT_ROWS)
