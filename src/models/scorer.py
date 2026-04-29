"""
DerbyEdge V1  —  Scorer
src/models/scorer.py

Loads (or builds) a model artifact for a race card, produces calibrated
win probabilities, fair odds, model edge, bet tags, and writes outputs to:
  - DB: score_runs + entry_scores
  - output/derby_2026_board.csv
  - output/derby_2026_board.md
  - output/model_evaluation_{race_type}.md
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
    compute_feature_importances,
    register_model,
    save_artifact,
    train_or_build,
)
from src.utils.db import get_connection, get_derby_card_id

ROOT       = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "output"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _place_show_probs(win_probs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n     = len(win_probs)
    place = 0.45 * win_probs + 0.55 * (1.0 / n)
    show  = 0.35 * win_probs + 0.65 * (1.0 / n)
    return place / place.sum(), show / show.sum()


def _compute_metrics(
    win_probs:    np.ndarray,
    market_probs: np.ndarray,
    artifact:     ModelArtifact,
) -> dict:
    """
    Pre-race diagnostics only — no actual race outcomes available yet.
    Outcome-based metrics (log_loss, brier, top1) are explicitly N/A.
    """
    from scipy.stats import kendalltau

    tau, _   = kendalltau(win_probs, market_probs)
    edges    = win_probs - market_probs
    bet_mask = edges >= artifact.config["bet_edge_threshold"]
    ul_mask  = edges <= artifact.config["underlay_edge_threshold"]

    # KL divergence: KL(model || market)
    kl = float(np.sum(win_probs * np.log(np.maximum(win_probs / np.maximum(market_probs, 1e-9), 1e-9))))

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
        "bet_count":         int(bet_mask.sum()),
        "underlay_count":    int(ul_mask.sum()),
        # Outcome-based: not available until race is run
        "log_loss":          None,
        "brier_score":       None,
        "calibration_error": None,
        "top1_hit_rate":     None,
        "edge_roi":          None,
    }


def _bet_tag(
    edge: float,
    bet_threshold: float,
    underlay_threshold: float,
) -> str:
    if edge >= bet_threshold:
        return "bet"
    if edge <= underlay_threshold:
        return "underlay"
    return "neutral"


# ---------------------------------------------------------------------------
# Board and evaluation report writers
# ---------------------------------------------------------------------------
def _write_board(board: pd.DataFrame, run_id: str, model_type: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    csv_cols = [
        "rank", "horse_name", "post_position",
        "trainer", "jockey",
        "morning_line_odds",
        "model_win_prob_pct", "fair_odds",
        "pace_fit_score", "form_score", "surface_dist_fit",
        "value_score", "bet_tag",
    ]
    board[csv_cols].to_csv(OUTPUT_DIR / "derby_2026_board.csv", index=False)

    tag_icons = {"bet": "**BET**", "underlay": "~~UL~~", "neutral": "—"}
    lines = [
        "# DerbyEdge Engine — 2026 Kentucky Derby Board",
        "",
        f"*Model: `{model_type}` | Run ID: `{run_id}` | Race: 2026-05-02 Churchill Downs*",
        "",
        "| Rank | Horse | Post | Trainer | Jockey | ML | Win% | Fair Odds | Pace Fit | Form | SuDist | Edge | Tag |",
        "|------|-------|------|---------|--------|-----|------|-----------|----------|------|--------|------|-----|",
    ]
    for _, r in board.iterrows():
        edge_str = f"+{r['value_score']:.3f}" if r['value_score'] > 0 else f"{r['value_score']:.3f}"
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
            f"| {tag_icons.get(r['bet_tag'], r['bet_tag'])} |"
        )

    (OUTPUT_DIR / "derby_2026_board.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  [board]    board written -> {OUTPUT_DIR / 'derby_2026_board.md'}")


def _write_eval_report(
    metrics:      dict,
    artifact:     ModelArtifact,
    board:        pd.DataFrame,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    race_type = metrics["race_type_key"]
    path      = OUTPUT_DIR / f"model_evaluation_{race_type}.md"

    # Determine model quality label
    if metrics["training_rows"] >= 50:
        quality = "TRAINED — XGBoost on historical races with rolling CV"
    else:
        quality = ("SEED-ONLY BASELINE — principled weighted composite from 46-feature "
                   "catalog; no historical training data; probabilities are model-informed "
                   "estimates, not calibrated predictions")

    top5 = board.nsmallest(5, "rank")[
        ["rank", "horse_name", "model_win_prob_pct", "fair_odds", "value_score", "bet_tag"]
    ]
    top3_value = board.nlargest(3, "value_score")[
        ["horse_name", "morning_line_odds", "model_win_prob_pct", "value_score", "bet_tag"]
    ]

    # Feature importances sorted
    fi = sorted(
        artifact.feature_importances.items(), key=lambda x: -x[1]
    )[:15]

    lines = [
        f"# DerbyEdge Model Evaluation — {race_type}",
        "",
        f"**Generated** : {datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}  ",
        f"**Model name** : `{artifact.model_name}`  ",
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
        f"| Calibration method | temperature-scaled softmax (T={metrics['temperature']}) |",
        f"| Calibration target | overround-adjusted morning line |",
        f"| Outcome validation | NOT POSSIBLE — race not yet run (2026-05-02) |",
        "",
        "## Pre-Race Diagnostics",
        "",
        "Outcome-based metrics (log_loss, Brier, top-1) require actual race results.",
        "The metrics below are computable before the race.",
        "",
        "| Metric | Value | Interpretation |",
        "|--------|-------|----------------|",
        f"| `sum_win_prob` | {metrics['sum_win_prob']:.6f} | Should be 1.000000 |",
        f"| `kendall_tau_vs_ml` | {metrics['kendall_tau_vs_ml']:.4f} | Rank correlation with market; 1=identical, 0=no overlap |",
        f"| `kl_div_vs_ml` | {metrics['kl_div_vs_ml']:.4f} | KL(model \\|\\| market); 0=identical, higher=more divergent |",
        f"| `mean_edge_abs` | {metrics['mean_edge_abs']:.4f} | Mean absolute model-market divergence per horse |",
        f"| `max_positive_edge` | {metrics['max_positive_edge']:.4f} | Best value play |",
        f"| `max_negative_edge` | {metrics['max_negative_edge']:.4f} | Worst underlay |",
        f"| `bet_count` | {metrics['bet_count']} | Horses with edge >= +{artifact.config['bet_edge_threshold']:.3f} |",
        f"| `underlay_count` | {metrics['underlay_count']} | Horses with edge <= {artifact.config['underlay_edge_threshold']:.3f} |",
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
        "Effective weight = within-group weight x group weight, normalized to sum 1.0.",
        "",
        "| Rank | Feature | Effective Weight |",
        "|------|---------|-----------------|",
    ]
    for i, (fname, fw) in enumerate(fi, 1):
        lines.append(f"| {i} | `{fname}` | {fw:.4f} |")

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
            f"| {int(r['rank'])} | {r['horse_name']} | {r['model_win_prob_pct']:.1f}% "
            f"| {r['fair_odds']:.1f}-1 | {edge_str} | {r['bet_tag']} |"
        )

    lines += [
        "",
        "## Top 3 by Value Score (Model Edge)",
        "",
        "| Horse | ML Odds | Win% | Edge | Tag |",
        "|-------|---------|------|------|-----|",
    ]
    for _, r in top3_value.iterrows():
        edge_str = f"+{r['value_score']:.3f}" if r['value_score'] > 0 else f"{r['value_score']:.3f}"
        lines.append(
            f"| {r['horse_name']} | {r['morning_line_odds']:.0f}-1 "
            f"| {r['model_win_prob_pct']:.1f}% | {edge_str} | {r['bet_tag']} |"
        )

    lines += [
        "",
        "## Limitations",
        "",
        "- This model is a **seed-only baseline**. It has no access to:",
        "  - Race-by-race speed figures (horse_starts empty)",
        "  - Real workout records (workouts empty)",
        "  - Conditioned trainer/jockey stats (v_connections_180 empty)",
        "  - Track bias (track_bias empty)",
        "  - Trip flags (trip_flags empty)",
        "- 12 of 46 features are PLACEHOLDER (null for all entries).",
        "- 12 features are DEGRADED (proxy formulas from aggregate seed data).",
        "- Calibration is a temperature-scaled softmax tuned to morning line spread,",
        "  NOT isotonic regression calibrated against actual race outcomes.",
        "- **Do not use these probabilities for real-money wagering without historical validation.**",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [eval]     evaluation report -> {path}")


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------
def score_race(card_id: Optional[int] = None) -> pd.DataFrame:
    """
    Score a race card end-to-end.

    1. Load feature store and v_entries_live
    2. Build/load model artifact (seed_only_baseline if no history)
    3. Calibrate win probabilities
    4. Compute fair_odds, model_edge, bet_tags
    5. Write to DB: score_runs + entry_scores
    6. Write output files
    Returns the sorted board DataFrame.
    """
    conn = get_connection()

    if card_id is None:
        card_id = get_derby_card_id()
    if card_id is None:
        conn.close()
        raise RuntimeError("No Kentucky Derby card found — run ingest first.")

    # ── Load feature store ─────────────────────────────────────────────────
    feat_df = pd.read_sql(
        "SELECT * FROM feature_store WHERE card_id=? ORDER BY post_position",
        conn, params=(card_id,),
    )
    if feat_df.empty:
        conn.close()
        raise RuntimeError(f"No features for card_id={card_id} — run build_features first.")

    # ── Load live entries (for trainer/jockey names + morning line) ────────
    entries_df = pd.read_sql(
        "SELECT * FROM v_entries_live WHERE card_id=? ORDER BY post_position",
        conn, params=(card_id,),
    )

    # Determine race type from race_cards
    rc = conn.execute(
        "SELECT surface, distance_furlongs FROM race_cards WHERE card_id=?",
        (card_id,),
    ).fetchone()
    surface        = rc["surface"] if rc else "dirt"
    dist_furlongs  = float(rc["distance_furlongs"]) if rc else 10.0
    dist_cat       = "sprint" if dist_furlongs < 8.5 else "route"
    race_type_key  = f"{surface}_{dist_cat}"

    print(f"  [scorer]   card_id={card_id}  race_type={race_type_key}  "
          f"entries={len(feat_df)}")

    # ── Build model ────────────────────────────────────────────────────────
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

    # ── Derived scoring columns ────────────────────────────────────────────
    fair_odds    = np.round(1.0 / np.maximum(win_probs, 1e-9) - 1.0, 2)
    model_edge   = np.round(win_probs - market_probs, 4)
    place_probs, show_probs = _place_show_probs(win_probs)

    bet_threshold      = config["bet_edge_threshold"]
    underlay_threshold = config["underlay_edge_threshold"]
    bet_tags = [_bet_tag(e, bet_threshold, underlay_threshold) for e in model_edge]

    # ── Group scores for board columns ─────────────────────────────────────
    from src.models.trainer import compute_group_scores
    group_scores = compute_group_scores(feat_df, config)
    form_score_arr     = group_scores.get("form_class",     np.zeros(len(feat_df)))
    surf_dist_score_arr = group_scores.get("distance_surface", np.zeros(len(feat_df)))

    # ── Compute metrics ────────────────────────────────────────────────────
    metrics = _compute_metrics(win_probs, market_probs, artifact)

    # ── Save artifact + register model ────────────────────────────────────
    artifact_path = save_artifact(artifact)
    model_id      = register_model(artifact, artifact_path, metrics, conn)
    print(f"  [scorer]   model_id={model_id}  artifact={artifact_path.name}")

    # ── Score run record ───────────────────────────────────────────────────
    run_id = str(uuid.uuid4())[:8]
    conn.execute(
        """
        INSERT INTO score_runs (run_id, card_id, model_id, model_type)
        VALUES (?,?,?,?)
        """,
        (run_id, card_id, model_id, artifact.model_type),
    )

    # ── Entry scores record ────────────────────────────────────────────────
    conn.execute("DELETE FROM entry_scores WHERE run_id IN "
                 "(SELECT run_id FROM score_runs WHERE card_id=? AND run_id != ?)",
                 (card_id, run_id))

    rank_arr = pd.Series(win_probs).rank(ascending=False, method="min").astype(int).values

    for i, row in entries_df.iterrows():
        idx = int(feat_df[feat_df["entry_id"] == row["entry_id"]].index[0]
                  if not feat_df[feat_df["entry_id"] == row["entry_id"]].empty
                  else i)
        missing_flag = int(any(
            feat_df.iloc[idx][f] is None or
            (isinstance(feat_df.iloc[idx][f], float) and np.isnan(feat_df.iloc[idx][f]))
            for f in config["feature_groups"]["speed_quality"]["features"]
        ))
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
                run_id, int(row["entry_id"]), row["horse_name"], int(row["post_position"]),
                float(row["morning_line_odds"]),
                round(float(win_probs[idx]),   6),
                round(float(place_probs[idx]), 6),
                round(float(show_probs[idx]),  6),
                round(float(feat_df.iloc[idx]["pace_fit_score"]) if feat_df.iloc[idx]["pace_fit_score"] is not None else 0.0, 4),
                round(float(form_score_arr[idx]),      4),
                round(float(surf_dist_score_arr[idx]), 4),
                round(float(model_edge[idx]),  4),
                round(float(market_probs[idx]), 6),
                bet_tags[idx],
                0,   # confidence_flag: 0 for seed-only baseline
                missing_flag,
                int(rank_arr[idx]),
                row.get("trainer", ""),
                row.get("jockey", ""),
            ),
        )

    conn.commit()
    conn.close()

    # ── Build board DataFrame ──────────────────────────────────────────────
    board = entries_df[[
        "entry_id", "horse_name", "post_position",
        "trainer", "jockey", "morning_line_odds",
    ]].copy().reset_index(drop=True)

    board["model_win_prob"]     = win_probs
    board["model_win_prob_pct"] = np.round(win_probs * 100, 2)
    board["fair_odds"]          = fair_odds
    board["market_prob"]        = market_probs
    board["value_score"]        = model_edge
    board["bet_tag"]            = bet_tags
    board["place_prob"]         = place_probs
    board["show_prob"]          = show_probs
    board["form_score"]         = np.round(form_score_arr, 4)
    board["surface_dist_fit"]   = np.round(surf_dist_score_arr, 4)
    board["pace_fit_score"]     = feat_df["pace_fit_score"].values
    board["rank"]               = rank_arr

    board = board.sort_values("rank").reset_index(drop=True)

    # ── Write outputs ──────────────────────────────────────────────────────
    _write_board(board, run_id, artifact.model_type)
    _write_eval_report(metrics, artifact, board)

    print(f"  [scorer]   run_id={run_id}  sum_win_prob={metrics['sum_win_prob']:.6f}  "
          f"bets={metrics['bet_count']}  underlays={metrics['underlay_count']}")

    return board


def score_derby() -> pd.DataFrame:
    """Alias for backwards compatibility with scripts/score.py."""
    return score_race(card_id=None)
