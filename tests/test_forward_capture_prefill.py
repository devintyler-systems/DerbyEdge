"""Read-only forward-capture manifest prefill scaffolding.

The prefill helper turns a future DK pre-race card path plus an Equibase
result-index row into a *draft* worksheet: deterministic identity fields are
filled, every contract field that still needs operator evidence is listed as a
gap, and the whole thing is labelled ``DRAFT_NOT_ELIGIBLE``.  It never ingests,
never opens SQLite, and its draft can never pass the contract audit on its own.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.services.forward_capture_prefill import (
    DRAFT_LABEL,
    ForwardCapturePrefillError,
    load_result_row,
    prefill_forward_capture,
    to_contract_manifest_draft,
    write_prefill_artifacts,
)
from src.services.historical_snapshot_contract_audit import validate_manifest_payload

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


def _dk_card(tmp_path: Path, name: str = "SAR_DK_Horse_R6_9-4-26.md") -> Path:
    path = tmp_path / name
    path.write_text(_DK_MARKDOWN, encoding="utf-8")
    return path


def _result_file(tmp_path: Path, name: str = "eqb_SAR_2026-09-04_fullcard.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\nfake equibase chart bytes for SAR race 6\n")
    return path


def _result_row(result_path: Path, candidate_key: str = "SAR|2026-09-04|R6") -> dict:
    return {
        "candidate_key": candidate_key,
        "track_code_or_name": candidate_key.split("|")[0],
        "race_date": candidate_key.split("|")[1],
        "race_number": candidate_key.split("|")[2].lstrip("R"),
        "source_file_path": str(result_path),
        "source_file_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "official_status_detected": "True",
        "winner_detected": "True",
        "winner_name": "Some Horse",
        "uncertainty_code": "",
        "extraction_confidence": "HIGH",
        "status": "RECON_ONLY_NOT_ELIGIBLE",
    }


def test_prefills_deterministic_identity_fields(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    result = prefill_forward_capture(dk, _result_row(res))

    assert result.candidate_key == "SAR|2026-09-04|R6"
    assert result.draft_status == DRAFT_LABEL
    pf = result.prefilled
    assert pf["pre_race_artifact_path_or_uri"].endswith("SAR_DK_Horse_R6_9-4-26.md")
    assert pf["pre_race_sha256"] == hashlib.sha256(dk.read_bytes()).hexdigest()
    assert pf["result_artifact_path_or_uri"].endswith("eqb_SAR_2026-09-04_fullcard.pdf")
    assert pf["result_sha256"] == hashlib.sha256(res.read_bytes()).hexdigest()
    assert pf["target_track_code"] == "SAR"
    assert pf["target_race_date"] == "2026-09-04"
    assert pf["target_race_number"] == 6
    assert "draftkings" in pf["source_provider"]
    assert pf["outcome_reference"]["path_or_uri"].endswith("eqb_SAR_2026-09-04_fullcard.pdf")
    assert pf["outcome_reference"]["sha256"] == pf["result_sha256"]


def test_unresolved_contract_fields_listed_as_gaps(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    result = prefill_forward_capture(dk, _result_row(res))

    gap_fields = {g.field for g in result.gaps}
    for required in (
        "source_as_of_timestamp",
        "source_as_of_tier",
        "source_as_of_provenance",
        "target_scheduled_post_timestamp",
        "target_surface",
        "target_distance_furlongs",
        "expected_active_starter_count",
        "observed_active_starter_count",
        "identity_reconciliation_status",
        "field_completeness_status",
        "feature_vector_status",
        "parser_version",
        "outcome_provenance_status",
    ):
        assert required in gap_fields, f"{required} should be an operator gap"
    assert all(g.reason for g in result.gaps)
    # A filename date is prefilled but explicitly not treated as proof.
    assert result.prefilled["target_race_date_provenance"] == "FILENAME_DERIVED"


def test_candidate_key_mismatch_fails_safely(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    with pytest.raises(ForwardCapturePrefillError):
        prefill_forward_capture(dk, _result_row(res, candidate_key="DMR|2026-09-03|R4"))


def test_missing_dk_card_fails_safely(tmp_path: Path) -> None:
    res = _result_file(tmp_path)
    with pytest.raises(ForwardCapturePrefillError):
        prefill_forward_capture(tmp_path / "nope.md", _result_row(res))


def test_non_dk_card_rejected(tmp_path: Path) -> None:
    stray = tmp_path / "random.md"
    stray.write_text("not a dk card at all\n", encoding="utf-8")
    res = _result_file(tmp_path)
    with pytest.raises(ForwardCapturePrefillError):
        prefill_forward_capture(stray, _result_row(res))


def test_malformed_result_row_fails_safely(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    with pytest.raises(ForwardCapturePrefillError):
        prefill_forward_capture(dk, {"no_candidate_key": "x"})
    with pytest.raises(ForwardCapturePrefillError):
        prefill_forward_capture(dk, "not a mapping")


def test_result_sha_mismatch_fails_safely(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    row = _result_row(res)
    row["source_file_sha256"] = "0" * 64
    with pytest.raises(ForwardCapturePrefillError):
        prefill_forward_capture(dk, row)


def test_load_result_row_accepts_path_and_object(tmp_path: Path) -> None:
    res = _result_file(tmp_path)
    row = _result_row(res)
    as_json = tmp_path / "row.json"
    as_json.write_text(json.dumps(row), encoding="utf-8")
    assert load_result_row(as_json)["candidate_key"] == "SAR|2026-09-04|R6"
    assert load_result_row({"rows": [row]})["candidate_key"] == "SAR|2026-09-04|R6"
    assert load_result_row(row)["candidate_key"] == "SAR|2026-09-04|R6"


def test_output_is_deterministic_and_labelled(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    result = prefill_forward_capture(dk, _result_row(res))
    out = tmp_path / "out"
    first = write_prefill_artifacts(result, out)
    blobs = {k: Path(v).read_bytes() for k, v in first.items()}
    second = write_prefill_artifacts(result, out)
    for k, v in second.items():
        assert Path(v).read_bytes() == blobs[k]
    assert set(first) == {"prefill_json", "gaps_json"}
    assert "SAR_2026-09-04_R6" in Path(first["prefill_json"]).name
    for path in first.values():
        assert json.loads(Path(path).read_text(encoding="utf-8"))["draft_status"] == DRAFT_LABEL


def test_draft_cannot_pass_contract_audit(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    result = prefill_forward_capture(dk, _result_row(res))
    manifest = to_contract_manifest_draft(result)
    audit = validate_manifest_payload(manifest, manifest_path=tmp_path / "draft.json")
    assert audit["contract_pass"] is False
    assert audit["rejection_codes"]


def test_no_database_created_or_modified(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    db = tmp_path / "db" / "derbyedge.db"
    db.parent.mkdir()
    db.write_bytes(b"SQLite format 3\x00 sentinel")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = prefill_forward_capture(dk, _result_row(res))
    write_prefill_artifacts(result, tmp_path / "out")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert [p for p in tmp_path.rglob("*.db") if p != db] == []


def test_cli_exit_codes(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    row_json = tmp_path / "row.json"
    row_json.write_text(json.dumps(_result_row(res)), encoding="utf-8")
    out_dir = tmp_path / "acceptance"

    db = ROOT / "db" / "derbyedge.db"
    db_before = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None

    ok = subprocess.run(
        [sys.executable, "scripts/prefill_forward_capture_manifest.py",
         "--dk-card", str(dk), "--result-row-json", str(row_json),
         "--output-dir", str(out_dir)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert ok.returncode == 0, ok.stderr + ok.stdout
    assert "DRAFT_NOT_ELIGIBLE" in ok.stdout
    assert list(out_dir.glob("forward_capture_manifest_prefill_*.json"))

    bad = subprocess.run(
        [sys.executable, "scripts/prefill_forward_capture_manifest.py",
         "--dk-card", str(tmp_path / "missing.md"), "--result-row-json", str(row_json)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert bad.returncode == 2

    if db_before is not None:
        assert hashlib.sha256(db.read_bytes()).hexdigest() == db_before
