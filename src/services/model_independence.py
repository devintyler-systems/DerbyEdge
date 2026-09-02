"""Non-market probability construction and market-prior collapse detection.

The scorer's historical artifact may be calibrated against morning-line
probabilities.  These helpers deliberately sit outside that path: the signal
below reads only permitted feature columns, and the detector fails closed when
the resulting forecast cannot be shown to be independent of the ML prior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd


MODEL_COLLAPSED_TO_ML_PRIOR = "MODEL_COLLAPSED_TO_ML_PRIOR"
COLLAPSE_DELTA_THRESHOLD = 0.0025
_PRE_MARKET_SIGNAL_TEMPERATURE = 8.0


@dataclass(frozen=True)
class MarketPriorCollapse:
    """Auditable result of comparing a displayed forecast with its ML prior."""

    status: str | None
    max_abs_delta: float | None
    mean_abs_delta: float | None
    reason: str | None

    @property
    def collapsed(self) -> bool:
        return self.status == MODEL_COLLAPSED_TO_ML_PRIOR


def is_market_derived_feature(name: str) -> bool:
    """Return whether a feature is direct morning-line/market information."""
    normalized = str(name).strip().lower()
    return (
        normalized in {
            "market_implied_prob",
            "morning_line_rank",
            "publicness_score",
            "public_underlay_penalty",
        }
        or "morning_line" in normalized
        or normalized.startswith("ml_")
        or "market" in normalized
        or "public" in normalized
    )


def pre_market_signal_probabilities(
    feat_df: pd.DataFrame,
    config: Mapping[str, object],
) -> np.ndarray:
    """Build a field-normalized probability vector from non-market features.

    Relative feature and group weights are inherited from the active model
    configuration.  Market-derived features and their group are excluded, then
    the remaining configured weights are re-normalized only to retain a valid
    probability scale.  There is intentionally no market-target calibration.
    """
    n_entries = len(feat_df)
    if n_entries == 0:
        return np.array([], dtype=float)

    feature_groups = config.get("feature_groups")
    if not isinstance(feature_groups, Mapping):
        return np.full(n_entries, np.nan, dtype=float)

    group_rows: list[tuple[float, np.ndarray]] = []
    for group_def in feature_groups.values():
        if not isinstance(group_def, Mapping):
            continue
        features = group_def.get("features")
        try:
            group_weight = float(group_def.get("group_weight", 0.0))
        except (TypeError, ValueError):
            group_weight = 0.0
        if not isinstance(features, Mapping) or group_weight <= 0:
            continue

        permitted = [
            (str(name), float(weight))
            for name, weight in features.items()
            if str(name) in feat_df.columns and not is_market_derived_feature(str(name))
        ]
        if not permitted:
            continue
        weight_total = sum(weight for _, weight in permitted)
        if weight_total <= 0:
            continue

        group_score = np.zeros(n_entries, dtype=float)
        for name, weight in permitted:
            raw = pd.to_numeric(feat_df[name], errors="coerce").to_numpy(dtype=float)
            finite = np.isfinite(raw)
            if not finite.any():
                normalized = np.full(n_entries, 0.5, dtype=float)
            else:
                fill = float(np.median(raw[finite]))
                raw = np.where(finite, raw, fill)
                low, high = float(raw.min()), float(raw.max())
                normalized = (
                    np.full(n_entries, 0.5, dtype=float)
                    if high == low else (raw - low) / (high - low)
                )
            group_score += normalized * (weight / weight_total)
        group_rows.append((group_weight, group_score))

    if not group_rows:
        return np.full(n_entries, np.nan, dtype=float)

    group_weight_total = sum(weight for weight, _ in group_rows)
    composite = sum(
        score * (weight / group_weight_total) for weight, score in group_rows
    )
    if not np.isfinite(composite).all():
        return np.full(n_entries, np.nan, dtype=float)
    shifted = (composite - composite.max()) * _PRE_MARKET_SIGNAL_TEMPERATURE
    exponentiated = np.exp(shifted)
    total = float(exponentiated.sum())
    return exponentiated / total if total > 0 and np.isfinite(total) else np.full(n_entries, np.nan)


def detect_market_prior_collapse(
    p_model_pre_market: np.ndarray | list[float],
    p_ml_implied: np.ndarray | list[float],
    *,
    displayed_model_assigned_from_market: bool = False,
) -> MarketPriorCollapse:
    """Fail closed when a forecast is indistinguishable from its ML prior."""
    model = np.asarray(p_model_pre_market, dtype=float)
    market = np.asarray(p_ml_implied, dtype=float)
    if (
        model.size == 0
        or market.size != model.size
        or not np.isfinite(model).all()
        or not np.isfinite(market).all()
    ):
        return MarketPriorCollapse(
            MODEL_COLLAPSED_TO_ML_PRIOR, None, None,
            "p_model_pre_market or p_ml_implied is missing or non-finite.",
        )

    deltas = np.abs(model - market)
    max_abs_delta = float(deltas.max())
    mean_abs_delta = float(deltas.mean())
    if displayed_model_assigned_from_market:
        return MarketPriorCollapse(
            MODEL_COLLAPSED_TO_ML_PRIOR, max_abs_delta, mean_abs_delta,
            "The displayed model vector was assigned directly from market_implied_prob.",
        )
    if max_abs_delta < COLLAPSE_DELTA_THRESHOLD:
        return MarketPriorCollapse(
            MODEL_COLLAPSED_TO_ML_PRIOR, max_abs_delta, mean_abs_delta,
            f"max_abs_delta {max_abs_delta:.6f} is below {COLLAPSE_DELTA_THRESHOLD:.4f}.",
        )
    return MarketPriorCollapse(None, max_abs_delta, mean_abs_delta, None)
