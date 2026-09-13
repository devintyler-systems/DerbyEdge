"""Neutral race-family assignment shared by read-only audit services."""
from __future__ import annotations

import math
from typing import Any


def _distance(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def classify_model_family(surface: Any, distance_furlongs: Any, stakes_name: Any = None, race_class: Any = None) -> str:
    """Classify ordinary races while keeping Kentucky Derby wholly separate."""
    derby_text = " ".join(str(value or "").casefold() for value in (stakes_name, race_class))
    if "kentucky derby" in derby_text:
        return "kentucky_derby"
    surface_norm = str(surface or "").casefold()
    if surface_norm in {"synthetic", "all_weather", "all-weather"}:
        return "synthetic"
    return f"{'turf' if surface_norm == 'turf' else 'dirt'}_{'sprint' if _distance(distance_furlongs) < 8.5 else 'route'}"
