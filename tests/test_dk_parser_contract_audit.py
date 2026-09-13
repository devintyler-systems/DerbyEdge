"""Read-only contract-audit regression for hardened DK markdown parsing.

The audit only measures parsed-input evidence coverage and the existing
read-only validator's verdict.  It never persists, never opens SQLite, and never
changes readiness policy.  Every row is ``AUDIT_ONLY_READ_ONLY``.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.services.dk_parser_contract_audit import (
    AUDIT_LABEL,
    IDENTITY_FIELDS,
    audit_fixture,
    audit_fixtures,
    write_audit_artifacts,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "draftkings_racedata_pdfs" / "fixtures"
DMR = FIXTURES / "DMR_DK_Horse_R10_9-7-26.md"
SAR = FIXTURES / "SAR_DK_Horse_R6_9-4-26.md"


def test_del_mar_audit_has_one_card_and_a_row_per_retained_runner() -> None:
    card = audit_fixture(DMR)
    assert card.fixture_name == "DMR_DK_Horse_R10_9-7-26.md"
    assert card.track_name == "Del Mar"
    assert card.race_number == 10
    assert card.detected_runner_count == 13
    assert len(card.runners) == card.detected_runner_count
    assert card.status == AUDIT_LABEL
    assert all(r.status == AUDIT_LABEL for r in card.runners)


def test_saratoga_audit_has_one_card_and_runner_rows() -> None:
    card = audit_fixture(SAR)
    assert card.track_name == "Saratoga"
    assert card.race_number == 6
    assert card.detected_runner_count == 10
    assert len(card.runners) == 10


def test_active_and_scratched_counts_are_deterministic() -> None:
    a = audit_fixture(DMR)
    b = audit_fixture(DMR)
    assert a.scratched_runner_count == b.scratched_runner_count == 1
    assert a.active_runner_count == b.active_runner_count == 12
    assert a.detected_runner_count == a.active_runner_count + a.scratched_runner_count
    scratched = [r for r in a.runners if r.scratched_flag]
    assert [r.horse_name for r in scratched] == ["Hondo Crouch"]


def test_identity_completeness_ratio_is_bounded_and_counted() -> None:
    card = audit_fixture(DMR)
    assert len(IDENTITY_FIELDS) == 11
    for r in card.runners:
        assert r.identity_fields_expected_count == 11
        assert 0 <= r.identity_fields_present_count <= 11
        assert 0.0 <= r.identity_completeness_ratio <= 1.0
        assert abs(r.identity_completeness_ratio - r.identity_fields_present_count / 11) < 1e-6
    first = next(r for r in card.runners if r.horse_name == "Its a Cinch")
    assert first.identity_completeness_ratio == 1.0


def test_history_coverage_percentages_are_deterministic() -> None:
    card = audit_fixture(DMR)
    assert card.runners_with_all_races_count == 12
    assert card.runners_with_workouts_count == 12
    assert card.pct_with_all_races_section == round(12 / 13, 6)
    assert card.pct_with_workouts_section == round(12 / 13, 6)
    assert card.retained_without_history_count == 1


def test_missing_history_or_workouts_does_not_drop_runner_from_audit() -> None:
    card = audit_fixture(DMR)
    scratched = next(r for r in card.runners if r.horse_name == "Hondo Crouch")
    assert scratched.has_all_races_section is False
    assert scratched.has_workouts_section is False
    assert scratched.runner_retained_without_history is True
    # Still present in the audit output with identity fields measured.
    assert scratched.identity_fields_present_count >= 6


def test_validator_is_invoked_read_only_and_reported() -> None:
    card = audit_fixture(SAR)
    assert card.validator_invoked is True
    assert isinstance(card.scoring_ready_under_current_rules, bool)
    assert card.scoring_ready_under_current_rules is True  # SAR fixture validates today
    assert card.validator_missing_fields_count == len(card.validator_missing_fields)
    assert card.feature_vector_audit_status == "NOT_AVAILABLE_READ_ONLY"


def test_report_and_artifacts_are_written_and_deterministic(tmp_path: Path) -> None:
    audit = audit_fixtures([DMR, SAR])
    out = tmp_path / "acc"
    first = write_audit_artifacts(audit, out)
    blobs = {k: Path(v).read_bytes() for k, v in first.items()}
    second = write_audit_artifacts(audit, out)
    for k, v in second.items():
        assert Path(v).read_bytes() == blobs[k]
    assert set(first) == {"cards_csv", "runners_csv", "summary_json", "report_md"}

    report = Path(first["report_md"]).read_text(encoding="utf-8")
    assert "AUDIT_ONLY_READ_ONLY" in report
    assert "readiness" in report.lower()
    # Required card-level table header.
    assert (
        "| fixture | track | race | detected | active | scratched | retained_no_history "
        "| pct_all_races | pct_workouts | parser_warnings | scoring_ready_current_rules |"
    ) in report
    assert "DMR_DK_Horse_R10_9-7-26.md" in report and "SAR_DK_Horse_R6_9-4-26.md" in report

    summary = json.loads(Path(first["summary_json"]).read_text(encoding="utf-8"))
    assert summary["status"] == "AUDIT_ONLY_READ_ONLY"
    assert summary["card_count"] == 2
    assert summary["totals"]["detected_runner_count"] == 23


def test_audit_creates_no_database(tmp_path: Path) -> None:
    db = tmp_path / "db" / "derbyedge.db"
    db.parent.mkdir()
    db.write_bytes(b"SQLite format 3\x00 sentinel")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    audit = audit_fixtures([DMR, SAR])
    write_audit_artifacts(audit, tmp_path / "out")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert [p for p in tmp_path.rglob("*.db") if p != db] == []
    assert list(tmp_path.rglob("*.sqlite*")) == []


def test_missing_fixture_raises() -> None:
    with pytest.raises(FileNotFoundError):
        audit_fixture(FIXTURES / "does_not_exist.md")


def test_cli_exit_codes_and_db_untouched(tmp_path: Path) -> None:
    db = ROOT / "db" / "derbyedge.db"
    db_before = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
    out = tmp_path / "acceptance"

    ok = subprocess.run(
        [sys.executable, "scripts/audit_dk_parser_contract.py",
         "--fixture", str(DMR), "--fixture", str(SAR), "--output-dir", str(out)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert ok.returncode == 0, ok.stderr + ok.stdout
    assert "AUDIT_ONLY_READ_ONLY" in ok.stdout
    assert (out / "dk_parser_contract_audit_report.md").is_file()
    assert (out / "dk_parser_contract_audit_cards.csv").is_file()

    bad = subprocess.run(
        [sys.executable, "scripts/audit_dk_parser_contract.py", "--fixture", str(tmp_path / "nope.md")],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert bad.returncode == 2

    if db_before is not None:
        assert hashlib.sha256(db.read_bytes()).hexdigest() == db_before
