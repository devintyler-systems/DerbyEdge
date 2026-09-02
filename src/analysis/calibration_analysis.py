"""Calibration snapshot analysis harness.

Reads the latest calibration_snapshot_YYYYMMDD.csv from output/ and prints
grouped performance tables to stdout.

Usage:
    python -m src.analysis.calibration_analysis
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT       = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "output"

_W = 72  # section header width


def _hr(title: str) -> None:
    print(f"\n{'=' * _W}")
    print(f"  {title}")
    print("=" * _W)


def find_latest_snapshot(output_dir: Path = OUTPUT_DIR) -> Path | None:
    """Return the lexicographically latest calibration_snapshot_*.csv, or None."""
    files = sorted(output_dir.glob("calibration_snapshot_*.csv"))
    return files[-1] if files else None


def _load(path: Path) -> pd.DataFrame | None:
    df = pd.read_csv(path)
    if df.empty:
        print(f"[calibration_analysis] {path.name} is empty — nothing to analyse.")
        return None
    print(f"Loaded: {path.name}  ({len(df)} rows × {len(df.columns)} cols)")
    present = [c for c in [
        "run_id", "card_id", "top_pick_hit", "winner_rank",
        "winner_official_odds", "confidence_bucket", "confidence_score",
        "chaos_active", "ptf_aligned", "value_gap_top_vs_ptf",
    ] if c in df.columns]
    print(f"Key columns: {present}")
    return df


def _add_profit(df: pd.DataFrame) -> pd.DataFrame:
    """Add profit_unit_flat: winner_official_odds on hit, -1.0 on miss."""
    odds = df.get("winner_official_odds", pd.Series(dtype=float))

    def _p(row: pd.Series) -> float:
        if row["top_pick_hit"] == 1 and pd.notna(row.get("winner_official_odds")):
            return float(row["winner_official_odds"])
        return -1.0

    df = df.copy()
    df["profit_unit_flat"] = df.apply(_p, axis=1)
    return df


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _section_a(df: pd.DataFrame) -> None:
    _hr("A) Hit-rate by confidence_bucket")
    tbl = (
        df.groupby("confidence_bucket", dropna=False)
        .agg(
            races=("run_id", "count"),
            top_pick_hit_rate=("top_pick_hit", "mean"),
            avg_winner_rank=("winner_rank", "mean"),
        )
        .round(3)
    )
    print(tbl.to_string())


def _section_b(df: pd.DataFrame) -> None:
    _hr("B) Hit-rate by confidence_bucket x chaos_active")
    tbl = (
        df.groupby(["confidence_bucket", "chaos_active"], dropna=False)
        .agg(
            races=("run_id", "count"),
            top_pick_hit_rate=("top_pick_hit", "mean"),
            avg_winner_rank=("winner_rank", "mean"),
        )
        .round(3)
    )
    print(tbl.to_string())


def _section_c(df: pd.DataFrame) -> None:
    _hr("C) Hit-rate by confidence_bucket x ptf_aligned")
    tbl = (
        df.groupby(["confidence_bucket", "ptf_aligned"], dropna=False)
        .agg(
            races=("run_id", "count"),
            top_pick_hit_rate=("top_pick_hit", "mean"),
            avg_winner_rank=("winner_rank", "mean"),
        )
        .round(3)
    )
    print(tbl.to_string())


def _section_d(df: pd.DataFrame) -> None:
    _hr("D-i) Flat-bet ROI by confidence_bucket")
    grp1 = df.groupby("confidence_bucket", dropna=False).agg(
        races=("run_id", "count"),
        total_profit=("profit_unit_flat", "sum"),
    )
    grp1["roi_per_race"] = (grp1["total_profit"] / grp1["races"].clip(lower=1)).round(3)
    grp1 = grp1.round(3)
    print(grp1.to_string())

    _hr("D-ii) Flat-bet ROI by confidence_bucket x chaos_active x ptf_aligned")
    grp2 = df.groupby(
        ["confidence_bucket", "chaos_active", "ptf_aligned"], dropna=False
    ).agg(
        races=("run_id", "count"),
        total_profit=("profit_unit_flat", "sum"),
    )
    grp2["roi_per_race"] = (grp2["total_profit"] / grp2["races"].clip(lower=1)).round(3)
    grp2 = grp2.round(3)
    print(grp2.to_string())


def _section_e(df: pd.DataFrame) -> None:
    _hr("E) Value-gap slice (confidence_bucket x value_gap_bucket)")
    col = "value_gap_top_vs_ptf"
    if col not in df.columns:
        print(f"  Skipping: column '{col}' not found in snapshot.")
        return
    if df[col].isna().all():
        print(f"  Skipping: '{col}' is entirely null in this snapshot (no PTF odds).")
        return

    df = df.copy()
    bins   = [-1.0, -0.05, 0.0, 0.05, 0.10, 1.0]
    labels = ["<-5%", "-5–0%", "0–5%", "5–10%", "10%+"]
    df["value_gap_bucket"] = pd.cut(
        df[col], bins=bins, labels=labels, include_lowest=True
    )
    tbl = (
        df.groupby(["confidence_bucket", "value_gap_bucket"], dropna=False, observed=False)
        .agg(
            races=("run_id", "count"),
            top_pick_hit_rate=("top_pick_hit", "mean"),
            roi_per_race=("profit_unit_flat", "mean"),
        )
        .round(3)
    )
    print(tbl.to_string())


# ---------------------------------------------------------------------------
# Public driver
# ---------------------------------------------------------------------------

def run_analysis(output_dir: Path = OUTPUT_DIR) -> None:
    """Find the latest snapshot in output_dir and print all analysis tables."""
    path = find_latest_snapshot(output_dir)
    if path is None:
        print(
            "[calibration_analysis] No calibration_snapshot_*.csv found in "
            f"{output_dir} — run calibration_snapshot first."
        )
        return

    df = _load(path)
    if df is None:
        return

    df = _add_profit(df)

    _section_a(df)
    _section_b(df)
    _section_c(df)
    _section_d(df)
    _section_e(df)

    print(f"\n{'=' * _W}")
    print("  Done.")
    print("=" * _W)


def main() -> None:
    run_analysis()


if __name__ == "__main__":
    main()
