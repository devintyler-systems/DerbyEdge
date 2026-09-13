"""Read persisted per-entry feature lineage for display-only diagnostics."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any


_META_COLUMNS = frozenset({
    "entry_id", "horse_id", "card_id", "horse_name", "post_position",
    "build_ts", "feature_lineage_json",
})


def _display_value(value: Any) -> Any:
    """Convert a pandas NaN to the value the UI/CSV display expects."""
    return None if isinstance(value, float) and math.isnan(value) else value


def runtime_lineage_by_feature(feature_row: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the persisted runtime lineage, keyed by feature name.

    ``feature_lineage_json`` is the only authority for an entry's runtime
    status, source, evidence, and fallback reason.  The static feature catalog
    describes implementation intent and must not override this data.
    """
    try:
        items = json.loads(feature_row.get("feature_lineage_json") or "[]")
    except (TypeError, ValueError):
        return {}
    return {
        str(item["feature_name"]): item
        for item in items
        if isinstance(item, dict) and item.get("feature_name")
    }


def _runtime_feature_status_rows(
    feature_row: Mapping[str, Any],
    *,
    importances: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Build display rows solely from the entry's persisted runtime lineage."""
    lineage_by_feature = runtime_lineage_by_feature(feature_row)
    importances = importances or {}
    rows: list[dict[str, Any]] = []
    for feature_name, raw_value in feature_row.items():
        if feature_name in _META_COLUMNS or feature_name.startswith("_"):
            continue
        lineage = lineage_by_feature.get(str(feature_name), {})
        rows.append({
            "feature": str(feature_name),
            "value": _display_value(raw_value),
            # Do not fall back to the static catalog: a legacy row without
            # runtime lineage is unverifiable, rather than IMPLEMENTED/DEGRADED.
            "tier": lineage.get("status") or lineage.get("tier") or "UNKNOWN",
            "source": lineage.get("source_system") or lineage.get("source") or "unknown",
            "evidence": lineage.get("evidence_count", 0),
            "reason": lineage.get("fallback_reason") if lineage else "runtime lineage unavailable",
            "in_model": float(importances.get(str(feature_name), 0.0)) > 0,
            "importance": importances.get(str(feature_name), 0.0),
        })
    return rows


def entry_details_feature_status_rows(
    feature_row: Mapping[str, Any],
    *,
    importances: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Rows for Entry Details → Feature Audit."""
    return _runtime_feature_status_rows(feature_row, importances=importances)


def model_diagnostics_feature_status_rows(
    feature_row: Mapping[str, Any],
    *,
    importances: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Rows for Model Diagnostics → Top Feature Importances."""
    return _runtime_feature_status_rows(feature_row, importances=importances)
