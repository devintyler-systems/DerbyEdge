"""Acceptance tests for the source-contract acquisition rule.

These tests are fixture-driven and TDD-only.  They verify:

1. SAR Saratoga R6 card satisfies the acquisition rule (SOURCE_CONTRACT_COMPLETE).
2. DMR Del Mar R10 card fails the acquisition rule (SOURCE_CONTRACT_INCOMPLETE)
   with ASSIGNED_WEIGHT_BY_DK_LAYOUT as the only acquisition requirement.
3. The decision gate: DMR must not be score_candidate_eligible even after rule
   evaluation — relaxing weight does not remove this blocker.
4. SourceManifest round-trips through JSON without data loss.
5. build_manifest_from_card emits correct weight status for both cards.
6. validate_manifest catches malformed manifests.
7. A future card with weight populated passes the acquisition rule.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixture discovery
# ---------------------------------------------------------------------------

def _find_fixture(name: str) -> Path:
    matches = list(ROOT.rglob(name))
    assert matches, f"Required fixture not found: {name}"
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


# ---------------------------------------------------------------------------
# SAR acquisition rule — must pass
# ---------------------------------------------------------------------------

class TestSARSourceContractComplete:
    def test_sar_contract_label(self, sar_card):
        from src.ingest.dk_source_contract import (
            evaluate_acquisition_rule,
            SOURCE_CONTRACT_COMPLETE,
        )
        result = evaluate_acquisition_rule(sar_card)
        assert result.contract_label == SOURCE_CONTRACT_COMPLETE, (
            f"SAR should be SOURCE_CONTRACT_COMPLETE; blockers: {result.runner_field_gaps}"
        )

    def test_sar_score_candidate_eligible(self, sar_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        result = evaluate_acquisition_rule(sar_card)
        assert result.score_candidate_eligible is True

    def test_sar_no_acquisition_requirements(self, sar_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        result = evaluate_acquisition_rule(sar_card)
        assert result.acquisition_requirements == [], (
            f"SAR should have no acquisition requirements; got: {result.acquisition_requirements}"
        )

    def test_sar_runners_missing_weight_is_zero(self, sar_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        result = evaluate_acquisition_rule(sar_card)
        assert result.runners_missing_weight == 0


# ---------------------------------------------------------------------------
# DMR acquisition rule — must fail on MISSING_WEIGHT
# ---------------------------------------------------------------------------

class TestDMRSourceContractIncomplete:
    def test_dmr_contract_label(self, dmr_card):
        from src.ingest.dk_source_contract import (
            evaluate_acquisition_rule,
            SOURCE_CONTRACT_INCOMPLETE,
        )
        result = evaluate_acquisition_rule(dmr_card)
        print(f"\n=== DMR Acquisition Rule Result ===")
        print(f"  label: {result.contract_label}")
        print(f"  eligible: {result.score_candidate_eligible}")
        print(f"  active runners: {result.active_runner_count}")
        print(f"  runners missing weight: {result.runners_missing_weight}")
        print(f"  acquisition requirements: {result.acquisition_requirements}")
        assert result.contract_label == SOURCE_CONTRACT_INCOMPLETE

    def test_dmr_not_score_candidate_eligible(self, dmr_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        result = evaluate_acquisition_rule(dmr_card)
        assert result.score_candidate_eligible is False

    def test_dmr_weight_is_the_only_acquisition_requirement(self, dmr_card):
        """DECISION GATE: weight is the sole blocker; no other requirement must be injected."""
        from src.ingest.dk_source_contract import (
            evaluate_acquisition_rule,
            ACQUISITION_REQUIREMENT_WEIGHT,
        )
        result = evaluate_acquisition_rule(dmr_card)
        assert ACQUISITION_REQUIREMENT_WEIGHT in result.acquisition_requirements, (
            "ASSIGNED_WEIGHT_BY_DK_LAYOUT must be listed as an acquisition requirement"
        )
        assert result.acquisition_requirements == [ACQUISITION_REQUIREMENT_WEIGHT], (
            f"Weight must be the ONLY acquisition requirement; got: {result.acquisition_requirements}"
        )

    def test_dmr_all_active_runners_missing_weight(self, dmr_card):
        from src.ingest.dk_source_contract import evaluate_acquisition_rule
        result = evaluate_acquisition_rule(dmr_card)
        assert result.runners_missing_weight == result.active_runner_count, (
            f"All {result.active_runner_count} active DMR runners should be missing weight; "
            f"{result.runners_missing_weight} reported missing"
        )

    def test_dmr_weight_gate_cannot_be_relaxed(self, dmr_card):
        """Permanent regression guard: weight blocker must never be silently cleared."""
        from src.ingest.dk_source_contract import (
            evaluate_acquisition_rule,
            ACQUISITION_REQUIREMENT_WEIGHT,
        )
        result = evaluate_acquisition_rule(dmr_card)
        # If weight blockers exist, score_candidate_eligible must be False.
        if ACQUISITION_REQUIREMENT_WEIGHT in result.acquisition_requirements:
            assert result.score_candidate_eligible is False, (
                "GATE VIOLATION: weight acquisition requirement is present but "
                "score_candidate_eligible is True — the weight requirement has been "
                "silently relaxed. This is not permitted."
            )


# ---------------------------------------------------------------------------
# Source manifest round-trip
# ---------------------------------------------------------------------------

class TestSourceManifestSchema:
    POST_TS = "2026-09-04T21:30:00+00:00"

    def test_sar_manifest_build_round_trip(self, sar_card):
        from src.ingest.dk_source_contract import (
            build_manifest_from_card,
            WEIGHT_STATUS_PRESENT,
        )
        manifest = build_manifest_from_card(sar_card, scheduled_post_timestamp=self.POST_TS)
        assert manifest.assigned_weight_status == WEIGHT_STATUS_PRESENT
        assert manifest.source_provider == "draftkings_markdown"
        assert manifest.field_status == "pre_race"
        assert manifest.source_tier == "OPERATOR_ATTESTED"
        assert len(manifest.raw_file_sha256) == 64
        # Round-trip through JSON
        from src.ingest.dk_source_contract import SourceManifest
        reloaded = SourceManifest.from_json(manifest.to_json())
        assert reloaded.to_dict() == manifest.to_dict()

    def test_dmr_manifest_weight_absent(self, dmr_card):
        from src.ingest.dk_source_contract import (
            build_manifest_from_card,
            WEIGHT_STATUS_ABSENT,
        )
        manifest = build_manifest_from_card(
            dmr_card,
            scheduled_post_timestamp="2026-09-07T21:30:00+00:00",
        )
        assert manifest.assigned_weight_status == WEIGHT_STATUS_ABSENT, (
            f"DMR manifest should report ABSENT weight; got {manifest.assigned_weight_status}"
        )

    def test_validate_manifest_catches_bad_sha(self, sar_card):
        from src.ingest.dk_source_contract import (
            build_manifest_from_card,
            validate_manifest,
        )
        manifest = build_manifest_from_card(sar_card, scheduled_post_timestamp=self.POST_TS)
        bad = copy.copy(manifest)
        bad.raw_file_sha256 = "tooshort"
        errors = validate_manifest(bad)
        assert any("sha256" in e.lower() for e in errors)

    def test_validate_manifest_catches_missing_track(self, sar_card):
        from src.ingest.dk_source_contract import (
            build_manifest_from_card,
            validate_manifest,
        )
        manifest = build_manifest_from_card(sar_card, scheduled_post_timestamp=self.POST_TS)
        bad = copy.copy(manifest)
        bad.track_code = ""
        errors = validate_manifest(bad)
        assert any("track_code" in e for e in errors)

    def test_validate_manifest_catches_post_race_field_status(self, sar_card):
        from src.ingest.dk_source_contract import (
            build_manifest_from_card,
            validate_manifest,
        )
        manifest = build_manifest_from_card(sar_card, scheduled_post_timestamp=self.POST_TS)
        bad = copy.copy(manifest)
        bad.field_status = "post_race"
        errors = validate_manifest(bad)
        assert any("field_status" in e for e in errors)


# ---------------------------------------------------------------------------
# Synthetic weight-populated card (future card acceptance test)
# ---------------------------------------------------------------------------

class TestSyntheticWeightPopulatedCard:
    """Prove that a DMR-style card with weight populated passes the acquisition rule.

    This test constructs a minimal synthetic card by deep-copying the DMR fixture
    and patching weight onto every active runner.  It verifies that no code change
    is required when an actual weight-bearing source artifact is available.
    """

    def test_weight_populated_card_is_eligible(self, dmr_card):
        from src.ingest.dk_source_contract import (
            evaluate_acquisition_rule,
            SOURCE_CONTRACT_COMPLETE,
        )
        patched = copy.deepcopy(dmr_card)
        for entry in patched.entries:
            if not entry.is_scratched:
                entry.weight = 122  # representative assigned weight
        result = evaluate_acquisition_rule(patched)
        print(f"\n=== Synthetic Weight-Populated DMR Card ===")
        print(f"  label: {result.contract_label}")
        print(f"  eligible: {result.score_candidate_eligible}")
        print(f"  runners missing weight: {result.runners_missing_weight}")
        assert result.contract_label == SOURCE_CONTRACT_COMPLETE, (
            f"Weight-populated DMR card should be SOURCE_CONTRACT_COMPLETE; "
            f"remaining gaps: {result.runner_field_gaps}"
        )
        assert result.score_candidate_eligible is True
        assert result.runners_missing_weight == 0
