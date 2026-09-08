"""Acceptance and regression tests for the source-contract acquisition rule.

Covers:
  - SAR Saratoga R6: SOURCE_CONTRACT_COMPLETE, weight present, no requirements
  - DMR Del Mar R10: SOURCE_CONTRACT_INCOMPLETE, MISSING_WEIGHT sole blocker, gate
  - Determinism: evaluate_acquisition_rule is idempotent across repeated calls
  - SourceManifest JSON round-trip
  - build_manifest_from_card weight status for both cards
  - validate_manifest_schema: all strict rejection rules
  - validate_manifest_against_card: identity mismatch, weight-status lie, SHA mismatch
  - validate_manifest_against_raw_file: SHA mismatch
  - Synthetic weight-populated card passes the acquisition rule
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

POST_TS   = "2026-09-04T21:30:00+00:00"
AS_OF_TS  = "2026-09-04T18:00:00+00:00"
DMR_POST  = "2026-09-07T21:30:00+00:00"
DMR_AS_OF = "2026-09-07T18:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _find_fixture(name: str) -> Path:
    matches = list(ROOT.rglob(name))
    assert matches, f"Required fixture not found anywhere under repo root: {name}"
    return matches[0]


@pytest.fixture(scope="module")
def dmr_card():
    from src.ingest.draftkings_markdown import parse_draftkings_markdown
    path = _find_fixture("DMR_DK_Horse_R10_9-7-26.md")
    return parse_draftkings_markdown(path, as_of=datetime(2026, 9, 7, 18, 0, 0, tzinfo=timezone.utc))


@pytest.fixture(scope="module")
def sar_card():
    from src.ingest.draftkings_markdown import parse_draftkings_markdown
    path = _find_fixture("SAR_DK_Horse_R6_9-4-26.md")
    return parse_draftkings_markdown(path, as_of=datetime(2026, 9, 4, 18, 0, 0, tzinfo=timezone.utc))


@pytest.fixture(scope="module")
def sar_manifest(sar_card):
    from src.ingest.dk_source_contract import build_manifest_from_card
    return build_manifest_from_card(
        sar_card,
        scheduled_post_timestamp=POST_TS,
        source_as_of_timestamp=AS_OF_TS,
    )


# ---------------------------------------------------------------------------
# SAR acquisition rule — must pass
# ---------------------------------------------------------------------------

class TestSARSourceContractComplete:
    def test_sar_contract_label(self, sar_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule, SOURCE_CONTRACT_COMPLETE
        assert evaluate_acquisition_rule(sar_card).contract_label == SOURCE_CONTRACT_COMPLETE

    def test_sar_score_candidate_eligible(self, sar_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        assert evaluate_acquisition_rule(sar_card).score_candidate_eligible is True

    def test_sar_no_acquisition_requirements(self, sar_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        assert evaluate_acquisition_rule(sar_card).acquisition_requirements == []

    def test_sar_runners_missing_weight_is_zero(self, sar_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        assert evaluate_acquisition_rule(sar_card).runners_missing_assigned_weight == 0


# ---------------------------------------------------------------------------
# DMR acquisition rule — must fail on MISSING_WEIGHT
# ---------------------------------------------------------------------------

class TestDMRSourceContractIncomplete:
    def test_dmr_contract_label(self, dmr_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule, SOURCE_CONTRACT_INCOMPLETE
        result = evaluate_acquisition_rule(dmr_card)
        print(f"\n=== DMR Acquisition Rule Result ===")
        print(f"  label:                    {result.contract_label}")
        print(f"  eligible:                 {result.score_candidate_eligible}")
        print(f"  active runners:           {result.active_runner_count}")
        print(f"  runners missing weight:   {result.runners_missing_assigned_weight}")
        print(f"  acquisition requirements: {result.acquisition_requirements}")
        assert result.contract_label == SOURCE_CONTRACT_INCOMPLETE

    def test_dmr_not_score_candidate_eligible(self, dmr_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        assert evaluate_acquisition_rule(dmr_card).score_candidate_eligible is False

    def test_dmr_weight_is_the_only_acquisition_requirement(self, dmr_card):
        """DECISION GATE: weight is the sole blocker."""
        from src.ingest.dk_source_contract import evaluate_acquisition_rule, ACQUISITION_REQUIREMENT_WEIGHT
        result = evaluate_acquisition_rule(dmr_card)
        assert ACQUISITION_REQUIREMENT_WEIGHT in result.acquisition_requirements
        assert result.acquisition_requirements == [ACQUISITION_REQUIREMENT_WEIGHT], (
            f"Weight must be the ONLY acquisition requirement; got: {result.acquisition_requirements}"
        )

    def test_dmr_all_active_runners_missing_assigned_weight(self, dmr_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        result = evaluate_acquisition_rule(dmr_card)
        assert result.runners_missing_assigned_weight == result.active_runner_count

    def test_dmr_weight_gate_cannot_be_relaxed(self, dmr_card):
        """Permanent regression guard: weight blocker must never be silently cleared."""
        from src.ingest.dk_source_contract import evaluate_acquisition_rule, ACQUISITION_REQUIREMENT_WEIGHT
        result = evaluate_acquisition_rule(dmr_card)
        if ACQUISITION_REQUIREMENT_WEIGHT in result.acquisition_requirements:
            assert result.score_candidate_eligible is False, (
                "GATE VIOLATION: ASSIGNED_WEIGHT_BY_DK_LAYOUT is present as an acquisition "
                "requirement but score_candidate_eligible is True — the weight requirement "
                "has been silently relaxed. This is not permitted."
            )


# ---------------------------------------------------------------------------
# Determinism — evaluate_acquisition_rule must be idempotent
# ---------------------------------------------------------------------------

class TestEvaluatorDeterminism:
    def test_sar_repeated_calls_are_equal(self, sar_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        r1 = evaluate_acquisition_rule(sar_card)
        r2 = evaluate_acquisition_rule(sar_card)
        assert r1.to_dict() == r2.to_dict(), (
            "evaluate_acquisition_rule must return identical dicts across repeated calls "
            "on the same card (no wall-clock timestamps or random state)"
        )

    def test_dmr_repeated_calls_are_equal(self, dmr_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        r1 = evaluate_acquisition_rule(dmr_card)
        r2 = evaluate_acquisition_rule(dmr_card)
        assert r1.to_dict() == r2.to_dict()

    def test_result_has_no_evaluated_at_field(self, sar_card):
        """AcquisitionRuleResult must not contain a wall-clock timestamp."""
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        result = evaluate_acquisition_rule(sar_card)
        assert not hasattr(result, "evaluated_at"), (
            "AcquisitionRuleResult must not carry evaluated_at "
            "(it would make the result non-deterministic)"
        )


# ---------------------------------------------------------------------------
# Manifest builder and round-trip
# ---------------------------------------------------------------------------

class TestSourceManifestBuilder:
    def test_sar_manifest_weight_present(self, sar_manifest):
        from src.ingest.dk_source_contract import WEIGHT_STATUS_PRESENT
        assert sar_manifest.assigned_weight_status == WEIGHT_STATUS_PRESENT

    def test_sar_manifest_provider_and_tier(self, sar_manifest):
        assert sar_manifest.source_provider == "draftkings_markdown"
        assert sar_manifest.source_tier == "OPERATOR_ATTESTED"
        assert sar_manifest.field_status == "pre_race"

    def test_sar_manifest_sha_is_64_hex(self, sar_manifest):
        import re
        assert re.match(r"^[0-9a-f]{64}$", sar_manifest.raw_file_sha256)

    def test_sar_manifest_json_round_trip(self, sar_manifest):
        from src.ingest.dk_source_contract import SourceManifest
        reloaded = SourceManifest.from_json(sar_manifest.to_json())
        assert reloaded.to_dict() == sar_manifest.to_dict()

    def test_dmr_manifest_weight_absent(self, dmr_card):
        from src.ingest.dk_source_contract import build_manifest_from_card, WEIGHT_STATUS_ABSENT
        manifest = build_manifest_from_card(
            dmr_card,
            scheduled_post_timestamp=DMR_POST,
            source_as_of_timestamp=DMR_AS_OF,
        )
        assert manifest.assigned_weight_status == WEIGHT_STATUS_ABSENT

    def test_build_manifest_requires_source_as_of_explicitly(self, sar_card):
        """build_manifest_from_card must not accept a missing source_as_of_timestamp."""
        from src.ingest.dk_source_contract import build_manifest_from_card
        with pytest.raises(TypeError):
            # source_as_of_timestamp is now a required keyword argument
            build_manifest_from_card(sar_card, scheduled_post_timestamp=POST_TS)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# validate_manifest_schema — strict rejection rules
# ---------------------------------------------------------------------------

class TestValidateManifestSchema:
    """Each test mutates one field of a valid SAR manifest and confirms rejection."""

    def _good(self, sar_manifest):
        return copy.copy(sar_manifest)

    def test_valid_manifest_has_no_errors(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        assert validate_manifest_schema(sar_manifest) == []

    def test_rejects_empty_track_code(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.track_code = ""
        assert any("track_code" in e for e in validate_manifest_schema(m))

    def test_rejects_race_number_zero(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.race_number = 0
        assert any("race_number" in e for e in validate_manifest_schema(m))

    def test_rejects_race_number_negative(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.race_number = -1
        assert any("race_number" in e for e in validate_manifest_schema(m))

    def test_rejects_bad_race_date_format(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.race_date = "09/04/2026"
        assert any("race_date" in e for e in validate_manifest_schema(m))

    def test_rejects_invalid_calendar_date(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.race_date = "2026-13-01"
        assert any("race_date" in e for e in validate_manifest_schema(m))

    def test_rejects_naive_scheduled_post_timestamp(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.scheduled_post_timestamp = "2026-09-04T21:30:00"
        errors = validate_manifest_schema(m)
        assert any("scheduled_post_timestamp" in e and "offset" in e for e in errors), errors

    def test_rejects_invalid_scheduled_post_timestamp(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.scheduled_post_timestamp = "not-a-timestamp+00:00"
        assert any("scheduled_post_timestamp" in e for e in validate_manifest_schema(m))

    def test_rejects_naive_source_as_of_timestamp(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.source_as_of_timestamp = "2026-09-04T18:00:00"
        errors = validate_manifest_schema(m)
        assert any("source_as_of_timestamp" in e and "offset" in e for e in errors), errors

    def test_rejects_invalid_source_as_of_timestamp(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.source_as_of_timestamp = "UNKNOWN+00:00"
        assert any("source_as_of_timestamp" in e for e in validate_manifest_schema(m))

    def test_rejects_source_as_of_equal_to_scheduled_post(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest)
        m.source_as_of_timestamp = m.scheduled_post_timestamp  # equal = reject
        errors = validate_manifest_schema(m)
        assert any("source_as_of_timestamp" in e and "before" in e for e in errors), errors

    def test_rejects_source_as_of_after_scheduled_post(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest)
        m.source_as_of_timestamp = "2026-09-04T23:59:59+00:00"  # after post
        errors = validate_manifest_schema(m)
        assert any("source_as_of_timestamp" in e and "before" in e for e in errors), errors

    def test_rejects_non_hex_sha256_same_length(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest)
        # 64 chars but contains non-hex characters
        m.raw_file_sha256 = "z" * 64
        errors = validate_manifest_schema(m)
        assert any("sha256" in e.lower() for e in errors), errors

    def test_rejects_short_sha256(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.raw_file_sha256 = "abc123"
        assert any("sha256" in e.lower() for e in validate_manifest_schema(m))

    def test_rejects_invalid_source_provider(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.source_provider = "pdf_upload"
        assert any("source_provider" in e for e in validate_manifest_schema(m))

    def test_rejects_invalid_source_tier(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.source_tier = "INFERRED"
        assert any("source_tier" in e for e in validate_manifest_schema(m))

    def test_rejects_invalid_field_status(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.field_status = "post_race"
        assert any("field_status" in e for e in validate_manifest_schema(m))

    def test_rejects_invalid_assigned_weight_status(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_schema
        m = self._good(sar_manifest); m.assigned_weight_status = "MAYBE"
        assert any("assigned_weight_status" in e for e in validate_manifest_schema(m))


# ---------------------------------------------------------------------------
# validate_manifest_against_card — evidence checks
# ---------------------------------------------------------------------------

class TestValidateManifestAgainstCard:
    def test_valid_sar_manifest_passes_card_check(self, sar_manifest, sar_card):
        from src.ingest.dk_source_contract import validate_manifest_against_card
        assert validate_manifest_against_card(sar_manifest, sar_card) == []

    def test_detects_track_code_mismatch(self, sar_manifest, sar_card):
        from src.ingest.dk_source_contract import validate_manifest_against_card
        m = copy.copy(sar_manifest); m.track_code = "DMR"
        errors = validate_manifest_against_card(m, sar_card)
        assert any("track_code" in e for e in errors), errors

    def test_detects_race_number_mismatch(self, sar_manifest, sar_card):
        from src.ingest.dk_source_contract import validate_manifest_against_card
        m = copy.copy(sar_manifest); m.race_number = 99
        errors = validate_manifest_against_card(m, sar_card)
        assert any("race_number" in e for e in errors), errors

    def test_detects_race_date_mismatch(self, sar_manifest, sar_card):
        from src.ingest.dk_source_contract import validate_manifest_against_card
        m = copy.copy(sar_manifest); m.race_date = "2020-01-01"
        errors = validate_manifest_against_card(m, sar_card)
        assert any("race_date" in e for e in errors), errors

    def test_detects_weight_status_lie_present_when_absent(self, dmr_card):
        """Manifest says PRESENT but parsed runners are missing weight."""
        from src.ingest.dk_source_contract import (
            build_manifest_from_card, validate_manifest_against_card, WEIGHT_STATUS_PRESENT,
        )
        m = build_manifest_from_card(
            dmr_card,
            scheduled_post_timestamp=DMR_POST,
            source_as_of_timestamp=DMR_AS_OF,
        )
        m.assigned_weight_status = WEIGHT_STATUS_PRESENT  # lie
        errors = validate_manifest_against_card(m, dmr_card)
        assert any("assigned_weight_status" in e for e in errors), errors

    def test_detects_sha256_mismatch_against_card(self, sar_manifest, sar_card):
        from src.ingest.dk_source_contract import validate_manifest_against_card
        m = copy.copy(sar_manifest); m.raw_file_sha256 = "a" * 64
        errors = validate_manifest_against_card(m, sar_card)
        assert any("sha256" in e.lower() for e in errors), errors


# ---------------------------------------------------------------------------
# validate_manifest_against_raw_file — file SHA check
# ---------------------------------------------------------------------------

class TestValidateManifestAgainstRawFile:
    def test_matching_sha_passes(self, sar_manifest):
        from src.ingest.dk_source_contract import validate_manifest_against_raw_file
        raw_path = _find_fixture("SAR_DK_Horse_R6_9-4-26.md")
        assert validate_manifest_against_raw_file(sar_manifest, raw_path) == []

    def test_mismatched_sha_fails(self, sar_manifest, tmp_path):
        from src.ingest.dk_source_contract import validate_manifest_against_raw_file
        tampered = tmp_path / "tampered.md"
        tampered.write_bytes(b"tampered content")
        errors = validate_manifest_against_raw_file(sar_manifest, tampered)
        assert any("sha256" in e.lower() for e in errors), errors

    def test_missing_file_is_an_error(self, sar_manifest, tmp_path):
        from src.ingest.dk_source_contract import validate_manifest_against_raw_file
        missing = tmp_path / "does_not_exist.md"
        errors = validate_manifest_against_raw_file(sar_manifest, missing)
        assert errors


# ---------------------------------------------------------------------------
# Synthetic weight-populated card (future acceptance test)
# ---------------------------------------------------------------------------

class TestSyntheticWeightPopulatedCard:
    def test_weight_populated_card_is_eligible(self, dmr_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule, SOURCE_CONTRACT_COMPLETE
        patched = copy.deepcopy(dmr_card)
        for entry in patched.entries:
            if not entry.is_scratched:
                entry.weight = 122
        result = evaluate_acquisition_rule(patched)
        print(f"\n=== Synthetic Weight-Populated DMR Card ===")
        print(f"  label: {result.contract_label}")
        print(f"  eligible: {result.score_candidate_eligible}")
        print(f"  runners missing weight: {result.runners_missing_assigned_weight}")
        assert result.contract_label == SOURCE_CONTRACT_COMPLETE
        assert result.score_candidate_eligible is True
        assert result.runners_missing_assigned_weight == 0
