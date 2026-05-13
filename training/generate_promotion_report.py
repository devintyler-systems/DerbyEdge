"""
training/generate_promotion_report.py

Read evaluation artifacts and produce output/ml_promotion_report.md —
a concise operator-facing report answering: "Should ML replace heuristic?"

Inputs (from most recent eval_run_* directory, or --eval-dir override)
----------------------------------------------------------------------
  metrics_summary.json
  segment_metrics.csv
  calibration_table.csv
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

    seg_df   = pd.read_csv(seg_path)   if seg_path.exists()   else pd.DataFrame()
    calib_df = pd.read_csv(calib_path) if calib_path.exists() else pd.DataFrame()

    h = summary.get("heuristic") or {}
    m = summary.get("ml")        or {}
    decision   = promotion.get("decision", "HOLD")
    reasons    = promotion.get("reasons", [])
    rec_action = promotion.get("recommended_action", "remain in shadow")
    eval_ts    = summary.get("evaluated_at", "N/A")
    n_races    = summary.get("n_races_total", "N/A")
    n_labeled  = summary.get("n_labeled_rows", "N/A")
    ll_imp     = summary.get("ll_improvement_pct")
    brier_imp  = summary.get("brier_improvement_pct")
    integrity_ok = promotion.get("integrity_checks_passed", False)
    int_checks   = promotion.get("integrity_checks", [])

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

    # Segment table
    lines += [
        "---",
        "",
        "## 2. Segment Breakdown",
        "",
    ]
    if seg_df.empty:
        lines.append("_No segment data available._")
    else:
        lines += [
            "| Segment | Races | Starters | H Log Loss | ML Log Loss | LL Delta | H Brier | ML Brier | Flag |",
            "|---------|-------|----------|------------|-------------|----------|---------|----------|------|",
        ]
        for _, row in seg_df.iterrows():
            lines.append(
                f"| {row.get('segment','—')} "
                f"| {_fmt(row.get('n_races'), '.0f')} "
                f"| {_fmt(row.get('n_starters'), '.0f')} "
                f"| {_fmt(row.get('h_log_loss'))} "
                f"| {_fmt(row.get('ml_log_loss'))} "
                f"| {_pct_arrow(row.get('ll_delta_pct'))} "
                f"| {_fmt(row.get('h_brier'))} "
                f"| {_fmt(row.get('ml_brier'))} "
                f"| {_seg_flag(row.get('ll_delta_pct'))} |"
            )

    lines += [""]

    # Integrity checks table
    lines += [
        "---",
        "",
        "## 3. Integrity Checks",
        "",
        "| Check | Result | Detail |",
        "|-------|--------|--------|",
    ]
    if int_checks:
        for c in int_checks:
            status = "PASS" if c.get("passed") else "**FAIL**"
            lines.append(f"| `{c['check']}` | {status} | {c.get('detail','—')} |")
    else:
        lines.append("| _No integrity check data_ | — | — |")

    lines += [""]

    # Promotion decision
    decision_icon = {"PASS": "✅ PASS", "HOLD": "🔶 HOLD", "FAIL": "❌ FAIL"}.get(decision, decision)
    lines += [
        "---",
        "",
        "## 4. Promotion Decision",
        "",
        f"### {decision_icon}",
        "",
    ]
    for r in reasons:
        lines.append(f"- {r}")
    if not reasons:
        lines.append("_No reasons recorded._")

    lines += [
        "",
        "---",
        "",
        "## 5. Recommended Next Action",
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
