"""Regression coverage for null/legacy card-audit rendering state."""
from src.app.board_formatting import (
    DK_BLOCKED_GUIDANCE,
    GENERIC_BLOCKED_GUIDANCE,
    blocked_state_guidance,
)
from src.ingest.run_state import RunMode
from src.services.run_mode import CardRunState


def test_card_run_state_audit_defaults_to_empty_dict():
    state = CardRunState(RunMode.BLOCKED, ["No active race."])
    assert state.audit == {}
    assert isinstance(state.audit, dict)


def test_blocked_guidance_is_safe_for_null_or_missing_audit():
    state = CardRunState(RunMode.BLOCKED, ["Blocked"], None)
    assert state.audit == {}
    assert blocked_state_guidance(state.audit) == GENERIC_BLOCKED_GUIDANCE

    class LegacyState:
        pass

    legacy = LegacyState()
    audit = getattr(legacy, "audit", None) or {}
    assert blocked_state_guidance(audit) == GENERIC_BLOCKED_GUIDANCE
    assert DK_BLOCKED_GUIDANCE not in blocked_state_guidance(audit)


def test_blocked_guidance_uses_dk_message_for_valid_dk_audit():
    audit = {
        "source_format": "dkhorse_program_pdf",
        "field_reconciliation_status": "unexplained",
    }
    assert blocked_state_guidance(audit) == DK_BLOCKED_GUIDANCE
    assert GENERIC_BLOCKED_GUIDANCE not in blocked_state_guidance(audit)
