"""
training/generate_promotion_report.py

Read evaluation artifacts and produce output/ml_promotion_report.md —
a concise operator-facing report answering: "Should ML replace heuristic?"

Inputs (from most recent eval_run_* directory, or --eval-dir override)
----------------------------------------------------------------------
  metrics_summary.json
  segment_metrics.csv
  calibration_table.csv
  insufficient_segments.csv
  promotion_decision.json

Usage
-----
    python -m training.generate_promotion_report
    python -m training.generate_promotion_report --eval-dir output/eval_run_20260512_120000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT    = _REPO_ROOT / "output"
_REPORT    = _OUTPUT / "ml_promotion_report.md"


def _latest_eval_dir() -> Path | None:
    dirs = sorted(_OUTPUT.glob("eval_run_*"), key=lambda p: p.name, reverse=True)
    return dirs[0] if dirs else None


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt(v, fmt=".4f", fallback="—") -> str:
    if v is None or (isinstance(v, float) and __import__("math").isnan(v)):
        return fallback
    try:
        return format(float(v), fmt)
    except (TypeError, ValueError):
        return str(v)


def _pct_arrow(delta) -> str:
    if delta is None:
        return "—"
    try:
        d = float(delta)
    except (TypeError, ValueError):
        return "—"
    arrow = "▲" if d > 0 else "▼" if d < 0 else "="
    return f"{arrow} {abs(d):.1f}%"


def _seg_flag(delta) -> str:
    if delta is None:
        return "—"
    try:
        d = float(delta)
    except (TypeError, ValueError):
        return "—"
    if d >= 3.0:
        return "PASS"
    if d < -1.0:
        return "FAIL"
    return "HOLD"


def generate_report(eval_dir: Path | None = None) -> Path:
    if eval_dir is None:
        eval_dir = _latest_eval_dir()
    if eval_dir is None or not eval_dir.exists():
        print("No eval_run_* directory found in output/. Run evaluate_shadow_vs_baseline first.")
        sys.exit(1)

    # Load artifacts
    summary    = _load_json(eval_dir / "metrics_summary.json")
    promotion  = _load_json(eval_dir / "promotion_decision.json")
    seg_path   = eval_dir / "segment_metrics.csv"
    calib_path = eval_dir / "calibration_table.csv"
    insuf_path = eval_dir / "insufficient_segments.csv"
    jdiag_path = eval_dir / "join_diagnostics.json"

    seg_df   = pd.read_csv(seg_path)   if seg_path.exists()   else pd.DataFrame()
    calib_df = pd.read_csv(calib_path) if calib_path.exists() else pd.DataFrame()
    insuf_df = pd.read_csv(insuf_path) if insuf_path.exists() else pd.DataFrame()
    jdiag    = _load_json(jdiag_path)  if jdiag_path.exists() else {}

    h = summary.get("heuristic") or {}
    m = summary.get("ml")        or {}
    decision    = promotion.get("decision", "HOLD")
    reasons     = promotion.get("reasons", [])
    rec_action  = promotion.get("recommended_action", "remain in shadow")
    eval_ts     = summary.get("evaluated_at", "N/A")
    n_races     = summary.get("n_races_total", "N/A")
    n_labeled   = summary.get("n_labeled_rows", "N/A")
    ll_imp      = summary.get("ll_improvement_pct")
    brier_imp   = summary.get("brier_improvement_pct")
    integrity_ok  = promotion.get("integrity_checks_passed", False)
    int_checks    = promotion.get("integrity_checks", [])
    insuf_segs    = promotion.get("insufficient_segments", [])
    thresholds    = promotion.get("thresholds", {})

    lines = [
        "# ML Promotion Report — DerbyEdge Engine",
        "",
        f"**Evaluated:** {eval_ts}  ",
        f"**Eval directory:** `{eval_dir.relative_to(_REPO_ROOT)}`  ",
        f"**Races with outcomes:** {n_races}  ",
        f"**Labeled starters:** {n_labeled}",
        "",
        "---",
        "",
        "## 1. Overall Comparison",
        "",
        "| Model | Log Loss | Brier | Top-Pick Hit% | Winner in Top 3 | Avg Winner Rank |",
        "|-------|----------|-------|---------------|-----------------|-----------------|",
        f"| Heuristic | {_fmt(h.get('log_loss'))} | {_fmt(h.get('brier'))} "
        f"| {_fmt(h.get('top1_hit_rate'), '.3f')} | {_fmt(h.get('winner_top3'), '.3f')} "
        f"| {_fmt(h.get('avg_winner_rank'), '.2f')} |",
        f"| ML | {_fmt(m.get('log_loss'))} | {_fmt(m.get('brier'))} "
        f"| {_fmt(m.get('top1_hit_rate'), '.3f')} | {_fmt(m.get('winner_top3'), '.3f')} "
        f"| {_fmt(m.get('avg_winner_rank'), '.2f')} |",
        "",
        f"**Log loss improvement:** {_pct_arrow(ll_imp)}  ",
        f"**Brier improvement:** {_pct_arrow(brier_imp)}",
        "",
    ]

    # Join diagnostics (if available)
    if jdiag:
        mr     = jdiag.get("match_rate", 0.0)
        n_tot  = jdiag.get("total_shadow_rows", "N/A")
        n_mat  = jdiag.get("matched_rows", "N/A")
        n_unm  = jdiag.get("unmatched_rows", "N/A")
        n_full = jdiag.get("matched_full_key", "N/A")
        n_part = jdiag.get("matched_partial_key", "N/A")
        mr_pct = f"{mr:.1%}" if isinstance(mr, float) else "N/A"
        lines += [
            "---",
            "",
            "## 2. Join Diagnostics",
            "",
            f"| Total shadow rows | Matched | Full key | Partial key | Unmatched | Match rate |",
            f"|-------------------|---------|----------|-------------|-----------|------------|",
            f"| {n_tot} | {n_mat} | {n_full} | {n_part} | {n_unm} | {mr_pct} |",
            "",
        ]
        if isinstance(mr, float) and mr < 0.80:
            lines += [
                "> **Warning:** match rate below 80%.  Check `unmatched_shadow_rows.csv`.",
                "> Common causes: horse name formatting mismatch, missing post position,",
                "> or race results not yet loaded (run `backfill_observations`).",
                "",
            ]
        unmatched_keys = jdiag.get("sample_unmatched_keys", [])
        if unmatched_keys:
            lines += [
                f"**Sample unmatched keys** (race_id|post|horse_norm):",
                "",
            ]
            for k in unmatched_keys:
                lines.append(f"- `{k}`")
            lines.append("")
        section_num = 3
    else:
        section_num = 2

    # Segment table
    lines += [
        "---",
        "",
        f"## {section_num}. Segment Breakdown",
        "",
    ]
    section_num += 1

    if seg_df.empty:
        lines.append("_No segment data available._")
    else:
        lines += [
            "| Segment | Races | Starters | Winners | H Log Loss | ML Log Loss | LL Delta | H Brier | ML Brier | Flag |",
            "|---------|-------|----------|---------|------------|-------------|----------|---------|----------|------|",
        ]
        # Build set of insufficient segment names for easy lookup
        insuf_names = {r.get("segment") for r in insuf_segs}
        for _, row in seg_df.iterrows():
            seg_name   = row.get("segment", "—")
            sparse_tag = " ⚠" if seg_name in insuf_names else ""
            lines.append(
                f"| {seg_name}{sparse_tag} "
                f"| {_fmt(row.get('n_races'), '.0f')} "
                f"| {_fmt(row.get('n_starters'), '.0f')} "
                f"| {_fmt(row.get('n_winners', None), '.0f')} "
                f"| {_fmt(row.get('h_log_loss'))} "
                f"| {_fmt(row.get('ml_log_loss'))} "
                f"| {_pct_arrow(row.get('ll_delta_pct'))} "
                f"| {_fmt(row.get('h_brier'))} "
                f"| {_fmt(row.get('ml_brier'))} "
                f"| {_seg_flag(row.get('ll_delta_pct'))} |"
            )

    lines += [""]

    # Insufficient segments detail
    if not insuf_df.empty:
        lines += [
            "---",
            "",
            f"## {section_num}. Insufficient Segments",
            "",
            "> These segments have too few races or winners for reliable evaluation.",
            "> Their metrics are shown above but should not be used as promotion evidence.",
            "",
            "| Segment | Races | Winners | Reason |",
            "|---------|-------|---------|--------|",
        ]
        for _, row in insuf_df.iterrows():
            lines.append(
                f"| {row.get('segment','—')} "
                f"| {_fmt(row.get('n_races'), '.0f')} "
                f"| {_fmt(row.get('n_winners', None), '.0f')} "
                f"| {row.get('reasons','—')} |"
            )
        lines += [""]
        section_num += 1

    # Integrity checks table
    lines += [
        "---",
        "",
        f"## {section_num}. Integrity Checks",
        "",
        "| Check | Result | Detail |",
        "|-------|--------|--------|",
    ]
    section_num += 1
    if int_checks:
        for c in int_checks:
            status = "PASS" if c.get("passed") else "**FAIL**"
            lines.append(f"| `{c['check']}` | {status} | {c.get('detail','—')} |")
    else:
        lines.append("| _No integrity check data_ | — | — |")

    lines += [""]

    # Calibration table + interpretation
    lines += [
        "---",
        "",
        f"## {section_num}. Calibration",
        "",
        "> **Reading the calibration table:**",
        "> Each row is a probability bin.  A well-calibrated model has",
        "> `mean_predicted ≈ actual_win_rate` (on the diagonal).",
        "> - **Above diagonal** (actual > predicted): model is *underconfident* —",
        ">   it assigns lower probabilities than the horses actually win at.",
        "> - **Below diagonal** (actual < predicted): model is *overconfident* —",
        ">   it assigns higher probabilities than the horses actually win at.",
        "> - ⚠ marks bins with fewer than 20 samples — treat those rows with caution.",
        "",
    ]
    section_num += 1

    if calib_df.empty:
        lines.append("_No calibration data available._")
    else:
        lines += [
            "| Model | Bin | n | Mean Predicted | Actual Win Rate | Sparse? |",
            "|-------|-----|---|----------------|-----------------|---------|",
        ]
        for _, row in calib_df.iterrows():
            sparse_flag = "⚠" if row.get("flag_sparse", False) else ""
            bin_label   = f"{_fmt(row.get('bin_low'), '.2f')}–{_fmt(row.get('bin_high'), '.2f')}"
            lines.append(
                f"| {row.get('model','—')} "
                f"| {bin_label} "
                f"| {_fmt(row.get('n'), '.0f')} "
                f"| {_fmt(row.get('mean_predicted'))} "
                f"| {_fmt(row.get('actual_win_rate'))} "
                f"| {sparse_flag} |"
            )

    lines += [""]

    # Feature Coverage
    null_audit_path = _OUTPUT / "feature_null_audit.csv"
    fimp_path       = _OUTPUT / "feature_importance_report.csv"
    null_df  = pd.read_csv(null_audit_path) if null_audit_path.exists() else pd.DataFrame()
    fimp_df  = pd.read_csv(fimp_path)       if fimp_path.exists()       else pd.DataFrame()

    if not null_df.empty:
        lines += [
            "---",
            "",
            f"## {section_num}. Feature Coverage",
            "",
            "| Feature | Tier | Null Rate | Importance Rank | Flags |",
            "|---------|------|-----------|-----------------|-------|",
        ]
        section_num += 1

        rank_map: dict[str, int] = {}
        if not fimp_df.empty and "feature_name" in fimp_df.columns and "importance_rank" in fimp_df.columns:
            rank_map = dict(zip(fimp_df["feature_name"], fimp_df["importance_rank"].astype(int)))

        t1_features = {"speed_fig_adj", "layoff_bucket_encoded", "class_delta_v2",
                       "horses_beaten_pct_actual", "pace_pressure_tier",
                       "collapse_risk_v2", "morning_line_delta"}

        for _, row in null_df.iterrows():
            feat      = row.get("feature", "—")
            tier      = row.get("tier", "UNKNOWN")
            null_rate = float(row.get("null_rate", 0.0))
            rank      = rank_map.get(str(feat))
            rank_str  = str(rank) if rank is not None else "—"
            flags: list[str] = []
            if null_rate > 0.50:
                flags.append("⚠️ DATA GAP")
            if feat in t1_features and rank is not None and rank > 15:
                flags.append("⚠️ LOW SIGNAL")
            lines.append(
                f"| {feat} | {tier} | {null_rate:.1%} | {rank_str} | {' '.join(flags)} |"
            )
        lines += [""]

    # Promotion decision
    decision_icon = {
        "PASS":              "✅ PASS",
        "HOLD":              "🔶 HOLD",
        "FAIL":              "❌ FAIL",
        "INSUFFICIENT_DATA": "🔵 INSUFFICIENT DATA",
    }.get(decision, decision)

    lines += [
        "---",
        "",
        f"## {section_num}. Promotion Decision",
        "",
        f"### {decision_icon}",
        "",
    ]
    section_num += 1

    for r in reasons:
        lines.append(f"- {r}")
    if not reasons:
        lines.append("_No reasons recorded._")

    # Sample-size thresholds box
    if thresholds:
        lines += [
            "",
            "> **Sample-size thresholds used:**",
            f"> - Overall minimum races: {thresholds.get('min_races_overall', '—')}",
            f"> - Per-segment minimum races: {thresholds.get('min_races_segment', '—')}",
            f"> - Per-segment minimum winners: {thresholds.get('min_winners_segment', '—')}",
            f"> - Log loss improvement to PASS: ≥ {thresholds.get('ll_improve_pct', '—')}%",
            f"> - Brier improvement to PASS: ≥ {thresholds.get('brier_improve_pct', '—')}%",
            f"> - Segment degradation to FAIL: > {thresholds.get('segment_degrade_pct', '—')}%",
        ]

    lines += [
        "",
        "---",
        "",
        f"## {section_num}. Recommended Next Action",
        "",
        f"**{rec_action.capitalize()}**",
        "",
        "| Mode | Command |",
        "|------|---------|",
        "| Stay in shadow | `DERBYEDGE_ML_MODE=shadow python scripts/score.py` |",
        "| Promote to live | `DERBYEDGE_ML_MODE=live python scripts/score.py` |",
        "| Turn off ML | `DERBYEDGE_ML_MODE=off python scripts/score.py` |",
        "",
        "---",
        "",
        f"_Generated by `training/generate_promotion_report.py` from `{eval_dir.name}`_",
    ]

    _OUTPUT.mkdir(parents=True, exist_ok=True)
    _REPORT.write_text("\n".join(lines), encoding="utf-8")
    return _REPORT


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate ML promotion report")
    parser.add_argument(
        "--eval-dir", default=None,
        help="Path to eval_run_* directory (default: most recent in output/)",
    )
    args = parser.parse_args()

    report_path = generate_report(
        eval_dir=Path(args.eval_dir) if args.eval_dir else None
    )
    print(f"Report written: {report_path}")


if __name__ == "__main__":
    main()
