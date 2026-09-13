"""Read-only forward-capture evidence ledger.

The ledger records immutable operator capture facts for *future* paired races.
It is an audit aid only: it never ingests, never opens SQLite, and no entry (or
bundle) it produces can satisfy the historical snapshot contract.  Every record
is labelled ``DRAFT_NOT_ELIGIBLE`` / ``created_from = EVIDENCE_LEDGER_ONLY``.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.services.forward_capture_evidence import (
    CREATED_FROM,
    DRAFT_LABEL,
    ForwardCaptureEvidenceError,
    build_bundle_summaries,
    build_evidence_entry,
    load_entry_documents,
    write_evidence_artifacts,
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
#
ODDS
Runner
1
20
Some Horse
L122
Jock
Train
"""


def _dk_card(tmp_path: Path, name: str = "SAR_DK_Horse_R6_9-4-26.md") -> Path:
    path = tmp_path / name
    path.write_text(_DK_MARKDOWN, encoding="utf-8")
    return path


def _result_file(tmp_path: Path, name: str = "eqb_SAR_2026-09-04_fullcard.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\nfake equibase chart bytes\n")
    return path


def _as_of() -> dict:
    return {
        "source_as_of_timestamp": "2026-09-04T12:40:00-04:00",
        "source_as_of_timezone": "America/New_York",
        "source_as_of_tier": "OPERATOR_ATTESTED",
        "source_as_of_provenance": "OPERATOR_ATTESTED",
        "operator_id": "op-jane",
        "raw_bytes_preserved": True,
    }


def test_pre_race_and_result_entries_accepted(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    pre = build_evidence_entry({"event_type": "PRE_RACE_CAPTURE", "artifact_path": str(dk), **_as_of()})
    result = build_evidence_entry({
        "event_type": "RESULT_CAPTURE", "artifact_path": str(res),
        "candidate_key": "SAR|2026-09-04|R6", **_as_of(),
    })
    for entry in (pre, result):
        assert entry.draft_status == DRAFT_LABEL
        assert entry.created_from == CREATED_FROM
    assert pre.event_type == "PRE_RACE_CAPTURE"
    assert result.event_type == "RESULT_CAPTURE"


def test_manual_note_entry_accepted(tmp_path: Path) -> None:
    entry = build_evidence_entry({
        "event_type": "MANUAL_NOTE", "operator_id": "op-jane",
        "note": "Captured DK card 20 min before post from web app; screenshot in notes/.",
        "candidate_key": "SAR|2026-09-04|R6",
    })
    assert entry.event_type == "MANUAL_NOTE"
    assert entry.artifact_path is None
    assert entry.note.startswith("Captured DK card")


def test_deterministic_identity_enrichment_from_dk_path(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    entry = build_evidence_entry({"event_type": "PRE_RACE_CAPTURE", "artifact_path": str(dk), **_as_of()})
    assert entry.candidate_key == "SAR|2026-09-04|R6"
    assert entry.target_track_code_or_name == "SAR"
    assert entry.target_race_date == "2026-09-04"
    assert entry.target_race_date_provenance == "FILENAME_DERIVED"
    assert entry.target_race_number == 6
    assert entry.source_provider_candidate == "draftkings_markdown"
    assert entry.observed_wall_clock_post_display == "1:03 PM"
    assert entry.artifact_sha256 == hashlib.sha256(dk.read_bytes()).hexdigest()
    assert entry.artifact_sha256_source == "STREAMED_LOCAL_FILE"


def test_sha256_is_streamed_read_only(tmp_path: Path) -> None:
    res = _result_file(tmp_path)
    before = res.read_bytes()
    entry = build_evidence_entry({
        "event_type": "RESULT_CAPTURE", "artifact_path": str(res),
        "candidate_key": "SAR|2026-09-04|R6", **_as_of(),
    })
    assert res.read_bytes() == before
    assert entry.artifact_sha256 == hashlib.sha256(before).hexdigest()


def test_malformed_entries_fail_safely(tmp_path: Path) -> None:
    with pytest.raises(ForwardCaptureEvidenceError):
        build_evidence_entry({"event_type": "NOT_A_REAL_EVENT"})
    with pytest.raises(ForwardCaptureEvidenceError):
        build_evidence_entry({"event_type": "MANUAL_NOTE", "operator_id": "op"})  # no note
    with pytest.raises(ForwardCaptureEvidenceError):
        build_evidence_entry({"event_type": "PRE_RACE_CAPTURE", **_as_of()})  # no artifact_path
    with pytest.raises(ForwardCaptureEvidenceError):
        build_evidence_entry("not a mapping")
    with pytest.raises(ForwardCaptureEvidenceError):
        build_evidence_entry({"event_type": "PRE_RACE_CAPTURE", "artifact_path": "x", "candidate_key": "bad-key"})


def test_candidate_key_mismatch_with_enrichment_fails(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    with pytest.raises(ForwardCaptureEvidenceError):
        build_evidence_entry({
            "event_type": "PRE_RACE_CAPTURE", "artifact_path": str(dk),
            "candidate_key": "DMR|2026-09-03|R4", **_as_of(),
        })


def test_bundle_summary_groups_by_candidate_key(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    entries = [
        build_evidence_entry({"event_type": "PRE_RACE_CAPTURE", "artifact_path": str(dk), **_as_of()}),
        build_evidence_entry({
            "event_type": "RESULT_CAPTURE", "artifact_path": str(res),
            "candidate_key": "SAR|2026-09-04|R6", **_as_of(),
        }),
        build_evidence_entry({
            "event_type": "PRE_RACE_CAPTURE", "artifact_path": str(_dk_card(tmp_path, "DMR_DK_Horse_R4_9-3-26.md")),
            **_as_of(),
        }),
    ]
    summaries = {s.candidate_key: s for s in build_bundle_summaries(entries)}
    sar = summaries["SAR|2026-09-04|R6"]
    assert sar.pre_race_capture_present and sar.result_capture_present
    assert sar.artifact_count == 2
    assert sar.status == DRAFT_LABEL
    dmr = summaries["DMR|2026-09-03|R4"]
    assert dmr.pre_race_capture_present and not dmr.result_capture_present
    assert "result_capture" in dmr.unresolved_fields


def test_unknown_candidate_key_allowed_but_unresolved(tmp_path: Path) -> None:
    note = build_evidence_entry({
        "event_type": "MANUAL_NOTE", "operator_id": "op", "note": "target race not chosen yet",
    })
    assert note.candidate_key is None
    assert "candidate_key" in note.unresolved_fields
    summaries = build_bundle_summaries([note])
    assert len(summaries) == 1
    assert summaries[0].candidate_key is None
    assert summaries[0].status == DRAFT_LABEL


def test_outputs_deterministic_and_labelled(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    e1 = build_evidence_entry({"event_type": "PRE_RACE_CAPTURE", "artifact_path": str(dk), **_as_of()})
    e2 = build_evidence_entry({
        "event_type": "RESULT_CAPTURE", "artifact_path": str(res),
        "candidate_key": "SAR|2026-09-04|R6", **_as_of(),
    })
    out = tmp_path / "out"
    forward = write_evidence_artifacts([e1, e2], build_bundle_summaries([e1, e2]), out)
    reverse = write_evidence_artifacts([e2, e1], build_bundle_summaries([e2, e1]), out)
    for key in forward:
        assert Path(forward[key]).read_bytes() == Path(reverse[key]).read_bytes()
    assert set(forward) == {"evidence_log", "bundle_summary"}
    for path in forward.values():
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        assert doc["status"] == DRAFT_LABEL
        assert doc["created_from"] == CREATED_FROM


def test_load_entry_documents_accepts_paths_and_lists(tmp_path: Path) -> None:
    one = tmp_path / "a.json"
    one.write_text(json.dumps({"event_type": "MANUAL_NOTE", "operator_id": "o", "note": "n"}), encoding="utf-8")
    many = tmp_path / "b.json"
    many.write_text(json.dumps([{"event_type": "MANUAL_NOTE", "operator_id": "o", "note": "n2"}]), encoding="utf-8")
    docs = load_entry_documents([one, many])
    assert len(docs) == 2
    with pytest.raises(ForwardCaptureEvidenceError):
        load_entry_documents([tmp_path / "missing.json"])


def test_no_database_created_or_modified(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    db = tmp_path / "db" / "derbyedge.db"
    db.parent.mkdir()
    db.write_bytes(b"SQLite format 3\x00 sentinel")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    entry = build_evidence_entry({"event_type": "PRE_RACE_CAPTURE", "artifact_path": str(dk), **_as_of()})
    write_evidence_artifacts([entry], build_bundle_summaries([entry]), tmp_path / "out")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert [p for p in tmp_path.rglob("*.db") if p != db] == []


def test_cli_exit_codes(tmp_path: Path) -> None:
    dk = _dk_card(tmp_path)
    res = _result_file(tmp_path)
    a = tmp_path / "pre.json"
    a.write_text(json.dumps({"event_type": "PRE_RACE_CAPTURE", "artifact_path": str(dk), **_as_of()}), encoding="utf-8")
    b = tmp_path / "result.json"
    b.write_text(json.dumps({
        "event_type": "RESULT_CAPTURE", "artifact_path": str(res),
        "candidate_key": "SAR|2026-09-04|R6", **_as_of(),
    }), encoding="utf-8")
    out_dir = tmp_path / "acceptance"

    db = ROOT / "db" / "derbyedge.db"
    db_before = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None

    ok = subprocess.run(
        [sys.executable, "scripts/record_forward_capture_evidence.py",
         "--entry-json", str(a), "--entry-json", str(b), "--output-dir", str(out_dir)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert ok.returncode == 0, ok.stderr + ok.stdout
    assert "DRAFT_NOT_ELIGIBLE" in ok.stdout
    assert (out_dir / "forward_capture_evidence_log.json").is_file()
    assert (out_dir / "forward_capture_evidence_bundle_summary.json").is_file()

    bad = subprocess.run(
        [sys.executable, "scripts/record_forward_capture_evidence.py", "--entry-json", str(tmp_path / "nope.json")],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert bad.returncode == 2

    if db_before is not None:
        assert hashlib.sha256(db.read_bytes()).hexdigest() == db_before
