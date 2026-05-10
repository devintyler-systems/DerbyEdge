"""
DerbyEdge V1  —  Scorer
src/models/scorer.py

Bet-tag thresholds:
  bet     : model_edge >= +0.025
  underlay: model_edge <  -0.015
  neutral : -0.015 <= model_edge < +0.025

Confidence tiers (4-component scored system — see src/models/confidence.py):
  high   : score >= 0.70
  medium : 0.45 <= score < 0.70
  low    : score < 0.45

Score = 0.35*A(horse evidence) + 0.25*B(race evidence)
      + 0.30*C(model certainty) + 0.10*D(calibration)

Sparse distance history alone no longer forces LOW when other signals are strong.
Missing-data flags are per-horse text labels (CRITICAL_MISSING + dist_fit_single_start).
"""

import datetime
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from src.models.confidence import (
    compute_horse_confidence,
    legacy_missing_flags,
    CRITICAL_MISSING,
    DERBY_EXTRA_MISSING,
)
from src.models.trainer import (
    ModelArtifact,
    TRAIN_CONFIGS,
    DERBY_TRAIN_CONFIG,
    compute_feature_importances,
    compute_group_scores,
    register_model,
    save_artifact,
    train_or_build,
    build_seed_baseline,
)
from src.utils.db import (
    get_connection,
    get_derby_card_id,
    ensure_entry_scores_columns,
)

ROOT       = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "output"

# Derby context detection criteria
_DERBY_CRITERIA = {
    "surface":           "dirt",
    "min_furlongs":      9.5,      # 1.25 miles = 10f; allow slight tolerance
    "min_field_size":    18,
    "stakes_contains":   "derby",  # case-insensitive substring
    "track_abbrev":      "CD",     # Churchill Downs
}

# Feature-catalog tier lookup (for diagnostic footer)
_FEATURE_TIER = {
    "speed_best_3": "DEGRADED", "speed_last": "IMPLEMENTED",
    "pace_fit_score": "IMPLEMENTED", "distance_fit": "DEGRADED",
    "surface_fit": "DEGRADED", "derby_override_score": "DEGRADED",
    "work_readiness_score": "DEGRADED", "form_cycle_idx": "DEGRADED",
    "beyer_last": "IMPLEMENTED", "class_delta": "DEGRADED",
    "traffic_resilience_proxy": "DEGRADED", "market_implied_prob": "IMPLEMENTED",
    "trainer_intent_proxy": "DEGRADED", "horses_beaten_pct_last": "DEGRADED",
    "career_win_pct": "IMPLEMENTED", "finish_energy_proxy": "DEGRADED",
}


_DERBY_DEFAULT_CHAOS_INDEX = 0.85   # default for scorer; UI slider default matches


# ---------------------------------------------------------------------------
# Schema guards — add new columns if missing (idempotent, called before writes)
# ---------------------------------------------------------------------------
def _ensure_chaos_columns(conn: sqlite3.Connection) -> None:
    for stmt in (
        "ALTER TABLE score_runs   ADD COLUMN chaos_active        INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE score_runs   ADD COLUMN chaos_intensity     REAL",
        "ALTER TABLE score_runs   ADD COLUMN field_entropy_score REAL",
        "ALTER TABLE entry_scores ADD COLUMN chaos_score         REAL",
        "ALTER TABLE entry_scores ADD COLUMN chaos_boost         REAL",
        "ALTER TABLE entry_scores ADD COLUMN chaos_tier          TEXT",
        "ALTER TABLE entry_scores ADD COLUMN chaos_eligible      INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()




# ---------------------------------------------------------------------------
# Chaos pipeline — maps scorer arrays to chaos patch inputs, returns per-entry
# outputs.  Returns zero-impact values when derby_active=False or patch fails.
# ---------------------------------------------------------------------------
def _chaos_outputs_for_run(
    entries_df:    pd.DataFrame,
    feat_df:       pd.DataFrame,
    win_probs:     np.ndarray,
    form_arr:      np.ndarray,
    surf_dist_arr: np.ndarray,
    derby_active:  bool,
    chaos_index:   float = _DERBY_DEFAULT_CHAOS_INDEX,
) -> tuple[np.ndarray, np.ndarray, list, np.ndarray, bool, float]:
    """Return (chaos_score, chaos_boost, chaos_tier_list, chaos_eligible,
               chaos_was_applied, chaos_intensity).
    chaos_score = WinProb_final per entry (equals win_probs when inactive)
    chaos_boost = WinProb_final − WinProb_base (0.0 when inactive)
    """
    n = len(win_probs)
    _zero = (win_probs.copy(), np.zeros(n), ["none"] * n,
             np.zeros(n, dtype=int), False, 0.0)
    if not derby_active or n == 0:
        return _zero

    def _col(df: pd.DataFrame, name: str, default: float) -> np.ndarray:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce").fillna(default).values
        return np.full(n, default, dtype=float)

    ch = pd.DataFrame(index=range(n))
    ch["WinProb_base"]       = win_probs
    ch["PaceFit_score"]      = _col(feat_df, "pace_fit_score",    0.5) * 10.0
    ch["DevCurve_score"]     = form_arr * 10.0
    ch["FinishEnergy_score"] = form_arr * 10.0
    ch["DistanceProj_score"] = surf_dist_arr * 10.0

    eq  = 1.0 / max(n, 1)
    mkt = _col(feat_df, "market_implied_prob", eq)
    ch["Publicness_score"] = np.clip(
        5.0 + 2.5 * np.log2(np.maximum(mkt / eq, 1e-6)), 0.0, 10.0
    )

    last_spd = _col(feat_df, "last_speed_fig", 0.0)
    avg_spd  = _col(feat_df, "avg_speed_fig",  0.0)
    std_spd  = float(np.std(last_spd)) if np.std(last_spd) > 0 else 1.0
    ch["late_fig_z"] = (last_spd - avg_spd) / std_spd

    ps_arr = (
        entries_df["pace_style"].fillna("stalker").values
        if "pace_style" in entries_df.columns
        else np.full(n, "stalker")
    )
    pp_arr = _col(entries_df, "post_position", 10.0)
    med_pp = float(np.median(pp_arr))
    ch["FavRailCloserFlag"]    = (ps_arr == "closer").astype(int)
    ch["FavTacticalInnerFlag"] = np.array(
        [1 if (ps_arr[i] == "presser" and pp_arr[i] <= med_pp) else 0 for i in range(n)]
    )
    ch["FavTacticalOuterFlag"] = np.array(
        [1 if (ps_arr[i] == "front" or (ps_arr[i] == "presser" and pp_arr[i] > med_pp))
         else 0 for i in range(n)]
    )

    try:
        from src.derbyedge.chaos_patch import apply_derby_chaos_patch, realloc_target
        patched = apply_derby_chaos_patch(ch, chaos_index=chaos_index)
        return (
            patched["WinProb_final"].values,
            (patched["WinProb_final"] - patched["WinProb_base"]).values,
            patched["DarkHorseTier"].tolist(),
            patched["DarkHorseFlag"].astype(int).values,
            True,
            float(realloc_target(chaos_index)),
        )
    except Exception as exc:
        print(f"  [scorer]   chaos patch skipped: {exc!r}")
        return _zero


# ---------------------------------------------------------------------------
# Derby context detection
# ---------------------------------------------------------------------------
def is_derby_context(conn, card_id: int) -> bool:
    """
    Return True when the race card matches all Derby context criteria:
    dirt, >= 9.5 furlongs, >= 18 runners, stakes name contains 'derby',
    at Churchill Downs (abbrev 'CD').
    """
    row = conn.execute(
        """
        SELECT rc.surface, rc.distance_furlongs, rc.field_size,
               rc.stakes_name, t.abbrev AS track_abbrev
        FROM race_cards rc
        JOIN tracks t ON rc.track_id = t.track_id
        WHERE rc.card_id = ?
        """,
        (card_id,),
    ).fetchone()
    if not row:
        return False
    return (
        str(row["surface"] or "") == _DERBY_CRITERIA["surface"]
        and float(row["distance_furlongs"] or 0) >= _DERBY_CRITERIA["min_furlongs"]
        and int(row["field_size"] or 0) >= _DERBY_CRITERIA["min_field_size"]
        and _DERBY_CRITERIA["stakes_contains"] in str(row["stakes_name"] or "").lower()
        and str(row["track_abbrev"] or "") == _DERBY_CRITERIA["track_abbrev"]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _place_show_probs(win_probs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n     = len(win_probs)
    place = 0.45 * win_probs + 0.55 * (1.0 / n)
    show  = 0.35 * win_probs + 0.65 * (1.0 / n)
    return place / place.sum(), show / show.sum()


def _bet_tag(edge: float, bet_threshold: float, underlay_threshold: float) -> str:
    if edge >= bet_threshold:
        return "bet"
    if edge < underlay_threshold:
        return "underlay"
    return "neutral"


pass  # _model_confidence / _missing_flags / _compute_confidence_and_flags
# removed — replaced by src.models.confidence.compute_horse_confidence


# ---------------------------------------------------------------------------
# Pre-race evaluation metrics
# ---------------------------------------------------------------------------
def _compute_metrics(
    win_probs:    np.ndarray,
    market_probs: np.ndarray,
    artifact:     ModelArtifact,
) -> dict:
    from scipy.stats import kendalltau

    tau, _  = kendalltau(win_probs, market_probs)
    edges   = win_probs - market_probs
    bet_thr = artifact.config["bet_edge_threshold"]
    ul_thr  = artifact.config["underlay_edge_threshold"]

    # KL divergence: KL(model || market)
    kl = float(np.sum(
        win_probs * np.log(np.maximum(win_probs / np.maximum(market_probs, 1e-9), 1e-9))
    ))

    return {
        "model_type":        artifact.model_type,
        "race_type_key":     artifact.race_type_key,
        "training_rows":     artifact.training_rows,
        "temperature":       artifact.temperature,
        "sum_win_prob":      round(float(win_probs.sum()), 6),
        "kendall_tau_vs_ml": round(float(tau), 4),
        "kl_div_vs_ml":      round(kl, 4),
        "mean_edge_abs":     round(float(np.abs(edges).mean()), 4),
        "max_positive_edge": round(float(edges.max()), 4),
        "max_negative_edge": round(float(edges.min()), 4),
        "bet_count":         int((edges >= bet_thr).sum()),
        "underlay_count":    int((edges < ul_thr).sum()),
        # Outcome-based: not available until race is run
        "log_loss":          None,
        "brier_score":       None,
        "calibration_error": None,
        "top1_hit_rate":     None,
        "edge_roi":          None,
    }


# ---------------------------------------------------------------------------
# Board writers
# ---------------------------------------------------------------------------
def _write_board(
    board:       pd.DataFrame,
    run_id:      str,
    model_id:    int,
    artifact:    ModelArtifact,
    metrics:     dict,
    score_ts:    str,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── CSV ────────────────────────────────────────────────────────────────
    csv_cols = [
        "rank", "horse_name", "post_position",
        "trainer", "jockey",
        "morning_line_odds",
        "model_win_prob_pct", "fair_odds",
        "pace_fit_score", "form_score", "surface_dist_fit",
        "value_score", "bet_tag", "low_conf_bet_block",
        "model_confidence", "missing_data_flags",
    ]
    board[csv_cols].to_csv(OUTPUT_DIR / "derby_2026_board.csv", index=False)

    # ── Markdown ───────────────────────────────────────────────────────────
    bet_horses = board[board["bet_tag"] == "bet"]["horse_name"].tolist()
    ul_horses  = board[board["bet_tag"] == "underlay"]["horse_name"].tolist()
    low_conf   = int((board["model_confidence"] == "low").sum())
    top_row    = board[board["rank"] == 1].iloc[0]
    top_value  = board.nlargest(1, "value_score").iloc[0]
    bet_str    = ", ".join(bet_horses) if bet_horses else "none"
    ul_str     = ", ".join(ul_horses)  if ul_horses  else "none"

    tag_icons  = {"bet": "**BET**", "underlay": "~~UL~~", "neutral": "--"}
    conf_icons = {"high": "HIGH", "medium": "MED", "low": "LOW!"}
    blocked_n  = int(board.get("low_conf_bet_block", pd.Series(dtype=int)).sum()) \
                 if "low_conf_bet_block" in board.columns else 0
    blocked_horses = board[board.get("low_conf_bet_block", pd.Series(dtype=int)) == 1]["horse_name"].tolist() \
                     if "low_conf_bet_block" in board.columns else []

    lines = [
        "# DerbyEdge Engine — 2026 Kentucky Derby Board",
        "",
        "## Board Summary",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Model type | `{artifact.model_type}` |",
        f"| Version | `{artifact.version}` |",
        f"| Score timestamp | {score_ts} |",
        f"| Model ID | {model_id} |",
        f"| Race | 2026 Kentucky Derby (G1) · Churchill Downs · 2026-05-02 |",
        f"| Total horses | {len(board)} |",
        f"| Bet-tagged | {metrics['bet_count']} ({bet_str}) |",
        f"| Underlay-tagged | {metrics['underlay_count']} ({ul_str}) |",
        f"| Low-conf BET blocked | {blocked_n} ({', '.join(blocked_horses) if blocked_horses else 'none'}) |",
        f"| Top win probability | {top_row['horse_name']} {top_row['model_win_prob_pct']:.1f}% "
        f"(fair {top_row['fair_odds']:.1f}-1) |",
        f"| Top value score | {top_value['horse_name']} "
        f"{'+' if top_value['value_score'] > 0 else ''}{top_value['value_score']:.3f} "
        f"({top_value['bet_tag']}) |",
        f"| Kendall tau vs market | {metrics['kendall_tau_vs_ml']:.4f} |",
        f"| Mean abs edge | {metrics['mean_edge_abs']:.4f} |",
        f"| Low-confidence entries | {low_conf} of {len(board)} "
        f"(score < 0.45 — see confidence_reasons per entry) |",
        "",
        "---",
        "",
        "## Race Card",
        "",
        "**Bet thresholds:** BET >= +0.025  |  UNDERLAY < -0.015  |  NEUTRAL otherwise",
        "",
        "| Rank | Horse | Post | Trainer | Jockey | ML | Win% | Fair | PaceFit | Form | SuDist | Edge | Tag | Conf |",
        "|------|-------|------|---------|--------|----|------|------|---------|------|--------|------|-----|------|",
    ]

    for _, r in board.iterrows():
        edge_str = f"+{r['value_score']:.3f}" if r['value_score'] > 0 else f"{r['value_score']:.3f}"
        conf_str = conf_icons.get(r['model_confidence'], r['model_confidence'])
        tag_str  = "--[B]" if r.get("low_conf_bet_block", 0) else tag_icons.get(r['bet_tag'], r['bet_tag'])
        lines.append(
            f"| {int(r['rank'])} | **{r['horse_name']}** | {int(r['post_position'])} "
            f"| {r['trainer']} | {r['jockey']} "
            f"| {r['morning_line_odds']:.0f}-1 "
            f"| {r['model_win_prob_pct']:.1f}% "
            f"| {r['fair_odds']:.1f}-1 "
            f"| {r['pace_fit_score']:.3f} "
            f"| {r['form_score']:.3f} "
            f"| {r['surface_dist_fit']:.3f} "
            f"| {edge_str} "
            f"| {tag_str} "
            f"| {conf_str} |"
        )

    # ── Low-confidence detail ─────────────────────────────────────────────
    low_conf_horses = board[board["model_confidence"] == "low"]
    if not low_conf_horses.empty:
        lines += [
            "",
            "### Low-Confidence Entries",
            "",
            "These horses scored < 0.45 on the 4-component confidence system "
            "(horse evidence × 0.35, race evidence × 0.25, model certainty × 0.30, "
            "calibration × 0.10).",
            "",
            "| Horse | Post | Score | Reasons |",
            "|-------|------|-------|---------|",
        ]
        for _, r in low_conf_horses.iterrows():
            score   = r.get("confidence_score", 0.0)
            reasons = r.get("confidence_reasons", "—")
            lines.append(
                f"| {r['horse_name']} | {int(r['post_position'])} "
                f"| {score:.3f} | {reasons} |"
            )

    # ── Diagnostic footer ──────────────────────────────────────────────────
    fi = sorted(artifact.feature_importances.items(), key=lambda x: -x[1])[:5]

    lines += [
        "",
        "---",
        "",
        "## Diagnostics",
        "",
        "### Feature Tier Summary",
        "",
        "| Tier | Count | Meaning |",
        "|------|-------|---------|",
        "| IMPLEMENTED | 22 | Computed directly from seed columns |",
        "| DEGRADED | 12 | Proxy formula from aggregate seed data; less precise than row-level history |",
        "| PLACEHOLDER | 12 | Null; requires horse_starts / workouts / track_bias / trip_flags |",
        "",
        "### Top 5 Feature Importances",
        "",
        "| Feature | Weight | Tier |",
        "|---------|--------|------|",
    ]
    for fname, fw in fi:
        tier = _FEATURE_TIER.get(fname, "DEGRADED")
        lines.append(f"| `{fname}` | {fw:.4f} | {tier} |")

    lines += [
        "",
        "### Calibration",
        "",
        "| Parameter | Value |",
        "|-----------|-------|",
        f"| Method | temperature-scaled softmax |",
        f"| Temperature | {artifact.temperature} |",
        f"| Calibration target | overround-adjusted morning line |",
        f"| Sum of win probabilities | {metrics['sum_win_prob']:.6f} |",
        f"| KL divergence vs market | {metrics['kl_div_vs_ml']:.4f} |",
        "",
        "### Model Limitations",
        "",
        "> This baseline uses seed-aggregate features and has not been validated on historical Derby preps.",
        "> Fair odds and value scores are **directional only**.",
        "> The following features are unavailable until real historical data is loaded:",
        "> race-by-race speed splits, bullet workout counts, trainer/jockey conditioned stats,",
        "> Churchill Downs track form, post-position win bias, trip trouble flags.",
        ">",
        "> **Do not wager without manual audit of speed figures, trip notes, and trainer intent.**",
        "",
        "### Low-Confidence BET Guardrail",
        "",
        "> Low-confidence entries (`conf == LOW`) with a raw edge ≥ +0.025 are **NOT** auto-tagged BET.",
        "> Their apparent edge comes from the odds-floor vs market probability gap, not from model signal.",
        "> These entries are downgraded to `neutral` and flagged with `low_conf_bet_block = 1`.",
        "> Tag column shows `--[B]` for blocked entries.",
        "> To elevate after manual review, override the bet_tag in the database directly.",
    ]

    (OUTPUT_DIR / "derby_2026_board.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  [board]    board written -> {OUTPUT_DIR / 'derby_2026_board.md'}")


def _write_eval_report(
    metrics:   dict,
    artifact:  ModelArtifact,
    board:     pd.DataFrame,
    model_id:  int,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    race_type = metrics["race_type_key"]
    path      = OUTPUT_DIR / f"model_evaluation_{race_type}.md"

    quality = (
        "SEED-ONLY BASELINE — principled weighted composite from 46-feature "
        "catalog; no historical training data; probabilities are model-informed "
        "estimates, not calibrated predictions"
    )

    top5      = board.nsmallest(5, "rank")[
        ["rank", "horse_name", "model_win_prob_pct", "fair_odds", "value_score", "bet_tag"]
    ]
    top3_val  = board.nlargest(3, "value_score")[
        ["horse_name", "morning_line_odds", "model_win_prob_pct", "value_score", "bet_tag"]
    ]
    fi = sorted(artifact.feature_importances.items(), key=lambda x: -x[1])[:15]

    lines = [
        f"# DerbyEdge Model Evaluation — {race_type}",
        "",
        f"**Generated** : {metrics.get('score_ts', 'N/A')}  ",
        f"**Model name** : `{artifact.model_name}` (ID={model_id})  ",
        f"**Version**    : `{artifact.version}`  ",
        f"**Model type** : {metrics['model_type']}  ",
        "",
        "## Model Quality Assessment",
        "",
        f"> **{quality}**",
        "",
        "| Criterion | Status |",
        "|-----------|--------|",
        f"| Training rows | {metrics['training_rows']} (need >= 50 for XGBoost) |",
        f"| Calibration | temperature-scaled softmax (T={metrics['temperature']}) |",
        f"| Calibration target | overround-adjusted morning line |",
        f"| Bet threshold | edge >= +{artifact.config['bet_edge_threshold']:.3f} |",
        f"| Underlay threshold | edge < {artifact.config['underlay_edge_threshold']:.3f} |",
        f"| Outcome validation | NOT POSSIBLE — race not yet run (2026-05-02) |",
        "",
        "## Pre-Race Diagnostics",
        "",
        "| Metric | Value | Interpretation |",
        "|--------|-------|----------------|",
        f"| `sum_win_prob` | {metrics['sum_win_prob']:.6f} | Should be 1.000000 |",
        f"| `kendall_tau_vs_ml` | {metrics['kendall_tau_vs_ml']:.4f} | Rank correlation with market |",
        f"| `kl_div_vs_ml` | {metrics['kl_div_vs_ml']:.4f} | KL(model \\|\\| market) |",
        f"| `mean_edge_abs` | {metrics['mean_edge_abs']:.4f} | Mean abs model-market divergence |",
        f"| `max_positive_edge` | {metrics['max_positive_edge']:.4f} | Best value candidate |",
        f"| `max_negative_edge` | {metrics['max_negative_edge']:.4f} | Worst underlay |",
        f"| `bet_count` | {metrics['bet_count']} | Horses with edge >= +0.025 |",
        f"| `underlay_count` | {metrics['underlay_count']} | Horses with edge < -0.015 |",
        "",
        "## Post-Race Metrics (N/A — Race Not Run)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        "| log_loss | N/A |",
        "| brier_score | N/A |",
        "| calibration_error | N/A |",
        "| top1_hit_rate | N/A |",
        "| edge_bucket_roi | N/A |",
        "",
        "## Top Feature Importances",
        "",
        "| Rank | Feature | Weight | Tier |",
        "|------|---------|--------|------|",
    ]
    for i, (fname, fw) in enumerate(fi, 1):
        tier = _FEATURE_TIER.get(fname, "DEGRADED")
        lines.append(f"| {i} | `{fname}` | {fw:.4f} | {tier} |")

    lines += [
        "",
        "## Group Weights",
        "",
        "| Group | Weight |",
        "|-------|--------|",
    ]
    for gname, gdef in artifact.config["feature_groups"].items():
        lines.append(f"| {gname} | {gdef['group_weight']:.2f} |")

    lines += [
        "",
        "## Top 5 by Win Probability",
        "",
        "| Rank | Horse | Win% | Fair Odds | Edge | Tag |",
        "|------|-------|------|-----------|------|-----|",
    ]
    for _, r in top5.iterrows():
        edge_str = f"+{r['value_score']:.3f}" if r['value_score'] > 0 else f"{r['value_score']:.3f}"
        lines.append(
            f"| {int(r['rank'])} | {r['horse_name']} "
            f"| {r['model_win_prob_pct']:.1f}% "
            f"| {r['fair_odds']:.1f}-1 | {edge_str} | {r['bet_tag']} |"
        )

    lines += [
        "",
        "## Top 3 by Value Score",
        "",
        "| Horse | ML Odds | Win% | Edge | Tag |",
        "|-------|---------|------|------|-----|",
    ]
    for _, r in top3_val.iterrows():
        edge_str = f"+{r['value_score']:.3f}" if r['value_score'] > 0 else f"{r['value_score']:.3f}"
        lines.append(
            f"| {r['horse_name']} | {r['morning_line_odds']:.0f}-1 "
            f"| {r['model_win_prob_pct']:.1f}% | {edge_str} | {r['bet_tag']} |"
        )

    lines += [
        "",
        "## Limitations",
        "",
        "- **Seed-only**: no access to race-by-race speed splits, real workout records,",
        "  conditioned trainer/jockey stats, track bias, or trip flags.",
        "- 12/46 features are PLACEHOLDER (null for all entries).",
        "- 12/46 features are DEGRADED (proxy formulas from aggregate seed data).",
        "- Calibration is temperature-scaled softmax tuned to morning line spread;",
        "  NOT isotonic regression against actual race outcomes.",
        "- **Do not use for real-money wagering without historical validation.**",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [eval]     evaluation report -> {path}")


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------
def score_race(card_id: Optional[int] = None) -> pd.DataFrame:
    """
    Score a race card end-to-end.

    Stages:
      1. Load feature store + v_entries_live
      2. Build/load model artifact (seed_only_baseline when no history)
      3. Calibrated win probabilities -> fair_odds, model_edge, bet_tags
      4. Per-horse confidence and missing-data flags
      5. Write DB: score_runs + entry_scores
      6. Write outputs: board CSV/MD + evaluation MD

    Returns sorted board DataFrame (one row per entry, ranked by win_prob).
    """
    score_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection()

    if card_id is None:
        card_id = get_derby_card_id()
    if card_id is None:
        conn.close()
        raise RuntimeError("No Kentucky Derby card found — run ingest first.")

    # ── Load data ──────────────────────────────────────────────────────────
    entries_df = pd.read_sql(
        "SELECT * FROM v_entries_live WHERE card_id=? ORDER BY post_position",
        conn, params=(card_id,),
    )

    feat_df = pd.read_sql(
        "SELECT * FROM feature_store WHERE card_id=? ORDER BY post_position",
        conn, params=(card_id,),
    )
    if feat_df.empty:
        conn.close()
        raise RuntimeError(f"No features for card_id={card_id} — run build_features first.")

    # Filter feat_df to live (non-scratched) entries only.
    # v_entries_live already excludes scratches; we must align feat_df so
    # positional array indexing (win_probs[i], feat_df.iloc[i]) stays in sync.
    _live_eids = set(entries_df["entry_id"].astype(int))
    feat_df = (
        feat_df[feat_df["entry_id"].astype(int).isin(_live_eids)]
        .reset_index(drop=True)
    )
    if feat_df.empty:
        conn.close()
        raise RuntimeError(
            f"All entries are scratched or missing features for card_id={card_id}."
        )

    rc = conn.execute(
        "SELECT surface, distance_furlongs FROM race_cards WHERE card_id=?",
        (card_id,),
    ).fetchone()
    surface       = rc["surface"] if rc else "dirt"
    dist_furlongs = float(rc["distance_furlongs"]) if rc else 10.0
    race_type_key = f"{surface}_{'sprint' if dist_furlongs < 8.5 else 'route'}"

    # ── Derby override detection ───────────────────────────────────────────
    derby_active = is_derby_context(conn, card_id)
    print(f"  [scorer]   card_id={card_id}  race_type={race_type_key}  "
          f"entries={len(feat_df)}  derby_override={derby_active}")

    # ── Build model ────────────────────────────────────────────────────────
    if derby_active:
        # Use Derby-specific weight config; skip XGBoost path for Derby override
        artifact, win_probs = build_seed_baseline(feat_df, entries_df, DERBY_TRAIN_CONFIG)
        print(f"  [scorer]   Derby override active — using {DERBY_TRAIN_CONFIG['model_name']}")
    else:
        artifact, win_probs = train_or_build(
            feat_df=feat_df,
            entries_df=entries_df,
            race_type_key=race_type_key,
            conn=conn,
        )
    config = artifact.config

    # ── Sanitize win_probs — final gate before all downstream math ─────────
    _n_entries = len(feat_df)
    _n_nonfinite = int((~np.isfinite(win_probs)).sum())
    if _n_nonfinite or win_probs.sum() <= 0:
        print(
            f"[scorer] win_probs defaulted to uniform prior for {_n_entries} entries "
            f"due to non-finite model output ({_n_nonfinite} non-finite value(s))"
        )
        win_probs = np.full(_n_entries, 1.0 / _n_entries)
    else:
        win_probs = win_probs / win_probs.sum()   # normalize away any fp drift

    # ── Market probs (overround-adjusted) ─────────────────────────────────
    ml_implied = pd.to_numeric(
        feat_df["market_implied_prob"], errors="coerce"
    ).fillna(0.0).values
    ml_sum = ml_implied.sum()
    if ml_sum <= 0:
        market_probs = np.full(_n_entries, 1.0 / _n_entries)
    else:
        market_probs = ml_implied / ml_sum

    # ── Derived scoring ────────────────────────────────────────────────────
    fair_odds          = np.round(1.0 / np.maximum(win_probs, 1e-9) - 1.0, 2)
    model_edge         = np.round(win_probs - market_probs, 4)
    place_probs, show_probs = _place_show_probs(win_probs)

    bet_thr  = config["bet_edge_threshold"]
    ul_thr   = config["underlay_edge_threshold"]
    bet_tags = [_bet_tag(e, bet_thr, ul_thr) for e in model_edge]
    rank_arr = (
        pd.to_numeric(pd.Series(win_probs), errors="coerce")
        .fillna(0.0)
        .rank(ascending=False, method="first")
        .astype(int)
        .values
    )

    # ── Group scores for board columns ─────────────────────────────────────
    group_scores     = compute_group_scores(feat_df, config)
    form_arr         = group_scores.get("form_class",      np.zeros(len(feat_df)))
    surf_dist_arr    = group_scores.get("distance_surface", np.zeros(len(feat_df)))

    # ── Confidence scoring (4-component scored system) ────────────────────
    model_feats = [
        f for g in config["feature_groups"].values()
        for f in g["features"]
    ]
    conf_df = compute_horse_confidence(
        feat_df, entries_df, win_probs, market_probs,
        model_feats, derby_override=derby_active,
    )

    # ── Low-confidence BET guardrail ──────────────────────────────────────
    # LOW-bucket entries: edge may be artefact of odds-floor vs market gap,
    # not genuine model signal.  Force to neutral and record the block.
    _bucket_by_eid = {
        int(r["entry_id"]): r["confidence_bucket"]
        for _, r in conf_df.iterrows()
    }
    final_bet_tags     = []
    low_conf_bet_block = []
    for i, (_, erow) in enumerate(entries_df.iterrows()):
        raw_tag = bet_tags[i]
        bucket  = _bucket_by_eid.get(int(erow["entry_id"]), "LOW")
        if bucket == "LOW" and raw_tag == "bet":
            final_bet_tags.append("neutral")
            low_conf_bet_block.append(1)
        else:
            final_bet_tags.append(raw_tag)
            low_conf_bet_block.append(0)

    # ── Chaos pipeline ────────────────────────────────────────────────────
    (chaos_scores, chaos_boosts, chaos_tiers, chaos_eligs,
     chaos_applied, chaos_intensity) = _chaos_outputs_for_run(
        entries_df, feat_df, win_probs, form_arr, surf_dist_arr,
        derby_active=derby_active, chaos_index=_DERBY_DEFAULT_CHAOS_INDEX,
    )
    field_entropy = float(-np.sum(win_probs * np.log(np.maximum(win_probs, 1e-9))))
    if chaos_applied:
        print(f"  [scorer]   chaos applied  intensity={chaos_intensity:.3f}  "
              f"entropy={field_entropy:.3f}")

    # ── Metrics ───────────────────────────────────────────────────────────
    metrics = _compute_metrics(win_probs, market_probs, artifact)
    metrics["score_ts"]          = score_ts
    metrics["bet_count"]         = sum(1 for t in final_bet_tags if t == "bet")
    metrics["blocked_bet_count"] = sum(low_conf_bet_block)

    # ── Save artifact + register model ────────────────────────────────────
    artifact_path = save_artifact(artifact)
    model_id      = register_model(artifact, artifact_path, metrics, conn)
    print(f"  [scorer]   model_id={model_id}  artifact={artifact_path.name}")

    # ── DB writes ──────────────────────────────────────────────────────────
    _ensure_chaos_columns(conn)
    ensure_entry_scores_columns(conn)
    run_id = str(uuid.uuid4())[:8]

    quality_tier = "seed_only"
    try:
        n_pp = conn.execute(
            "SELECT COUNT(*) FROM firstbet_pp_starts WHERE card_id=?", (card_id,)
        ).fetchone()[0]
        if n_pp > 0:
            quality_tier = "enriched_proxy"
    except Exception:
        pass

    conn.execute(
        "INSERT INTO score_runs "
        "(run_id, card_id, model_id, model_type, derby_override_active, quality_tier, "
        " chaos_active, chaos_intensity, field_entropy_score) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, card_id, model_id, artifact.model_type, int(derby_active), quality_tier,
         int(chaos_applied), round(chaos_intensity, 4), round(field_entropy, 4)),
    )

    # Purge stale runs for this card (keep only latest)
    conn.execute(
        "DELETE FROM entry_scores WHERE run_id IN "
        "(SELECT run_id FROM score_runs WHERE card_id=? AND run_id != ?)",
        (card_id, run_id),
    )

    for i, (_, erow) in enumerate(entries_df.iterrows()):
        eid = int(erow["entry_id"])
        conf_row    = conf_df[conf_df["entry_id"] == eid]
        if not conf_row.empty:
            cr = conf_row.iloc[0]
            conf_flag   = int(cr["confidence_flag"])
            conf_score  = float(cr["confidence_score"])
            conf_bucket = str(cr["confidence_bucket"])
            conf_reasons= str(cr["confidence_reasons"])
        else:
            conf_flag, conf_score, conf_bucket, conf_reasons = 0, 0.25, "LOW", "no feature data"
        conn.execute(
            """
            INSERT INTO entry_scores (
                run_id, entry_id, horse_name, post_position,
                morning_line_odds, win_probability, place_probability, show_probability,
                pace_fit_score, form_score, surface_dist_fit, value_score,
                market_implied_prob, bet_tag,
                confidence_flag, missing_data_flag, low_conf_bet_block, rank,
                trainer_name, jockey_name,
                chaos_score, chaos_boost, chaos_tier, chaos_eligible,
                confidence_score, confidence_bucket, confidence_reasons
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, eid, erow["horse_name"], int(erow["post_position"]),
                float(erow["morning_line_odds"]),
                round(float(win_probs[i]),    6),
                round(float(place_probs[i]),  6),
                round(float(show_probs[i]),   6),
                round(float(feat_df.iloc[i]["pace_fit_score"]) if feat_df.iloc[i]["pace_fit_score"] is not None else 0.0, 4),
                round(float(form_arr[i]),     4),
                round(float(surf_dist_arr[i]), 4),
                round(float(model_edge[i]),   4),
                round(float(market_probs[i]), 6),
                final_bet_tags[i],
                conf_flag,
                1,   # missing_data_flag=1 for all entries (PLACEHOLDERs are absent)
                int(low_conf_bet_block[i]),
                int(rank_arr[i]),
                erow.get("trainer", ""),
                erow.get("jockey", ""),
                round(float(chaos_scores[i]), 6) if chaos_applied else None,
                round(float(chaos_boosts[i]), 6) if chaos_applied else None,
                chaos_tiers[i]                   if chaos_applied else None,
                int(chaos_eligs[i]),
                conf_score,
                conf_bucket,
                conf_reasons,
            ),
        )

    conn.commit()
    conn.close()

    # ── Build board DataFrame ──────────────────────────────────────────────
    board = entries_df[[
        "entry_id", "horse_name", "post_position",
        "trainer", "jockey", "morning_line_odds",
        "dist_starts",
    ]].copy().reset_index(drop=True)

    board["model_win_prob"]     = win_probs
    board["model_win_prob_pct"] = np.round(win_probs * 100, 2)
    board["fair_odds"]          = fair_odds
    board["market_prob"]        = market_probs
    board["value_score"]        = model_edge
    board["bet_tag"]            = final_bet_tags
    board["low_conf_bet_block"] = low_conf_bet_block
    board["form_score"]         = np.round(form_arr,      4)
    board["surface_dist_fit"]   = np.round(surf_dist_arr, 4)
    board["pace_fit_score"]     = feat_df["pace_fit_score"].values
    if chaos_applied:
        board["chaos_score"]    = np.round(chaos_scores, 6)
        board["chaos_boost"]    = np.round(chaos_boosts, 6)
        board["chaos_tier"]     = chaos_tiers
        board["chaos_eligible"] = chaos_eligs
    board["rank"]               = rank_arr

    # Merge confidence columns
    conf_merge = conf_df[[
        "entry_id", "model_confidence", "missing_data_flags",
        "confidence_score", "confidence_bucket", "confidence_reasons",
    ]]
    board = board.merge(conf_merge, on="entry_id", how="left")
    board["dist_starts_raw"] = board["dist_starts"]  # for low-conf table

    board = board.sort_values("rank").reset_index(drop=True)

    # ── Write outputs ──────────────────────────────────────────────────────
    _write_board(board, run_id, model_id, artifact, metrics, score_ts)
    _write_eval_report(metrics, artifact, board, model_id)

    low_conf_n = int((board["model_confidence"] == "low").sum())
    blocked_n = metrics.get("blocked_bet_count", 0)
    print(f"  [scorer]   run_id={run_id}  sum_win_prob={metrics['sum_win_prob']:.6f}  "
          f"bets={metrics['bet_count']}  blocked={blocked_n}  "
          f"underlays={metrics['underlay_count']}  low_conf={low_conf_n}")

    return board


def score_derby() -> pd.DataFrame:
    """Alias for backwards compatibility with scripts/score.py."""
    return score_race(card_id=None)
