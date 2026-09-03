"""Verification of the exact feature frame supplied to race scoring."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.ingest.run_state import feature_degeneracy_warnings
from src.models.trainer import TRAIN_CONFIGS, compute_group_scores


CORE_FEATURE_NAMES = ("pace_fit", "form", "surface_distance_fit")
PP_BACKED_FEATURE_NAMES = (
    "horses_beaten_pct_last",
    "form_cycle_idx",
    "distance_fit",
    "surface_fit",
)


@dataclass(frozen=True)
class FeatureVerification:
    schema_complete: bool
    entry_coverage_complete: bool
    core_rows: list[dict[str, float | None]]
    missing_columns: tuple[str, ...]
    warnings: tuple[str, ...]
    pp_backed_features_required: bool = False
    pp_backed_features_nonconstant: bool = False
    pace_state: str | None = None

    @property
    def passed(self) -> bool:
        core_warnings = feature_degeneracy_warnings(
            self.core_rows, CORE_FEATURE_NAMES
        )
        return (
            self.schema_complete
            and self.entry_coverage_complete
            and bool(self.core_rows)
            and len(core_warnings) < len(CORE_FEATURE_NAMES)
            and (
                not self.pp_backed_features_required
                or self.pp_backed_features_nonconstant
            )
        )


def model_config_for_card(conn: sqlite3.Connection, card_id: int) -> dict:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(race_cards)").fetchall()
    }
    distance_expr = (
        "distance_furlongs" if "distance_furlongs" in columns
        else "distance_yards / 220.0" if "distance_yards" in columns
        else "0.0"
    )
    row = conn.execute(
        f"SELECT surface, {distance_expr} FROM race_cards WHERE card_id=?",
        (card_id,),
    ).fetchone()
    surface = str(row[0] if row else "dirt").lower()
    surface = "turf" if surface == "turf" else "dirt"
    distance = float(row[1] or 0.0) if row else 0.0
    key = f"{surface}_{'sprint' if distance < 8.5 else 'route'}"
    return TRAIN_CONFIGS.get(key, TRAIN_CONFIGS["dirt_route"])


def verify_feature_frame(
    feat_df: pd.DataFrame,
    config: dict,
    *,
    expected_entries: int | None = None,
    require_pp_backed_features: bool = False,
) -> FeatureVerification:
    """Verify model schema, starter coverage, and nonconstant core signal."""
    required = tuple(dict.fromkeys(
        name
        for group in config["feature_groups"].values()
        for name in group["features"]
    ))
    missing = tuple(name for name in required if name not in feat_df.columns)
    expected = expected_entries if expected_entries is not None else len(feat_df)
    entry_coverage_complete = bool(expected) and len(feat_df) == expected

    core_rows: list[dict[str, float | None]] = []
    if not feat_df.empty:
        groups = compute_group_scores(feat_df, config)
        form = groups.get("form_class", [])
        surface_distance = groups.get("distance_surface", [])
        pace = pd.to_numeric(
            feat_df.get("pace_fit_score", pd.Series(index=feat_df.index, dtype=float)),
            errors="coerce",
        )
        for index in range(len(feat_df)):
            core_rows.append({
                "pace_fit": _finite_or_none(pace.iloc[index]),
                "form": _finite_or_none(form[index] if len(form) > index else None),
                "surface_distance_fit": _finite_or_none(
                    surface_distance[index] if len(surface_distance) > index else None
                ),
            })

    warnings: list[str] = []
    if missing:
        warnings.append("Missing model-required feature columns: " + ", ".join(missing))
    if not entry_coverage_complete:
        warnings.append(
            f"Feature rows cover {len(feat_df)} of {expected or 0} parsed entries."
        )
    warnings.extend(feature_degeneracy_warnings(core_rows, CORE_FEATURE_NAMES))
    pace_states = (
        feat_df.get("pace_state", pd.Series(dtype=object))
        .dropna().astype(str).unique().tolist()
    )
    pace_state = pace_states[0] if len(pace_states) == 1 else None
    if pace_state == "PACE_UNAVAILABLE":
        warnings.append(
            "PACE_UNAVAILABLE: pace_fit_score is null for the active field and excluded from the forecast."
        )
    elif pace_state == "PACE_PARTIAL":
        warnings.append(
            "PACE_PARTIAL: only classified runners retain pace fit; confidence is reduced for unknown styles."
        )
    pp_backed_nonconstant = any(
        name in feat_df.columns
        and pd.to_numeric(feat_df[name], errors="coerce").nunique(dropna=True) > 1
        for name in PP_BACKED_FEATURE_NAMES
    )
    if require_pp_backed_features and not pp_backed_nonconstant:
        warnings.append(
            "Parsed 1/ST PPs did not produce a nonconstant PP-backed model feature."
        )
    return FeatureVerification(
        schema_complete=not missing,
        entry_coverage_complete=entry_coverage_complete,
        core_rows=core_rows,
        missing_columns=missing,
        warnings=tuple(warnings),
        pp_backed_features_required=require_pp_backed_features,
        pp_backed_features_nonconstant=pp_backed_nonconstant,
        pace_state=pace_state,
    )


def verify_card_features(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    expected_entries: int,
    require_pp_backed_features: bool = False,
) -> FeatureVerification:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feature_store'"
    ).fetchone()
    feat_df = (
        pd.read_sql(
            "SELECT * FROM feature_store WHERE card_id=? ORDER BY post_position",
            conn,
            params=(card_id,),
        )
        if table_exists else pd.DataFrame()
    )
    return verify_feature_frame(
        feat_df,
        model_config_for_card(conn, card_id),
        expected_entries=expected_entries,
        require_pp_backed_features=require_pp_backed_features,
    )


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
