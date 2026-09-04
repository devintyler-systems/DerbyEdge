"""Card-level run-mode lookup and the hard pre-model scoring guard."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from src.ingest.firstbet_pdf import load_latest_card_audit
from src.ingest.ingestion_run import (
    IngestionRunBindingInvalid,
    card_ingestion_run_id,
    load_ingestion_run,
    validate_ingestion_run,
)
from src.ingest.run_state import DataQuality, RunMode, resolve_mode_with_feature_checks
from src.services.feature_state import FeatureVerification, verify_card_features
from src.services.odds_intake import load_live_odds_by_pp
from src.utils.ingest_trace import audit_trace_fields, trace_ingest


@dataclass(frozen=True)
class CardRunState:
    mode: RunMode
    reasons: list[str]
    audit: dict = field(default_factory=dict)
    quality: DataQuality | None = None
    feature_verification: FeatureVerification | None = None
    # Refines ``mode`` when a source-specific model policy overrides it — e.g.
    # "FEATURE_LIMITED_NO_SCORING" for a DK card whose source cannot supply the
    # feature families the standard model needs and for which no separately
    # trained proxy model exists.
    scoring_state: str | None = None

    def __post_init__(self) -> None:
        if self.audit is None:
            object.__setattr__(self, "audit", {})

    @property
    def scoring_eligible(self) -> bool:
        if self.scoring_state == "FEATURE_LIMITED_NO_SCORING":
            return False
        return self.mode in (RunMode.MODEL_READY_LIMITED, RunMode.MODEL_READY)


class ScoringBlockedError(RuntimeError):
    pass


def get_card_run_state(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    runs_root: Path | str = Path("data/runs"),
) -> CardRunState:
    """Recompute effective state from immutable ingest and current DB facts.

    When the card is bound to an immutable ingestion run, that run — looked up
    by ``ingestion_run_id`` only — is the sole source of the parse result. A
    binding that cannot be matched to a valid run fails closed; it never falls
    back to a race-key / "latest card" audit lookup.
    """
    bound_run_id = card_ingestion_run_id(conn, card_id)
    if bound_run_id:
        try:
            run = validate_ingestion_run(
                load_ingestion_run(bound_run_id, runs_root=runs_root)
            )
        except IngestionRunBindingInvalid as exc:
            trace_ingest(
                "render", card_id=card_id, ingestion_run_id=bound_run_id,
                binding_invalid=exc.detail,
            )
            return CardRunState(
                RunMode.BLOCKED,
                [f"{IngestionRunBindingInvalid.reason}: {exc.detail}"],
                {
                    "binding_invalid": True,
                    "ingestion_run_binding_invalid_reason": exc.detail,
                    "ingestion_run_id": bound_run_id,
                },
            )
        return _card_run_state_from_ingestion_run(conn, card_id, run, runs_root=runs_root)

    audit = load_latest_card_audit(card_id, runs_root=runs_root)
    return _finalize_card_run_state(conn, card_id, audit)


def _card_run_state_from_ingestion_run(
    conn: sqlite3.Connection,
    card_id: int,
    run: "Any",
    *,
    runs_root: Path | str = Path("data/runs"),
) -> CardRunState:
    """Build the card state from the exact immutable ingestion run it is bound to."""
    audit = dict(run.feature_audit)
    audit.setdefault("source_format", run.source_format)
    audit["ingestion_run_id"] = run.ingestion_run_id
    audit["ingestion_run_upload_sha256"] = run.upload_sha256
    audit["ingestion_run_parser_pipeline_version"] = run.parser_pipeline_version

    # DK cards: a failed pre-race enrichment blocks honestly — never fall back to
    # a 0/0 audit, generic 1/ST guidance, or stale DB history.
    if str(run.source_format or "").startswith("dkhorse"):
        from src.services.dk_enrichment import FAILED as _DK_FAILED, get_dk_enrichment_state
        enr = get_dk_enrichment_state(conn, card_id, run.ingestion_run_id)
        audit["dk_enrichment_state"] = enr.get("state")
        if enr.get("state") == _DK_FAILED:
            audit["enrichment_failed"] = True
            audit["enrichment_failure_reason"] = enr.get("failure_reason")
            trace_ingest(
                "render", ingestion_run_id=run.ingestion_run_id, card_id=card_id,
                source_format=run.source_format, enrichment_failed=enr.get("failure_reason"),
            )
            return CardRunState(
                RunMode.BLOCKED,
                [f"dk_enrichment_failed: {enr.get('failure_reason') or 'pre-race feature enrichment failed'}"],
                audit,
            )
    trace_ingest(
        "render",
        ingestion_run_id=run.ingestion_run_id,
        card_id=card_id,
        upload_sha256=run.upload_sha256,
        parser_pipeline_version=run.parser_pipeline_version,
        parser_selected=run.parser_selected,
        race_key=run.race_key,
        **audit_trace_fields(audit),
    )
    state = _finalize_card_run_state(conn, card_id, audit)
    if str(run.source_format or "").startswith("dkhorse"):
        state = _apply_dk_model_policy(conn, card_id, state)
    return state


def _apply_dk_model_policy(
    conn: sqlite3.Connection, card_id: int, state: CardRunState
) -> CardRunState:
    """Enforce model-family separation for a DK card and record the decision."""
    from src.services.dk_model_policy import (
        FEATURE_LIMITED_NO_SCORING, decide_dk_model_policy,
    )

    audit = dict(state.audit or {})
    decision = decide_dk_model_policy(
        conn, card_id, state.mode, state.feature_verification,
        has_live_odds=bool(state.quality and state.quality.has_live_odds),
    )
    audit.update(decision.as_audit())

    if decision.scoring_state == FEATURE_LIMITED_NO_SCORING:
        audit["scoring_state"] = FEATURE_LIMITED_NO_SCORING
        reasons = [
            "FEATURE_LIMITED_NO_SCORING: DraftKings source supplies no speed, "
            "pace, form, or trip history and no limited_history_proxy model is "
            "registered; the standard full-feature model must not score this card.",
            *[f"{cap} disabled: {why}" for cap, why in decision.disabled_capability_reasons.items()],
        ]
        return replace(
            state, mode=RunMode.BLOCKED, reasons=reasons, audit=audit,
            scoring_state=FEATURE_LIMITED_NO_SCORING,
        )
    return replace(state, audit=audit)


def _finalize_card_run_state(
    conn: sqlite3.Connection,
    card_id: int,
    audit: dict | None,
) -> CardRunState:
    expected_entries = (
        int(audit.get("active_entries", audit.get("entries_parsed")) or 0)
        if audit else _active_entry_count(conn, card_id)
    )
    verification = verify_card_features(
        conn,
        card_id,
        expected_entries=expected_entries,
        require_pp_backed_features=bool(
            audit and audit.get("source_provider") == "1stbet"
        ),
    )
    quality = data_quality_from_card(
        conn,
        card_id,
        audit=audit,
        required_model_features_complete=verification.passed,
    )
    mode, reasons = resolve_mode_with_feature_checks(
        quality, verification.core_rows
    )
    reasons = list(dict.fromkeys(reasons + list(verification.warnings)))
    # Ingest artifacts remain immutable.  Publish the post-feature pace result
    # alongside the source audit for board diagnostics without rewriting the
    # original parser audit on disk.
    if audit and verification.pace_state:
        audit = dict(audit)
        audit["post_feature_pace"] = {
            "pace_state": verification.pace_state,
            "warnings": [
                warning for warning in verification.warnings
                if warning.startswith("PACE_")
            ],
        }

    # DraftKings source: name the source correctly in limited-forecast reasons
    # and record which feature families the source cannot supply.
    if audit and str(audit.get("source_format") or "").startswith("dkhorse"):
        reasons = [
            r.replace("1/ST PDF-supported features", "DraftKings Horse PP-supported features")
             .replace("1/ST PDF-derived inputs only", "DraftKings Horse PP-derived inputs only")
            for r in reasons
        ]
        degenerate = {
            w.split(":", 1)[1].strip().split(" ", 1)[0]
            for w in verification.warnings
            if w.startswith("FEATURE_DEGENERACY_WARNING:")
        }
        # DK Horse PP rows carry no speed figures and no historical field size,
        # so speed and field-adjusted form/pace families are unavailable.
        unavailable = {"speed_figures", "fractional_pace"}
        if "form" in degenerate:
            unavailable.add("field_adjusted_form")
        audit = dict(audit)
        audit["dk_unavailable_feature_families"] = sorted(unavailable)
        audit["dk_confidence_penalty"] = "limited_source_forecast"
    return CardRunState(mode, reasons, audit, quality, verification)


def ensure_scoring_eligible(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    runs_root: Path | str = Path("data/runs"),
) -> CardRunState:
    """Raise before any model call unless the card is forecast-eligible."""
    state = get_card_run_state(conn, card_id, runs_root=runs_root)
    _sa = state.audit or {}
    trace_ingest("score", card_id=card_id, **{
        **audit_trace_fields(_sa),
        "ingestion_run_id": _sa.get("ingestion_run_id"),
        "upload_sha256": _sa.get("ingestion_run_upload_sha256"),
        "parser_pipeline_version": _sa.get("ingestion_run_parser_pipeline_version"),
        "run_mode": state.mode.value,
        "scoring_eligible": state.scoring_eligible,
    })
    if not state.scoring_eligible:
        reason = "; ".join(state.reasons) or "Data-quality gate rejected the card."
        raise ScoringBlockedError(f"SCORING BLOCKED [{state.mode.value}]: {reason}")
    return state


def data_quality_from_card(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    audit: dict[str, Any] | None = None,
    required_model_features_complete: bool = False,
) -> DataQuality:
    race = conn.execute(
        """SELECT rc.field_size, rc.card_date, rc.race_number,
                  rc.distance_yards, rc.surface, t.abbrev
           FROM race_cards rc
           JOIN tracks t ON t.track_id = rc.track_id
           WHERE rc.card_id=?""",
        (card_id,),
    ).fetchone()
    entries = conn.execute(
        "SELECT entry_id, morning_line_odds FROM entries "
        "WHERE card_id=? AND scratch_flag=0",
        (card_id,),
    ).fetchall()
    entry_ids = {int(row[0]) for row in entries}
    live_entry_ids: set[int] = set()
    if _table_exists(conn, "live_odds"):
        for quote in load_live_odds_by_pp(conn, card_id).values():
            try:
                entry_id = int(quote["entry_id"])
                decimal_odds = float(quote["decimal_odds"])
            except (KeyError, TypeError, ValueError):
                continue
            if entry_id in entry_ids and decimal_odds > 1.0:
                live_entry_ids.add(entry_id)

    if audit:
        parsed = int(audit.get("active_entries", audit.get("entries_parsed")) or 0)
        entries_with_pp = int(audit.get("entries_with_pp_history") or 0)
        match_rate = float(audit.get("starter_match_rate") or 0.0)
        declared = audit.get("field_size_declared")
        scratches = int(audit.get("scratches") or 0)
        metadata_complete = bool(audit.get("race_metadata_complete", race is not None))
        has_morning_lines = bool(audit.get("has_morning_lines", entries))
        blocking_errors = list(audit.get("block_reasons") or audit.get("blocking_errors") or [])
        source_format = audit.get("source_format")
        field_reconciliation = audit.get("field_reconciliation_status", "unknown")
        identity_rate = audit.get("identity_resolution_rate")
        pp_link_rate = audit.get("starter_pp_link_rate")
        exp_coverage = audit.get("experienced_field_pp_coverage")
        resolved_no_hist = audit.get("resolved_no_history_count")
        unresolved_id = audit.get("unresolved_identity_count")
        unresolved_hist = audit.get("unresolved_history_count")
    else:
        pp_entry_ids: set[int] = set()
        for table in ("firstbet_pp_starts", "horse_starts"):
            if not _table_exists(conn, table):
                continue
            rows = conn.execute(
                f"SELECT DISTINCT entry_id FROM {table} WHERE card_id=?", (card_id,)
            ).fetchall()
            pp_entry_ids.update(int(row[0]) for row in rows if int(row[0]) in entry_ids)
        parsed = len(entries)
        entries_with_pp = len(pp_entry_ids)
        match_rate = entries_with_pp / parsed if parsed else 0.0
        declared = int(race[0]) if race and race[0] else None
        metadata_complete = bool(
            race and race[1] and race[2] is not None and race[3] and race[4] and race[5]
        )
        has_morning_lines = bool(entries) and all(row[1] is not None for row in entries)
        blocking_errors = [] if race else [f"No race card found for card_id={card_id}."]
        scratches = 0
        source_format = None
        field_reconciliation = "unknown"
        identity_rate = None
        pp_link_rate = None
        exp_coverage = None
        resolved_no_hist = None
        unresolved_id = None
        unresolved_hist = None
    return DataQuality(
        entries_parsed=parsed,
        field_size_declared=int(declared) if declared else None,
        entries_with_pp_history=entries_with_pp,
        starter_match_rate=match_rate,
        race_metadata_complete=metadata_complete,
        has_morning_lines=has_morning_lines,
        has_live_odds=(
            bool(parsed)
            and len(entries) == parsed
            and live_entry_ids == entry_ids
        ),
        required_model_features_complete=required_model_features_complete,
        blocking_errors=blocking_errors,
        entries_scratched=scratches,
        active_entry_count=parsed,
        field_reconciliation_status=field_reconciliation,
        source_format=source_format,
        identity_resolution_rate=identity_rate,
        starter_pp_link_rate=pp_link_rate,
        experienced_field_pp_coverage=exp_coverage,
        resolved_no_history_count=resolved_no_hist,
        unresolved_identity_count=unresolved_id,
        unresolved_history_count=unresolved_hist,
    )


def quality_with_verified_features(
    quality: DataQuality,
    verification: FeatureVerification,
) -> DataQuality:
    """Apply verification from the exact post-construction feature frame."""
    return replace(
        quality, required_model_features_complete=verification.passed
    )


def _active_entry_count(conn: sqlite3.Connection, card_id: int) -> int:
    if not _table_exists(conn, "entries"):
        return 0
    return int(conn.execute(
        "SELECT COUNT(*) FROM entries WHERE card_id=? AND scratch_flag=0", (card_id,)
    ).fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None
