"""
DerbyEdge V1  —  Scorer
src/models/scorer.py

Bet-tag thresholds:
  bet     : model_edge >= +0.025
  underlay: model_edge <  -0.015
  neutral : -0.015 <= model_edge < +0.025

Confidence tiers (seed-only install):
  medium : dist_starts >= 2 AND all model features non-null
  low    : dist_starts <= 1 (distance_fit based on stamina_index only)
  high   : not possible until horse_starts table is populated

Missing-data flags are per-horse text labels combining:
  - Global (every horse in seed-only install): the 5 most impactful PLACEHOLDERs
  - Per-horse: dist_fit_single_start when dist_starts <= 1
"""

import datetime
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

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
from src.utils.db import get_connection, get_derby_card_id

ROOT       = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "output"

# Critical PLACEHOLDER features (most impactful if they were available)
CRITICAL_MISSING = [
    "no_race_splits",       # pace_early_mean_3 / pace_mid_mean_3
    "no_workout_detail",    # bullet_30d / days_since_last_work
    "no_connections_stats", # trainer_jockey_itm_cond / jockey_route_cond
    "no_track_form",        # churchill_readiness
    "no_post_bias",         # post_win_bias
]

# Derby-specific PLACEHOLDER flags (added on top of CRITICAL_MISSING)
DERBY_EXTRA_MISSING = [
    "no_jan_apr_curve",       # jan_apr_improvement_curve: sequential speed progression
    "no_churchill_readiness", # churchill_readiness: Churchill Downs specific form
]

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


def _model_confidence(
    dist_starts: int,
    career_starts: int,
    has_null_model_feat: bool,
    derby_override: bool = False,
    pedigree_route_proxy: Optional[float] = None,
) -> str:
    """
    Confidence tiers for seed-only mode.
    'high' is not possible until horse_starts is populated.

    Derby tightening (derby_override=True):
      - dist_starts <= 1: always low
      - dist_starts == 2: low unless pedigree_route_proxy >= 0.75
      - dist_starts >= 3: medium (if model features present)
    """
    if has_null_model_feat:
        return "low"
    if dist_starts <= 1:
        return "low"
    if derby_override and dist_starts == 2:
        if pedigree_route_proxy is None or pedigree_route_proxy < 0.75:
            return "low"   # limited route experience, weak pedigree
    return "medium"


def _missing_flags(dist_starts: int, derby_override: bool = False) -> str:
    flags = list(CRITICAL_MISSING)
    if derby_override:
        flags.extend(DERBY_EXTRA_MISSING)
    if dist_starts <= 1:
        flags.append("dist_fit_single_start")
    return ",".join(flags)


def _compute_confidence_and_flags(
    feat_df:        pd.DataFrame,
    entries_df:     pd.DataFrame,
    model_features: list[str],
    derby_override: bool = False,
) -> pd.DataFrame:
    """
    Return DataFrame with entry_id, model_confidence, missing_data_flags,
    confidence_flag (0/1 for DB).
    """
    check_cols = [c for c in model_features if c in feat_df.columns]

    rows = []
    for _, erow in entries_df.iterrows():
        eid  = int(erow["entry_id"])
        frow = feat_df[feat_df["entry_id"] == eid]
        if frow.empty:
            base_flags = CRITICAL_MISSING + (DERBY_EXTRA_MISSING if derby_override else [])
            rows.append({"entry_id": eid, "model_confidence": "low",
                         "missing_data_flags": ",".join(base_flags),
                         "confidence_flag": 0})
            continue

        fr = frow.iloc[0]
        has_null = any(
            fr.get(c) is None or (isinstance(fr.get(c), float) and np.isnan(fr.get(c)))
            for c in check_cols
        )
        def _int_or_zero(v):
            return 0 if (v is None or (isinstance(v, float) and np.isnan(v))) else int(v)
        dist_starts   = _int_or_zero(erow.get("dist_starts"))
        career_starts = _int_or_zero(erow.get("career_starts"))
        ped_proxy     = fr.get("pedigree_route_proxy")
        if ped_proxy is not None:
            try:
                ped_proxy = float(ped_proxy)
            except (TypeError, ValueError):
                ped_proxy = None

        confidence = _model_confidence(
            dist_starts, career_starts, has_null,
            derby_override=derby_override,
            pedigree_route_proxy=ped_proxy,
        )
        flags     = _missing_flags(dist_starts, derby_override=derby_override)
        conf_flag = 1 if confidence == "medium" else 0

        rows.append({
            "entry_id":           eid,
            "model_confidence":   confidence,
            "missing_data_flags": flags,
            "confidence_flag":    conf_flag,
        })

    return pd.DataFrame(rows)


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
        "value_score", "bet_tag",
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

    tag_icons = {"bet": "**BET**", "underlay": "~~UL~~", "neutral": "--"}
    conf_icons = {"high": "HIGH", "medium": "MED", "low": "LOW!"}

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
        f"| Top win probability | {top_row['horse_name']} {top_row['model_win_prob_pct']:.1f}% "
        f"(fair {top_row['fair_odds']:.1f}-1) |",
        f"| Top value score | {top_value['horse_name']} "
        f"{'+' if top_value['value_score'] > 0 else ''}{top_value['value_score']:.3f} "
        f"({top_value['bet_tag']}) |",
        f"| Kendall tau vs market | {metrics['kendall_tau_vs_ml']:.4f} |",
        f"| Mean abs edge | {metrics['mean_edge_abs']:.4f} |",
        f"| Low-confidence entries | {low_conf} of {len(board)} "
        f"(dist_starts <= 1; distance_fit unreliable) |",
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
        tag_str  = tag_icons.get(r['bet_tag'], r['bet_tag'])
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

    # ── Missing-data detail ────────────────────────────────────────────────
    low_conf_horses = board[board["model_confidence"] == "low"]
    if not low_conf_horses.empty:
        lines += [
            "",
            "### Low-Confidence Entries",
            "",
            "These horses have `dist_starts <= 1`; their distance_fit score is based on "
            "`stamina_index` alone (no race history at 1.25 miles).",
            "",
            "| Horse | Post | Dist Starts | Additional Missing Flags |",
            "|-------|------|-------------|--------------------------|",
        ]
        for _, r in low_conf_horses.iterrows():
            flags = r['missing_data_flags'].replace(
                ",".join(CRITICAL_MISSING) + ",", ""
            ).replace(",".join(CRITICAL_MISSING), "")
            lines.append(
                f"| {r['horse_name']} | {int(r['post_position'])} "
                f"| {int(r.get('dist_starts_raw') or 0) if not (isinstance(r.get('dist_starts_raw'), float) and np.isnan(r.get('dist_starts_raw') or 0)) else 0} "
                f"| dist_fit_single_start |"
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
    feat_df = pd.read_sql(
        "SELECT * FROM feature_store WHERE card_id=? ORDER BY post_position",
        conn, params=(card_id,),
    )
    if feat_df.empty:
        conn.close()
        raise RuntimeError(f"No features for card_id={card_id} — run build_features first.")

    entries_df = pd.read_sql(
        "SELECT * FROM v_entries_live WHERE card_id=? ORDER BY post_position",
        conn, params=(card_id,),
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

    # ── Market probs (overround-adjusted) ─────────────────────────────────
    ml_implied   = feat_df["market_implied_prob"].astype(float).values
    market_probs = ml_implied / ml_implied.sum()

    # ── Derived scoring ────────────────────────────────────────────────────
    fair_odds          = np.round(1.0 / np.maximum(win_probs, 1e-9) - 1.0, 2)
    model_edge         = np.round(win_probs - market_probs, 4)
    place_probs, show_probs = _place_show_probs(win_probs)

    bet_thr  = config["bet_edge_threshold"]
    ul_thr   = config["underlay_edge_threshold"]
    bet_tags = [_bet_tag(e, bet_thr, ul_thr) for e in model_edge]
    rank_arr = pd.Series(win_probs).rank(ascending=False, method="first").astype(int).values

    # ── Group scores for board columns ─────────────────────────────────────
    group_scores     = compute_group_scores(feat_df, config)
    form_arr         = group_scores.get("form_class",      np.zeros(len(feat_df)))
    surf_dist_arr    = group_scores.get("distance_surface", np.zeros(len(feat_df)))

    # ── Confidence + missing flags ─────────────────────────────────────────
    model_feats = [
        f for g in config["feature_groups"].values()
        for f in g["features"]
    ]
    conf_df = _compute_confidence_and_flags(feat_df, entries_df, model_feats,
                                             derby_override=derby_active)

    # ── Metrics ───────────────────────────────────────────────────────────
    metrics = _compute_metrics(win_probs, market_probs, artifact)
    metrics["score_ts"] = score_ts

    # ── Save artifact + register model ────────────────────────────────────
    artifact_path = save_artifact(artifact)
    model_id      = register_model(artifact, artifact_path, metrics, conn)
    print(f"  [scorer]   model_id={model_id}  artifact={artifact_path.name}")

    # ── DB writes ──────────────────────────────────────────────────────────
    run_id = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT INTO score_runs (run_id, card_id, model_id, model_type, derby_override_active) "
        "VALUES (?,?,?,?,?)",
        (run_id, card_id, model_id, artifact.model_type, int(derby_active)),
    )

    # Purge stale runs for this card (keep only latest)
    conn.execute(
        "DELETE FROM entry_scores WHERE run_id IN "
        "(SELECT run_id FROM score_runs WHERE card_id=? AND run_id != ?)",
        (card_id, run_id),
    )

    for i, (_, erow) in enumerate(entries_df.iterrows()):
        eid = int(erow["entry_id"])
        conf_row  = conf_df[conf_df["entry_id"] == eid]
        conf_flag = int(conf_row["confidence_flag"].iloc[0]) if not conf_row.empty else 0
        conn.execute(
            """
            INSERT INTO entry_scores (
                run_id, entry_id, horse_name, post_position,
                morning_line_odds, win_probability, place_probability, show_probability,
                pace_fit_score, form_score, surface_dist_fit, value_score,
                market_implied_prob, bet_tag,
                confidence_flag, missing_data_flag, rank,
                trainer_name, jockey_name
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                bet_tags[i],
                conf_flag,
                1,   # missing_data_flag=1 for all entries (PLACEHOLDERs are absent)
                int(rank_arr[i]),
                erow.get("trainer", ""),
                erow.get("jockey", ""),
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
    board["bet_tag"]            = bet_tags
    board["form_score"]         = np.round(form_arr,      4)
    board["surface_dist_fit"]   = np.round(surf_dist_arr, 4)
    board["pace_fit_score"]     = feat_df["pace_fit_score"].values
    board["rank"]               = rank_arr

    # Merge confidence columns
    conf_merge = conf_df[["entry_id", "model_confidence", "missing_data_flags"]]
    board = board.merge(conf_merge, on="entry_id", how="left")
    board["dist_starts_raw"] = board["dist_starts"]  # for low-conf table

    board = board.sort_values("rank").reset_index(drop=True)

    # ── Write outputs ──────────────────────────────────────────────────────
    _write_board(board, run_id, model_id, artifact, metrics, score_ts)
    _write_eval_report(metrics, artifact, board, model_id)

    low_conf_n = int((board["model_confidence"] == "low").sum())
    print(f"  [scorer]   run_id={run_id}  sum_win_prob={metrics['sum_win_prob']:.6f}  "
          f"bets={metrics['bet_count']}  underlays={metrics['underlay_count']}  "
          f"low_conf={low_conf_n}")

    return board


def score_derby() -> pd.DataFrame:
    """Alias for backwards compatibility with scripts/score.py."""
    return score_race(card_id=None)
