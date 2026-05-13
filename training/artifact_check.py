"""
training/artifact_check.py

Check whether trained ML model artifacts are present in models/artifacts/.
Used by run_shadow_cycle to warn operators before shadow scoring.

Public API
----------
any_artifact_available() -> bool
    True if at least one win_model_*.pkl exists.

available_artifacts() -> dict[str, str | None]
    {segment: "YYYYMMDD"} for each known segment; None when absent.

artifact_status_lines() -> list[str]
    Human-readable lines for terminal display.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

_ARTIFACTS = Path(__file__).resolve().parents[1] / "models" / "artifacts"

_KNOWN_SEGMENTS = [
    "dirt_sprint", "dirt_route",
    "turf_sprint", "turf_route",
    "other", "pooled",
]


def available_artifacts() -> dict[str, Optional[str]]:
    """Return {segment: latest_version_tag_or_None} for every known segment."""
    result: dict[str, Optional[str]] = {}
    for seg in _KNOWN_SEGMENTS:
        files = sorted(_ARTIFACTS.glob(f"win_model_{seg}_*.pkl"))
        result[seg] = files[-1].stem.split("_")[-1] if files else None
    return result


def any_artifact_available() -> bool:
    """True if at least one trained win_model_*.pkl exists."""
    if not _ARTIFACTS.exists():
        return False
    return bool(list(_ARTIFACTS.glob("win_model_*.pkl")))


def artifact_status_lines() -> list[str]:
    """Return lines summarising which segments have artifacts and which do not."""
    arts = available_artifacts()
    present = {seg: v for seg, v in arts.items() if v}
    absent  = [seg for seg, v in arts.items() if not v]
    lines: list[str] = []
    if present:
        segs = ", ".join(f"{seg}(v{v})" for seg, v in present.items())
        lines.append(f"  Available : {segs}")
    if absent:
        lines.append(f"  Missing   : {', '.join(absent)}")
    return lines
