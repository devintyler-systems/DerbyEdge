"""Model-family separation policy for DraftKings-sourced cards.

A DK card whose source cannot supply speed, pace, form, and trip history must
never be scored by the standard full-feature dirt/turf/route model with generic
missing-value imputation. Exactly one of two outcomes applies:

* a separately trained ``limited_history_proxy`` model family exists (matching
  feature-availability mask, evaluated + calibrated separately) — score with it,
  wagering still disabled; or
* no such artifact exists — final state becomes ``FEATURE_LIMITED_NO_SCORING``:
  entries and diagnostics may be shown, but no win probabilities, fair odds,
  rankings, or betting outputs are emitted.

This module only *selects* and *records*; it never changes parser rules,
identity gates, ingestion-run binding, or scoring thresholds.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

LIMITED_HISTORY_PROXY_FAMILY = "limited_history_proxy"
FEATURE_LIMITED_NO_SCORING = "FEATURE_LIMITED_NO_SCORING"
DK_MODEL_FEATURE_SCHEMA_VERSION = "dk_enrich_v1"

# Feature families the DK Horse program PDF source structurally cannot supply:
# no speed/Beyer figures and no per-start trip/running-line data are in the PP
# rows, and (no historical field size) no field-adjusted pace.
_DK_STRUCTURALLY_ABSENT = ("speed", "trip")


@dataclass(frozen=True)
class ModelPolicyDecision:
    scoring_state: str | None            # None => keep the resolved run mode
    model_family_selected: str | None
    model_version: str | None
    model_feature_schema_version: str | None
    feature_availability_mask: dict[str, bool]
    calibration_version: str | None
    confidence_tier: str
    scoring_eligibility: bool
    betting_eligibility: bool
    disabled_capability_reasons: dict[str, str]

    def as_audit(self) -> dict[str, Any]:
        return {
            "model_family_selected": self.model_family_selected,
            "model_version": self.model_version,
            "model_feature_schema_version": self.model_feature_schema_version,
            "feature_availability_mask": dict(self.feature_availability_mask),
            "calibration_version": self.calibration_version,
            "confidence_tier": self.confidence_tier,
            "scoring_eligibility": self.scoring_eligibility,
            "betting_eligibility": self.betting_eligibility,
            "disabled_capability_reasons": dict(self.disabled_capability_reasons),
        }


def _degenerate_core(verification) -> set[str]:
    out: set[str] = set()
    for w in getattr(verification, "warnings", ()) or ():
        if w.startswith("FEATURE_DEGENERACY_WARNING:"):
            out.add(w.split(":", 1)[1].strip().split(" ", 1)[0])
    return out


def feature_availability_mask(verification) -> dict[str, bool]:
    """Per-family availability for a DK card, from the verified feature frame."""
    degenerate = _degenerate_core(verification)
    pace_state = getattr(verification, "pace_state", None)
    return {
        "speed_feature_available": False,   # no speed/Beyer figures in DK PP rows
        "trip_feature_available": False,    # no per-start trip / running line
        "pace_feature_available": pace_state not in (None, "PACE_UNAVAILABLE")
        and "pace_fit" not in degenerate,
        "form_feature_available": "form" not in degenerate,
        "surface_distance_feature_available": "surface_distance_fit" not in degenerate,
    }


def proxy_model_available(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the registered limited-history proxy model, or None."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_registry)")}
    except sqlite3.Error:
        return None
    if "model_family" not in cols:
        return None
    want = ["model_name", "version"]
    opt = [c for c in ("feature_schema_version", "calibration_artifact_path", "artifact_path") if c in cols]
    # A proxy model may be registered under its own family or (until the
    # model_family CHECK is widened) under an allowed family with the proxy name.
    row = conn.execute(
        f"SELECT {', '.join(want + opt)} FROM model_registry "
        f"WHERE model_family=? OR model_name LIKE ? "
        f"ORDER BY version DESC LIMIT 1",
        (LIMITED_HISTORY_PROXY_FAMILY, f"{LIMITED_HISTORY_PROXY_FAMILY}%"),
    ).fetchone()
    if not row:
        return None
    rec = dict(zip(want + opt, row))
    # A proxy family with no calibration artifact is not a usable proxy model.
    if "calibration_artifact_path" in rec and not rec.get("calibration_artifact_path"):
        return None
    return rec


def decide_dk_model_policy(
    conn: sqlite3.Connection,
    card_id: int,
    mode,
    verification,
    *,
    has_live_odds: bool = False,
) -> ModelPolicyDecision:
    """Decide the DK card's model family / scoring & betting eligibility."""
    from src.ingest.run_state import RunMode
    from src.services.feature_state import model_config_for_card

    mask = feature_availability_mask(verification)
    core_signal = any(
        mask[k] for k in (
            "pace_feature_available", "form_feature_available",
            "surface_distance_feature_available",
        )
    )
    all_model_families_absent = not (
        mask["speed_feature_available"] or mask["pace_feature_available"]
        or mask["form_feature_available"] or mask["trip_feature_available"]
    )

    scoreable_mode = mode in (RunMode.MODEL_READY_LIMITED, RunMode.MODEL_READY)

    # DK is never wagering-eligible regardless of state.
    betting_reason = (
        "DraftKings Horse program PDF is a pre-race information source, not a "
        "priced wagering market; edge, fair odds and bet tags stay disabled."
    )

    if scoreable_mode and (all_model_families_absent or not core_signal):
        proxy = proxy_model_available(conn)
        if proxy is not None:
            return ModelPolicyDecision(
                scoring_state=None,
                model_family_selected=LIMITED_HISTORY_PROXY_FAMILY,
                model_version=proxy.get("version"),
                model_feature_schema_version=proxy.get(
                    "feature_schema_version", DK_MODEL_FEATURE_SCHEMA_VERSION
                ),
                feature_availability_mask=mask,
                calibration_version=proxy.get("calibration_artifact_path"),
                confidence_tier="limited_data_proxy",
                scoring_eligibility=True,
                betting_eligibility=False,
                disabled_capability_reasons={
                    "betting": betting_reason,
                    "fair_odds": betting_reason,
                    "edge_vs_market": betting_reason,
                },
            )
        return ModelPolicyDecision(
            scoring_state=FEATURE_LIMITED_NO_SCORING,
            model_family_selected=None,
            model_version=None,
            model_feature_schema_version=None,
            feature_availability_mask=mask,
            calibration_version=None,
            confidence_tier="feature_limited_no_scoring",
            scoring_eligibility=False,
            betting_eligibility=False,
            disabled_capability_reasons={
                "win_probabilities": (
                    "No limited_history_proxy model is registered; the standard "
                    "full-feature model would impute missing speed, pace, form and "
                    "trip history, so it must not score this card."
                ),
                "fair_odds": "Requires model win probabilities, which are not emitted.",
                "rankings": "Requires model win probabilities, which are not emitted.",
                "betting": betting_reason,
            },
        )

    # Standard path — DK card has enough independent signal, or a non-scoreable
    # mode (BLOCKED / PP_PARSED_FEATURES_PENDING / MARKET_BASELINE_ONLY).
    cfg = model_config_for_card(conn, card_id)
    return ModelPolicyDecision(
        scoring_state=None,
        model_family_selected=cfg.get("model_family") if scoreable_mode else None,
        model_version=cfg.get("version") if scoreable_mode else None,
        model_feature_schema_version=DK_MODEL_FEATURE_SCHEMA_VERSION if scoreable_mode else None,
        feature_availability_mask=mask,
        calibration_version=cfg.get("calibration_method") if scoreable_mode else None,
        confidence_tier=(
            "limited_source" if mode == RunMode.MODEL_READY_LIMITED
            else "standard" if mode == RunMode.MODEL_READY
            else "not_scoreable"
        ),
        scoring_eligibility=scoreable_mode,
        betting_eligibility=False,  # DK source is never wagering-eligible
        disabled_capability_reasons={"betting": betting_reason},
    )
