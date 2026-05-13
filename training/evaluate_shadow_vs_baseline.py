"""
training/evaluate_shadow_vs_baseline.py

Evaluate ML win probabilities vs heuristic baseline from shadow-log data.
Produces a promotion recommendation: PASS | HOLD | FAIL | INSUFFICIENT_DATA.

Promotion thresholds
--------------------
  INSUFFICIENT_DATA  overall races < 30  (collect more data)
  PASS               ML log loss improves >= 3% overall
                     AND ML Brier improves >= 2% overall
                     AND no integrity check fails
                     AND no segment degrades LL by > 1%
  FAIL               any segment degrades ML log loss by > 1% vs heuristic
                     OR any integrity check fails
  HOLD               overall data sufficient but thresholds not yet met,
                     or individual segments are thin

Segment sufficiency rules (applied before PASS/FAIL/HOLD)
----------------------------------------------------------
  A segment is "insufficient" if:
    • n_races < 10  (too few labeled races), OR
    • n_winners < 10 (too few actual winners — calibration unreliable)
  Insufficient segments → segment is flagged, decision can be HOLD.
  Overall insufficient  → decision = INSUFFICIENT_DATA (no further check).

Usage
-----
    python -m training.evaluate_shadow_vs_baseline
    python -m training.evaluate_shadow_vs_baseline --eval-file output/shadow_eval.csv
    python -m training.evaluate_shadow_vs_baseline --out-dir output/eval_20260512
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss

_REPO_ROOT   = Path(__file__).resolve().parents[1]
_OUTPUT      = _REPO_ROOT / "output"
_SHADOW_EVAL = _OUTPUT / "shadow_eval.csv"

sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

# Promotion thresholds
_LL_IMPROVE_PCT_THRESHOLD    = 3.0   # PASS requires >= 3% overall LL improvement
_BRIER_IMPROVE_PCT_THRESHOLD = 2.0   # PASS requires >= 2% overall Brier improvement
_SEGMENT_DEGRADE_THRESHOLD   = 1.0   # FAIL if any segment degrades LL by > 1%

# Data sufficiency thresholds
_MIN_RACES_OVERALL   = 30  # overall races needed before any PASS/HOLD/FAIL decision
_MIN_RACES_SEGMENT   = 10  # races per segment to consider it evaluable
_MIN_WINNERS_SEGMENT = 10  # winner rows per segment (calibration reliability)

# Calibration bins with too few samples are flagged as unreliable
_CALIB_MIN_BIN_N = 20


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _log_loss_safe(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    y_pred = np.clip(y_pred, 1e-9, 1 - 1e-9)
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(log_loss(y_true, y_pred))


def _brier(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(brier_score_loss(y_true, y_pred))


def _top1_hit_rate(df: pd.DataFrame, prob_col: str) -> float:
    hits, total = 0, 0
    for _, grp in df.groupby("race_id"):
        if grp["win_flag"].sum() == 0:
            continue
        top_idx = grp[prob_col].idxmax()
        hits += int(grp.loc[top_idx, "win_flag"] == 1)
        total += 1
    return hits / total if total > 0 else float("nan")


def _winner_top3(df: pd.DataFrame, prob_col: str) -> float:
    hits, total = 0, 0
    for _, grp in df.groupby("race_id"):
        if grp["win_flag"].sum() == 0:
            continue
        top3 = grp.nlargest(3, prob_col)
        hits += int(top3["win_flag"].sum() > 0)
        total += 1
    return hits / total if total > 0 else float("nan")


def _avg_winner_rank(df: pd.DataFrame, prob_col: str) -> float:
    ranks = []
    for _, grp in df.groupby("race_id"):
        if grp["win_flag"].sum() == 0:
            continue
        sorted_grp = grp.sort_values(prob_col, ascending=False).reset_index(drop=True)
        winner = sorted_grp[sorted_grp["win_flag"] == 1]
        if not winner.empty:
            ranks.append(int(winner.index[0]) + 1)
    return float(np.mean(ranks)) if ranks else float("nan")


def _pct_improvement(baseline: float, new: float) -> float:
    """Return percentage improvement (positive = better / lower loss)."""
    if baseline == 0 or np.isnan(baseline) or np.isnan(new):
        return float("nan")
    return (baseline - new) / abs(baseline) * 100.0


def _compute_metrics(df: pd.DataFrame, prob_col: str) -> Optional[dict]:
    """Compute all evaluation metrics for a given probability column.

    Returns None if there are no valid rows with outcomes.
    """
    valid = df[df[prob_col].notna() & df["win_flag"].notna()].copy()
    if len(valid) == 0 or valid["win_flag"].sum() == 0:
        return None

    y_true = valid["win_flag"].astype(int).values
    y_pred = valid[prob_col].astype(float).values

    return {
        "n_races":         int(valid["race_id"].nunique()),
        "n_starters":      int(len(valid)),
        "n_winners":       int(valid["win_flag"].sum()),
        "log_loss":        round(_log_loss_safe(y_true, y_pred), 5),
        "brier":           round(_brier(y_true, y_pred), 5),
        "top1_hit_rate":   round(_top1_hit_rate(valid, prob_col), 4),
        "winner_top3":     round(_winner_top3(valid, prob_col), 4),
        "avg_winner_rank": round(_avg_winner_rank(valid, prob_col), 3),
    }


def _calibration_bins(
    y_true: np.ndarray, y_pred: np.ndarray, n_bins: int = 10
) -> list[dict]:
    bins = np.linspace(0, 1, n_bins + 1)
    rows = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (y_pred >= lo) & (y_pred < hi)
        if mask.sum() == 0:
            continue
        n = int(mask.sum())
        rows.append({
            "bin_low":         round(float(lo), 2),
            "bin_high":        round(float(hi), 2),
            "n":               n,
            "mean_predicted":  round(float(y_pred[mask].mean()), 4),
            "actual_win_rate": round(float(y_true[mask].mean()), 4),
            "flag_sparse":     n < _CALIB_MIN_BIN_N,
        })
    return rows


# ---------------------------------------------------------------------------
# Integrity checks
# ---------------------------------------------------------------------------

def run_integrity_checks(shadow_log_path: Path) -> list[dict]:
    """Run output integrity checks on shadow_log.csv (not shadow_eval).

    Returns list of {check, passed, detail}.
    """
    results = []

    if not shadow_log_path.exists():
        results.append({
            "check": "shadow_log_exists",
            "passed": False,
            "detail": f"File not found: {shadow_log_path}",
        })
        return results

    try:
        df = pd.read_csv(shadow_log_path)
    except Exception as exc:
        results.append({
            "check": "shadow_log_readable",
            "passed": False,
            "detail": str(exc),
        })
        return results

    results.append({"check": "shadow_log_exists", "passed": True, "detail": "OK"})

    if df.empty:
        results.append({"check": "shadow_log_nonempty", "passed": False, "detail": "Empty file"})
        return results

    results.append({"check": "shadow_log_nonempty", "passed": True, "detail": f"{len(df)} rows"})

    prob_col = "served_win_prob"

    # 1. Prob sum per race ≈ 1
    race_sums = df.groupby("race_id")[prob_col].sum()
    bad_sums  = race_sums[(race_sums < 0.98) | (race_sums > 1.02)]
    results.append({
        "check":  "prob_sum_near_1",
        "passed": len(bad_sums) == 0,
        "detail": f"OK ({len(race_sums)} races)" if bad_sums.empty
                  else f"{len(bad_sums)} races with sum outside [0.98, 1.02]",
    })

    # 2. No prob <= 0 or >= 1
    bad_range = df[(df[prob_col] <= 0) | (df[prob_col] >= 1)]
    results.append({
        "check":  "probs_in_open_interval",
        "passed": bad_range.empty,
        "detail": "OK" if bad_range.empty
                  else f"{len(bad_range)} starters with prob outside (0, 1)",
    })

    # 3. No NaN or Inf
    prob_arr   = df[prob_col].values.astype(float)
    bad_finite = (~np.isfinite(np.nan_to_num(prob_arr, nan=np.nan))).sum()
    results.append({
        "check":  "no_nan_inf",
        "passed": int(bad_finite) == 0,
        "detail": "OK" if bad_finite == 0 else f"{bad_finite} NaN/Inf served_win_prob values",
    })

    # 4. Rank consistent with probabilities
    rank_ok   = True
    bad_races = []
    for rid, grp in df.groupby("race_id"):
        computed = grp[prob_col].rank(ascending=False, method="first").astype(int)
        stored   = grp["served_rank"].astype(int)
        if not (computed.values == stored.values).all():
            rank_ok = False
            bad_races.append(str(rid))
    results.append({
        "check":  "rank_consistent_with_probs",
        "passed": rank_ok,
        "detail": "OK" if rank_ok
                  else f"{len(bad_races)} races with rank/prob mismatch",
    })

    # 5. No flatlined identical probabilities across entire race (field_size > 1)
    flatlines = []
    for rid, grp in df.groupby("race_id"):
        if len(grp) > 1 and grp[prob_col].nunique() == 1:
            flatlines.append(str(rid))
    results.append({
        "check":  "no_flatlined_probs",
        "passed": len(flatlines) == 0,
        "detail": "OK" if not flatlines
                  else f"{len(flatlines)} races with all-identical probabilities",
    })

    # 6. Fair odds finite for all non-Derby starters
    non_derby = df[df.get("derby_override_flag", pd.Series(0, index=df.index)) == 0]
    if not non_derby.empty:
        fair = np.where(
            non_derby[prob_col] > 0,
            1.0 / np.maximum(non_derby[prob_col].values, 1e-9) - 1.0,
            np.nan,
        )
        bad_fo = (~np.isfinite(fair)).sum()
        results.append({
            "check":  "fair_odds_finite",
            "passed": int(bad_fo) == 0,
            "detail": "OK" if bad_fo == 0 else f"{bad_fo} non-finite fair_odds",
        })

    return results


# ---------------------------------------------------------------------------
# Segment sufficiency check
# ---------------------------------------------------------------------------

def _check_segment_sufficiency(segment_rows: list[dict]) -> list[dict]:
    """Return a list of insufficient-segment records.

    A segment is insufficient if it has fewer than _MIN_RACES_SEGMENT races
    or fewer than _MIN_WINNERS_SEGMENT winners.
    """
    insufficient = []
    for row in segment_rows:
        reasons = []
        n_races   = row.get("n_races", 0) or 0
        n_winners = row.get("n_winners", 0) or 0
        if n_races < _MIN_RACES_SEGMENT:
            reasons.append(
                f"only {n_races} races (need >= {_MIN_RACES_SEGMENT})"
            )
        if n_winners < _MIN_WINNERS_SEGMENT:
            reasons.append(
                f"only {n_winners} winners (need >= {_MIN_WINNERS_SEGMENT})"
            )
        if reasons:
            insufficient.append({
                "segment":   row["segment"],
                "n_races":   n_races,
                "n_winners": n_winners,
                "reasons":   "; ".join(reasons),
            })
    return insufficient


# ---------------------------------------------------------------------------
# Promotion logic
# ---------------------------------------------------------------------------

def _promotion_decision(
    overall_h: dict,
    overall_ml: dict,
    segment_rows: list[dict],
    integrity_checks: list[dict],
    insufficient_segments: list[dict],
) -> dict:
    """Return promotion decision dict with decision and reasons list."""
    reasons_pass = []
    reasons_fail = []
    reasons_hold = []

    # Integrity gate
    integrity_ok = all(c["passed"] for c in integrity_checks)
    if not integrity_ok:
        failed = [c["check"] for c in integrity_checks if not c["passed"]]
        reasons_fail.append(f"Integrity checks failed: {', '.join(failed)}")

    if overall_ml is None:
        reasons_fail.append("No ML metrics available (ml_win_prob missing or no outcomes)")
        return {
            "decision": "FAIL",
            "reasons":  reasons_fail,
            "ll_improvement_pct":    None,
            "brier_improvement_pct": None,
        }

    n_races_overall = overall_ml.get("n_races", 0)

    # Overall data sufficiency — must pass before any other decision
    if n_races_overall < _MIN_RACES_OVERALL:
        return {
            "decision": "INSUFFICIENT_DATA",
            "reasons": [
                f"Only {n_races_overall} races with outcomes (need >= {_MIN_RACES_OVERALL} for a valid promotion decision)"
            ],
            "ll_improvement_pct":    None,
            "brier_improvement_pct": None,
        }

    ll_h  = overall_h.get("log_loss")
    ll_ml = overall_ml.get("log_loss")
    br_h  = overall_h.get("brier")
    br_ml = overall_ml.get("brier")

    ll_imp    = _pct_improvement(ll_h,  ll_ml)  if ll_h  and ll_ml  else None
    brier_imp = _pct_improvement(br_h,  br_ml)  if br_h  and br_ml  else None

    # Insufficient segments → note in hold reasons (never blocks FAIL)
    for insuf in insufficient_segments:
        reasons_hold.append(
            f"Segment '{insuf['segment']}' is insufficient: {insuf['reasons']}"
        )

    # Segment degradation check
    seg_fail = False
    for row in segment_rows:
        delta = row.get("ll_delta_pct")
        if delta is not None and delta < -_SEGMENT_DEGRADE_THRESHOLD:
            reasons_fail.append(
                f"Segment '{row['segment']}' log loss degraded by {abs(delta):.1f}%"
                f" (> {_SEGMENT_DEGRADE_THRESHOLD}% threshold)"
            )
            seg_fail = True

    # Pass criteria
    if ll_imp is not None and ll_imp >= _LL_IMPROVE_PCT_THRESHOLD:
        reasons_pass.append(
            f"Log loss improved {ll_imp:.1f}% overall (>= {_LL_IMPROVE_PCT_THRESHOLD}% threshold)"
        )
    else:
        reasons_hold.append(
            f"Log loss improvement {ll_imp:.1f}% is below {_LL_IMPROVE_PCT_THRESHOLD}% threshold"
            if ll_imp is not None else "Log loss improvement could not be computed"
        )

    if brier_imp is not None and brier_imp >= _BRIER_IMPROVE_PCT_THRESHOLD:
        reasons_pass.append(
            f"Brier score improved {brier_imp:.1f}% overall (>= {_BRIER_IMPROVE_PCT_THRESHOLD}% threshold)"
        )
    else:
        reasons_hold.append(
            f"Brier improvement {brier_imp:.1f}% is below {_BRIER_IMPROVE_PCT_THRESHOLD}% threshold"
            if brier_imp is not None else "Brier improvement could not be computed"
        )

    # Final decision
    if reasons_fail or not integrity_ok or seg_fail:
        decision    = "FAIL"
        all_reasons = reasons_fail
    elif len(reasons_pass) >= 2 and not reasons_hold:
        decision    = "PASS"
        all_reasons = reasons_pass
    else:
        decision    = "HOLD"
        all_reasons = reasons_hold + (reasons_pass if reasons_pass else [])

    return {
        "decision":              decision,
        "reasons":               all_reasons,
        "ll_improvement_pct":    round(ll_imp, 2)    if ll_imp    is not None else None,
        "brier_improvement_pct": round(brier_imp, 2) if brier_imp is not None else None,
    }


# ---------------------------------------------------------------------------
# Main evaluation runner
# ---------------------------------------------------------------------------

def run_evaluation(
    eval_file: Optional[Path] = None,
    out_dir:   Optional[Path] = None,
) -> dict:
    """Load shadow_eval.csv, compute metrics, write artifacts, return summary dict."""
    eval_path = Path(eval_file) if eval_file else _SHADOW_EVAL
    if not eval_path.exists():
        _log.error("Eval file not found: %s — run backfill_shadow_eval first", eval_path)
        return {}

    df = pd.read_csv(eval_path)
    if df.empty:
        _log.error("Eval file is empty: %s", eval_path)
        return {}

    # Require outcome columns
    df["win_flag"]   = pd.to_numeric(df.get("win_flag"),   errors="coerce")
    df["finish_pos"] = pd.to_numeric(df.get("finish_pos"), errors="coerce")
    df["off_odds"]   = pd.to_numeric(df.get("off_odds"),   errors="coerce")

    # Work only on rows that have a win_flag label
    labeled = df[df["win_flag"].notna()].copy()
    if labeled.empty:
        _log.warning("No labeled rows (win_flag) in eval dataset — cannot compute metrics")
        return {}

    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    if out_dir is None:
        out_dir = _OUTPUT / f"eval_run_{ts}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Overall metrics ─────────────────────────────────────────────────────
    m_heuristic = _compute_metrics(labeled, "heuristic_win_prob")
    m_ml        = None
    if "ml_win_prob" in labeled.columns and labeled["ml_win_prob"].notna().sum() > 0:
        m_ml = _compute_metrics(labeled, "ml_win_prob")

    # ── Segment metrics ──────────────────────────────────────────────────────
    segment_rows = []
    if "segment" in labeled.columns:
        for seg, grp in labeled.groupby("segment"):
            sh = _compute_metrics(grp, "heuristic_win_prob")
            sm = _compute_metrics(grp, "ml_win_prob") if "ml_win_prob" in grp.columns else None
            if sh is None:
                continue
            ll_delta = None
            if sm and sh.get("log_loss") and sm.get("log_loss"):
                ll_delta = round(_pct_improvement(sh["log_loss"], sm["log_loss"]), 2)
            segment_rows.append({
                "segment":           seg,
                "n_races":           sh["n_races"],
                "n_starters":        sh["n_starters"],
                "n_winners":         sh.get("n_winners", 0),
                "h_log_loss":        sh.get("log_loss"),
                "ml_log_loss":       sm.get("log_loss") if sm else None,
                "ll_delta_pct":      ll_delta,
                "h_brier":           sh.get("brier"),
                "ml_brier":          sm.get("brier") if sm else None,
                "h_top1_hit_rate":   sh.get("top1_hit_rate"),
                "ml_top1_hit_rate":  sm.get("top1_hit_rate") if sm else None,
            })

    # ── Segment sufficiency ──────────────────────────────────────────────────
    insufficient_segments = _check_segment_sufficiency(segment_rows)

    # ── Calibration bins ────────────────────────────────────────────────────
    calib_rows = []
    y_true_all = labeled["win_flag"].astype(int).values
    for col, label in (("heuristic_win_prob", "heuristic"), ("ml_win_prob", "ml")):
        if col not in labeled.columns:
            continue
        valid_mask = labeled[col].notna()
        if valid_mask.sum() == 0:
            continue
        y_pred = labeled.loc[valid_mask, col].astype(float).values
        y_t    = labeled.loc[valid_mask, "win_flag"].astype(int).values
        for b in _calibration_bins(y_t, y_pred):
            b["model"] = label
            calib_rows.append(b)

    # ── Integrity checks ────────────────────────────────────────────────────
    shadow_log = _OUTPUT / "shadow_log.csv"
    integrity  = run_integrity_checks(shadow_log)

    # ── Promotion decision ──────────────────────────────────────────────────
    decision_dict = _promotion_decision(
        overall_h=m_heuristic or {},
        overall_ml=m_ml,
        segment_rows=segment_rows,
        integrity_checks=integrity,
        insufficient_segments=insufficient_segments,
    )

    # ── Write artifacts ──────────────────────────────────────────────────────

    # metrics_summary.json
    summary = {
        "evaluated_at":    datetime.datetime.utcnow().isoformat() + "Z",
        "eval_file":       str(eval_path),
        "n_total_rows":    int(len(df)),
        "n_labeled_rows":  int(len(labeled)),
        "n_races_total":   int(labeled["race_id"].nunique()) if "race_id" in labeled.columns else None,
        "heuristic":       m_heuristic,
        "ml":              m_ml,
        "ll_improvement_pct":    decision_dict.get("ll_improvement_pct"),
        "brier_improvement_pct": decision_dict.get("brier_improvement_pct"),
    }
    (out_dir / "metrics_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )

    # segment_metrics.csv
    pd.DataFrame(segment_rows if segment_rows else []).to_csv(
        out_dir / "segment_metrics.csv", index=False
    )

    # insufficient_segments.csv
    pd.DataFrame(insufficient_segments if insufficient_segments else []).to_csv(
        out_dir / "insufficient_segments.csv", index=False
    )

    # calibration_table.csv
    pd.DataFrame(calib_rows if calib_rows else []).to_csv(
        out_dir / "calibration_table.csv", index=False
    )

    # integrity_checks.json (embedded in promotion_decision.json too)
    integrity_passed = all(c["passed"] for c in integrity)

    # promotion_decision.json
    promotion = {
        "decision":                 decision_dict["decision"],
        "reasons":                  decision_dict["reasons"],
        "ll_improvement_pct":       decision_dict.get("ll_improvement_pct"),
        "brier_improvement_pct":    decision_dict.get("brier_improvement_pct"),
        "integrity_checks_passed":  integrity_passed,
        "integrity_checks":         integrity,
        "insufficient_segments":    insufficient_segments,
        "evaluated_at":             datetime.datetime.utcnow().isoformat() + "Z",
        "eval_file":                str(eval_path),
        "recommended_action":       _recommended_action(decision_dict["decision"]),
        "thresholds": {
            "min_races_overall":   _MIN_RACES_OVERALL,
            "min_races_segment":   _MIN_RACES_SEGMENT,
            "min_winners_segment": _MIN_WINNERS_SEGMENT,
            "ll_improve_pct":      _LL_IMPROVE_PCT_THRESHOLD,
            "brier_improve_pct":   _BRIER_IMPROVE_PCT_THRESHOLD,
            "segment_degrade_pct": _SEGMENT_DEGRADE_THRESHOLD,
        },
    }
    (out_dir / "promotion_decision.json").write_text(
        json.dumps(promotion, indent=2, default=str), encoding="utf-8"
    )

    # Copy join_diagnostics and unmatched rows from output/ if they exist
    for fname in ("join_diagnostics.json", "unmatched_shadow_rows.csv"):
        src = _OUTPUT / fname
        if src.exists():
            shutil.copy2(src, out_dir / fname)

    _log.info("Artifacts written to %s", out_dir)
    _log.info("Decision: %s", decision_dict["decision"])
    for r in decision_dict["reasons"]:
        _log.info("  • %s", r)

    return {"summary": summary, "promotion": promotion, "out_dir": str(out_dir)}


def _recommended_action(decision: str) -> str:
    return {
        "PASS":             "promote to live — set DERBYEDGE_ML_MODE=live",
        "HOLD":             "remain in shadow — continue collecting data",
        "FAIL":             "roll back and retrain — investigate degraded segments",
        "INSUFFICIENT_DATA": (
            f"remain in shadow — need >= {_MIN_RACES_OVERALL} labeled races overall"
        ),
    }.get(decision, "unknown")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate ML vs heuristic from shadow log data"
    )
    parser.add_argument(
        "--eval-file", default=None,
        help=f"Path to shadow_eval.csv (default: {_SHADOW_EVAL})",
    )
    parser.add_argument(
        "--out-dir", default=None,
        help="Directory for output artifacts (default: output/eval_run_TIMESTAMP)",
    )
    args = parser.parse_args()

    result = run_evaluation(
        eval_file=args.eval_file,
        out_dir=args.out_dir,
    )

    if not result:
        print("Evaluation failed — check logs above.")
        sys.exit(1)

    promotion = result["promotion"]
    summary   = result["summary"]
    out_dir   = result["out_dir"]

    print()
    print("=" * 60)
    print(f"PROMOTION DECISION: {promotion['decision']}")
    print("=" * 60)
    for r in promotion["reasons"]:
        print(f"  • {r}")
    print()
    print(f"Recommended action: {promotion['recommended_action']}")
    print()

    h = summary.get("heuristic") or {}
    m = summary.get("ml")        or {}
    print(f"{'Metric':<22}  {'Heuristic':>12}  {'ML':>12}")
    print("-" * 50)
    for k in ("log_loss", "brier", "top1_hit_rate", "winner_top3", "avg_winner_rank"):
        hv = h.get(k, "—") if h else "—"
        mv = m.get(k, "—") if m else "—"
        print(f"  {k:<20}  {str(hv):>12}  {str(mv):>12}")
    print()

    insuf = promotion.get("insufficient_segments", [])
    if insuf:
        print("Insufficient segments (thin data — treat their metrics with caution):")
        for s in insuf:
            print(f"  • {s['segment']}: {s['reasons']}")
        print()

    print(f"Artifacts written to: {out_dir}")
    print(f"  • metrics_summary.json")
    print(f"  • segment_metrics.csv")
    print(f"  • insufficient_segments.csv")
    print(f"  • calibration_table.csv")
    print(f"  • promotion_decision.json")
    print(f"  • join_diagnostics.json        (if backfill_shadow_eval was run)")
    print(f"  • unmatched_shadow_rows.csv    (if backfill_shadow_eval was run)")


if __name__ == "__main__":
    main()
