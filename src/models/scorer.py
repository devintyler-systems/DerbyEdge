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
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from src.utils.horse_norm import normalize_horse_name as _norm_horse
from src.utils.run_assets import card_run_key, run_dir_for_card

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
    ensure_score_runs_columns,
)
from src.models.policy import (
    bucket_field_size,
    choose_tier,
    default_chaos as policy_default_chaos,
    normalize_surface as _policy_norm_surface,
    normalize_dist_category as _policy_norm_dist,
)
from src.ingest.run_state import (
    RunMode,
    resolve_mode_with_feature_checks,
)
from src.services.feature_state import verify_feature_frame
from src.services.run_mode import (
    ScoringBlockedError,
    get_card_run_state,
    quality_with_verified_features,
)
from src.services.odds_intake import load_live_odds_by_pp
from src.services.model_independence import (
    detect_market_prior_collapse,
    pre_market_signal_probabilities,
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

try:
    from training.win_model_loader import (
        load_best_model as _load_best_model,
        score_dataframe as _ml_score_dataframe,
    )
    _ML_LOADER_AVAILABLE = True
except ImportError:
    _ML_LOADER_AVAILABLE = False


def _resolve_serving_mode() -> str:
    """Return canonical serving mode from DERBYEDGE_ML_MODE env var.

    Values: 'off' (default) | 'shadow' | 'live'
    Any unrecognized value defaults to 'off'.
    """
    mode = os.getenv("DERBYEDGE_ML_MODE", "off").lower().strip()
    return mode if mode in ("off", "shadow", "live") else "off"


def _load_ml_win_probs(
    feat_df: pd.DataFrame,
    entries_df: pd.DataFrame,
    heuristic_probs: np.ndarray,
    market_probs: np.ndarray,
    race_type_key: str,
    dist_furlongs: float,
    surface: str,
) -> Optional[np.ndarray]:
    """Return ML-model win probabilities (normalized, same shape as heuristic_probs).

    Builds an inference DataFrame from scorer's live feat_df/entries_df and the
    already-computed heuristic probabilities (used as pred_win_prob feature).
    Returns None when no artifact is available or scoring fails — the caller
    falls back to the heuristic silently.
    """
    if not _ML_LOADER_AVAILABLE:
        return None

    import logging
    _mlog = logging.getLogger(__name__)

    try:
        model, cal, feat_cols = _load_best_model(race_type_key)
        if model is None:
            _mlog.debug("_load_ml_win_probs: no artifact for segment %s", race_type_key)
            return None

        # Temporary rank from heuristic — used as pred_rank feature.
        temp_rank = (
            pd.Series(heuristic_probs)
            .rank(ascending=False, method="first")
            .astype(int)
            .values
        )

        # Merge morning_line_odds from entries onto feat_df by entry_id.
        ent_sub = (
            entries_df[["entry_id", "morning_line_odds"]]
            .copy()
            .assign(entry_id=entries_df["entry_id"].astype(int))
        )
        merged = (
            feat_df.copy()
            .assign(entry_id=feat_df["entry_id"].astype(int))
            .merge(ent_sub, on="entry_id", how="left")
            .reset_index(drop=True)
        )

        n = len(merged)
        inf_df = pd.DataFrame({
            "post":              pd.to_numeric(merged["post_position"], errors="coerce"),
            "ml_odds":           pd.to_numeric(merged["morning_line_odds"], errors="coerce"),
            "pred_win_prob":     heuristic_probs,
            "pred_rank":         temp_rank,
            "edge":              heuristic_probs - market_probs,
            "pace_fit":          pd.to_numeric(merged.get("pace_fit_score"),  errors="coerce"),
            "form_score":        pd.to_numeric(merged.get("form_cycle_idx"),  errors="coerce"),
            "sudist_fit":        pd.to_numeric(merged.get("distance_fit"),    errors="coerce"),
            "chaos_pct":         np.full(n, np.nan),
            "field_size":        float(n),
            "distance_furlongs": dist_furlongs,
            "distance_bucket":   "sprint" if dist_furlongs < 8.5 else "route",
            "surface":           surface,
        })

        scored   = _ml_score_dataframe(inf_df, model, cal, feat_cols)
        ml_probs = scored["model_win_prob"].to_numpy(dtype=float)

        if not np.isfinite(ml_probs).all() or ml_probs.sum() <= 0:
            _mlog.warning("_load_ml_win_probs: non-finite output — falling back to heuristic")
            return None

        return ml_probs / ml_probs.sum()

    except Exception as exc:
        _mlog.warning("_load_ml_win_probs: scoring failed — %s", exc)
        return None


def _decide_served_probs(
    mode: str,
    derby_override: bool,
    heuristic_probs: np.ndarray,
    ml_probs,
) -> tuple[np.ndarray, bool]:
    """Return (served_probs, ml_loaded_flag).

    Derby override always returns heuristic regardless of mode.
    off    → heuristic served; ML never run.
    shadow → ML scored and logged, but heuristic is served.
    live   → ML served if loaded; heuristic fallback on ML failure.
    """
    ml_loaded = ml_probs is not None
    if derby_override or mode == "off":
        return heuristic_probs.copy(), ml_loaded
    if mode == "shadow":
        return heuristic_probs.copy(), ml_loaded
    return (ml_probs.copy() if ml_loaded else heuristic_probs.copy()), ml_loaded


def _emit_shadow_log(
    card_id: int,
    race_meta: dict,
    entries_df: pd.DataFrame,
    heuristic_probs: np.ndarray,
    ml_probs,
    served_probs: np.ndarray,
    ml_loaded_flag: bool,
    mode: str,
    model_version: str,
    dist_furlongs: float,
    surface: str,
    derby_override: bool,
    segment: str,
    scored_at: str,
    policy_surface: str = "",
    policy_dist_category: str = "",
    policy_field_size_bucket: str = "",
    policy_tier_selected: str = "",
    policy_tier_reason: str = "",
    policy_chaos_selected: int = 0,
    policy_chaos_reason: str = "",
) -> None:
    """Append one row per starter to output/shadow_log.csv."""
    import logging as _logging
    _slog = _logging.getLogger(__name__)
    n = len(entries_df)
    if n == 0:
        return

    def _rank_arr(probs):
        if probs is None:
            return [None] * n
        return (
            pd.Series(probs)
            .rank(ascending=False, method="first")
            .astype(int)
            .tolist()
        )

    h_ranks = _rank_arr(heuristic_probs)
    m_ranks = _rank_arr(ml_probs)
    s_ranks = _rank_arr(served_probs)

    rows = []
    for i, (_, erow) in enumerate(entries_df.iterrows()):
        rows.append({
            "race_id":             card_id,
            "race_date":           race_meta.get("race_date", ""),
            "track":               race_meta.get("track", ""),
            "race_no":             race_meta.get("race_no", ""),
            "horse":               erow["horse_name"],
            "horse_norm":          _norm_horse(erow["horse_name"]),
            "post":                int(erow["post_position"]),
            "field_size":          n,
            "segment":             segment,
            "heuristic_win_prob":  round(float(heuristic_probs[i]), 6),
            "ml_win_prob":         round(float(ml_probs[i]), 6) if ml_probs is not None else None,
            "served_win_prob":     round(float(served_probs[i]), 6),
            "heuristic_rank":      h_ranks[i],
            "ml_rank":             m_ranks[i],
            "served_rank":         s_ranks[i],
            "ml_loaded_flag":      int(ml_loaded_flag),
            "serving_mode":        mode,
            "model_version":       model_version,
            "distance_furlongs":   dist_furlongs,
            "surface":             surface,
            "derby_override_flag":        int(derby_override),
            "scored_at":                  scored_at,
            "policy_surface":             policy_surface,
            "policy_dist_category":       policy_dist_category,
            "policy_field_size_bucket":   policy_field_size_bucket,
            "policy_tier_selected":       policy_tier_selected,
            "policy_tier_reason":         policy_tier_reason,
            "policy_chaos_selected":      policy_chaos_selected,
            "policy_chaos_reason":        policy_chaos_reason,
        })

    log_path = OUTPUT_DIR / "shadow_log.csv"
    df_new = pd.DataFrame(rows)
    if log_path.exists():
        df_new.to_csv(log_path, mode="a", header=False, index=False)
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        df_new.to_csv(log_path, mode="w", header=True, index=False)

    _slog.info("_emit_shadow_log: %d rows appended  mode=%s  card_id=%s", n, mode, card_id)


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

def _get_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names present in *table* via PRAGMA table_info."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _fetch_race_meta(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    fallback_surface: str = "",
    fallback_dist: "float | None" = None,
) -> dict:
    """Fetch race-card metadata for shadow-log emission.

    Builds the SELECT list from whichever race_cards columns actually exist so
    the query never references an absent column.  Missing fields are filled from
    already-computed fallback values or empty-string defaults.

    Column name mapping (priority order):
        race_date         → rc2.race_date   else rc2.card_date   else ""
        race_no           → rc2.race_no     else rc2.race_number  else ""
        distance_furlongs → rc2.distance_furlongs (generated col) else fallback_dist
        surface           → rc2.surface     else fallback_surface
    """
    cols = _get_table_columns(conn, "race_cards")

    date_col = (
        "race_date"   if "race_date"   in cols else
        "card_date"   if "card_date"   in cols else None
    )
    rno_col = (
        "race_no"     if "race_no"     in cols else
        "race_number" if "race_number" in cols else None
    )
    dist_col    = "distance_furlongs" if "distance_furlongs" in cols else None
    surface_col = "surface"           if "surface"           in cols else None

    parts = ["t.abbrev AS track"]
    if date_col:
        parts.append(f"rc2.{date_col} AS race_date")
    if rno_col:
        parts.append(f"CAST(rc2.{rno_col} AS TEXT) AS race_no")
    if dist_col:
        parts.append(f"rc2.{dist_col} AS distance_furlongs")
    if surface_col:
        parts.append(f"rc2.{surface_col} AS surface")

    row = conn.execute(
        f"SELECT {', '.join(parts)}"
        " FROM race_cards rc2"
        " LEFT JOIN tracks t ON rc2.track_id = t.track_id"
        " WHERE rc2.card_id = ?",
        (card_id,),
    ).fetchone()

    d = dict(row) if row else {}
    return {
        "race_date":         str(d.get("race_date") or ""),
        "track":             str(d.get("track")     or ""),
        "race_no":           str(d.get("race_no")   or ""),
        "distance_furlongs": float(d["distance_furlongs"]) if "distance_furlongs" in d else fallback_dist,
        "surface":           str(d.get("surface")   or fallback_surface),
    }


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


def _ensure_policy_columns(conn: sqlite3.Connection) -> None:
    for table, col in [
        ("score_runs",   "policy_surface"),
        ("score_runs",   "policy_dist_category"),
        ("score_runs",   "policy_field_size_bucket"),
        ("score_runs",   "policy_tier_selected"),
        ("score_runs",   "policy_tier_reason"),
        ("score_runs",   "policy_chaos_selected"),
        ("score_runs",   "policy_chaos_reason"),
        ("entry_scores", "policy_surface"),
        ("entry_scores", "policy_dist_category"),
        ("entry_scores", "policy_field_size_bucket"),
        ("entry_scores", "policy_tier_selected"),
        ("entry_scores", "policy_tier_reason"),
        ("entry_scores", "policy_chaos_selected"),
        ("entry_scores", "policy_chaos_reason"),
    ]:
        defn = "INTEGER" if col == "policy_chaos_selected" else "TEXT"
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
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


def _complete_live_market_probs(
    conn: sqlite3.Connection,
    card_id: int,
    entries_df: pd.DataFrame,
) -> Optional[np.ndarray]:
    """Return a normalized live-market vector only for a complete exact snapshot."""
    live_by_pp = load_live_odds_by_pp(conn, card_id)
    try:
        posts = [int(value) for value in entries_df["post_position"]]
        entry_ids = [int(value) for value in entries_df["entry_id"]]
    except (KeyError, TypeError, ValueError):
        return None
    if not live_by_pp or set(live_by_pp) != set(posts):
        return None

    decimals: list[float] = []
    for post, entry_id in zip(posts, entry_ids):
        quote = live_by_pp.get(post)
        try:
            quote_entry_id = int(quote["entry_id"])
            decimal = float(quote["decimal_odds"])
        except (KeyError, TypeError, ValueError):
            return None
        if quote_entry_id != entry_id or not np.isfinite(decimal) or decimal <= 1.0:
            return None
        decimals.append(decimal)
    raw = 1.0 / np.asarray(decimals, dtype=float)
    total = float(raw.sum())
    return raw / total if total > 0 and np.isfinite(total) else None


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
    race_meta:   dict,
    run_dir:     Path,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)

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
    board[csv_cols].to_csv(run_dir / "board.csv", index=False)

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
        f"# DerbyEdge Engine — {race_meta['track']} {race_meta['race_date']} Race {race_meta['race_no']} Board",
        "",
        "## Board Summary",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Model type | `{artifact.model_type}` |",
        f"| Version | `{artifact.version}` |",
        f"| Score timestamp | {score_ts} |",
        f"| Model ID | {model_id} |",
        f"| Race | {race_meta['track']} · {race_meta['race_date']} · Race {race_meta['race_no']} |",
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

    board_path = run_dir / "board.md"
    board_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [board]    board written -> {board_path}")


def _write_eval_report(
    metrics:   dict,
    artifact:  ModelArtifact,
    board:     pd.DataFrame,
    model_id:  int,
    run_dir:   Path,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    race_type = metrics["race_type_key"]
    path      = run_dir / "model_evaluation.md"

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

    # Reject source-invalid and baseline-only cards immediately. A pending PP
    # card is allowed to reach feature loading so the exact constructed frame
    # can be verified, but never a model call.
    try:
        _pre_feature_state = get_card_run_state(
            conn,
            card_id,
            runs_root=Path(__file__).resolve().parents[2] / "data" / "runs",
        )
        if _pre_feature_state.mode in (RunMode.BLOCKED, RunMode.MARKET_BASELINE_ONLY):
            raise ScoringBlockedError(
                f"SCORING BLOCKED [{_pre_feature_state.mode.value}]: "
                + ("; ".join(_pre_feature_state.reasons) or "Data-quality gate rejected the card.")
            )
    except Exception:
        conn.close()
        raise

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

    # ── Policy layer ──────────────────────────────────────────────────────
    _policy_dist_cat        = "sprint" if dist_furlongs < 8.5 else "route"
    _policy_field_size      = len(entries_df)
    _policy_surf_norm       = _policy_norm_surface(surface)
    policy_field_bucket     = bucket_field_size(_policy_field_size)
    policy_tier, policy_tier_reason   = choose_tier(surface, _policy_dist_cat, _policy_field_size)
    policy_chaos_default, policy_chaos_reason = policy_default_chaos(
        surface, _policy_dist_cat, _policy_field_size
    )

    # Cache race metadata for shadow log (must read while conn is open).
    # Uses _fetch_race_meta so the query adapts to whichever column names
    # exist on this DB — older schemas use card_date/race_number rather than
    # race_date/race_no, and distance_furlongs may be absent as a generated col.
    _race_meta = _fetch_race_meta(
        conn, card_id,
        fallback_surface=surface,
        fallback_dist=dist_furlongs,
    )
    run_dir = run_dir_for_card(card_id, conn=conn)
    run_key = card_run_key(
        _race_meta["track"], _race_meta["race_date"], _race_meta["race_no"]
    )

    # ── Derby override detection ───────────────────────────────────────────
    derby_active = is_derby_context(conn, card_id)
    print(f"  [scorer]   card_id={card_id}  race_type={race_type_key}  "
          f"entries={len(feat_df)}  derby_override={derby_active}")

    # Final product-state gate. This is intentionally after feature
    # construction/loading and immediately before every model predict path.
    _active_config = (
        DERBY_TRAIN_CONFIG
        if derby_active else TRAIN_CONFIGS.get(race_type_key, TRAIN_CONFIGS["dirt_route"])
    )
    _feature_verification = verify_feature_frame(
        feat_df,
        _active_config,
        expected_entries=(
            _pre_feature_state.quality.entries_parsed
            if _pre_feature_state.quality else len(entries_df)
        ),
        require_pp_backed_features=bool(
            _pre_feature_state.audit
            and _pre_feature_state.audit.get("source_provider") == "1stbet"
        ),
    )
    _effective_quality = quality_with_verified_features(
        _pre_feature_state.quality, _feature_verification
    ) if _pre_feature_state.quality else None
    if _effective_quality is None:
        conn.close()
        raise ScoringBlockedError("SCORING BLOCKED: no DataQuality could be resolved.")
    _effective_mode, _effective_reasons = resolve_mode_with_feature_checks(
        _effective_quality, _feature_verification.core_rows
    )
    _effective_reasons = list(dict.fromkeys(
        _effective_reasons + list(_feature_verification.warnings)
    ))
    if _effective_mode not in (RunMode.MODEL_READY_LIMITED, RunMode.MODEL_READY):
        conn.close()
        raise ScoringBlockedError(
            f"SCORING BLOCKED [{_effective_mode.value}]: "
            + ("; ".join(_effective_reasons) or "Feature verification failed.")
        )

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

    # ── Sanitize trained/blended probabilities for audit-only persistence ──
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

    # ── ML serving pipeline ───────────────────────────────────────────────
    _serving_mode    = _resolve_serving_mode()
    _heuristic_probs = win_probs.copy()
    _ml_win_probs    = None

    if not derby_active and _serving_mode in ("shadow", "live"):
        _ml_win_probs = _load_ml_win_probs(
            feat_df, entries_df, win_probs, market_probs,
            race_type_key, dist_furlongs, surface,
        )

    blended_probs, _ml_loaded = _decide_served_probs(
        _serving_mode, derby_active, _heuristic_probs, _ml_win_probs
    )
    print(
        f"  [scorer]   mode={_serving_mode}  ml_loaded={_ml_loaded}  "
        f"derby_override={derby_active}"
    )

    # ── Independent pre-market forecast / collapse gate ───────────────────
    # The trained/blended vector remains diagnostic only. Non-Derby display
    # and action calculations use a signal built without ML/market features or
    # market-target calibration.
    if derby_active:
        p_signal_pre_market = blended_probs.copy()
        p_model_pre_market = blended_probs.copy()
        _collapse = None
    else:
        p_signal_pre_market = pre_market_signal_probabilities(feat_df, config)
        p_model_pre_market = p_signal_pre_market.copy()
        _collapse = detect_market_prior_collapse(
            p_model_pre_market, market_probs,
            displayed_model_assigned_from_market=False,
        )

    _model_is_finite = bool(
        len(p_model_pre_market)
        and np.isfinite(p_model_pre_market).all()
        and float(p_model_pre_market.sum()) > 0
    )
    analysis_probs = (
        p_model_pre_market / p_model_pre_market.sum()
        if _model_is_finite else np.full(_n_entries, 1.0 / _n_entries)
    )
    collapsed_to_ml = bool(_collapse and _collapse.collapsed)
    effective_run_mode = (
        RunMode.MARKET_ANCHORED_NOT_ACTIONABLE
        if collapsed_to_ml else _effective_mode
    )
    if collapsed_to_ml:
        print(
            "  [scorer]   MODEL_COLLAPSED_TO_ML_PRIOR "
            f"max_delta={_collapse.max_abs_delta} mean_delta={_collapse.mean_abs_delta}"
        )

    # ── Derived scoring ────────────────────────────────────────────────────
    # Morning-line probability is never substituted for the live market.
    p_market_live = _complete_live_market_probs(conn, card_id, entries_df)
    if p_market_live is not None and not collapsed_to_ml:
        edge_vs_live_market = analysis_probs - p_market_live
        model_edge = np.round(edge_vs_live_market, 4)
    else:
        edge_vs_live_market = np.full(_n_entries, np.nan)
        model_edge = np.full(_n_entries, np.nan)
    fair_odds = (
        np.round(1.0 / np.maximum(analysis_probs, 1e-9) - 1.0, 2)
        if not collapsed_to_ml else np.full(_n_entries, np.nan)
    )
    place_probs, show_probs = _place_show_probs(analysis_probs)

    bet_thr  = config["bet_edge_threshold"]
    ul_thr   = config["underlay_edge_threshold"]
    bet_tags = [
        _bet_tag(float(edge), bet_thr, ul_thr) if np.isfinite(edge) else None
        for edge in model_edge
    ]
    rank_arr = (
        pd.to_numeric(pd.Series(analysis_probs), errors="coerce")
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
        feat_df, entries_df, analysis_probs, market_probs,
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
        entries_df, feat_df, analysis_probs, form_arr, surf_dist_arr,
        derby_active=derby_active, chaos_index=_DERBY_DEFAULT_CHAOS_INDEX,
    )
    field_entropy = float(-np.sum(analysis_probs * np.log(np.maximum(analysis_probs, 1e-9))))
    if chaos_applied:
        print(f"  [scorer]   chaos applied  intensity={chaos_intensity:.3f}  "
              f"entropy={field_entropy:.3f}")

    # ── Metrics ───────────────────────────────────────────────────────────
    metrics = _compute_metrics(analysis_probs, market_probs, artifact)
    metrics["score_ts"]          = score_ts
    metrics["bet_count"]         = sum(1 for t in final_bet_tags if t == "bet")
    metrics["blocked_bet_count"] = sum(low_conf_bet_block)

    # ── Save artifact + register model ────────────────────────────────────
    artifact_path = save_artifact(artifact)
    model_id      = register_model(artifact, artifact_path, metrics, conn)
    print(f"  [scorer]   model_id={model_id}  artifact={artifact_path.name}")

    # ── DB writes ──────────────────────────────────────────────────────────
    _ensure_chaos_columns(conn)
    _ensure_policy_columns(conn)
    ensure_score_runs_columns(conn)
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
        " chaos_active, chaos_intensity, field_entropy_score,"
        " effective_run_mode, model_collapse_status, max_abs_model_ml_delta,"
        " mean_abs_model_ml_delta, displayed_model_assigned_from_market,"
        " policy_surface, policy_dist_category, policy_field_size_bucket,"
        " policy_tier_selected, policy_tier_reason,"
        " policy_chaos_selected, policy_chaos_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, card_id, model_id, artifact.model_type, int(derby_active), quality_tier,
         int(chaos_applied), round(chaos_intensity, 4), round(field_entropy, 4),
         effective_run_mode.value,
         _collapse.status if _collapse else None,
         _collapse.max_abs_delta if _collapse else None,
         _collapse.mean_abs_delta if _collapse else None,
         0,
         _policy_surf_norm, _policy_dist_cat, policy_field_bucket,
         policy_tier, policy_tier_reason,
         int(policy_chaos_default), policy_chaos_reason),
    )

    # Keep prior run rows: the app's card-scoped selector supports audit and
    # comparison, and no score run should erase a previous score artifact.

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
        score_values = (
            run_id, eid, erow["horse_name"], int(erow["post_position"]),
            float(erow["morning_line_odds"]),
            None if collapsed_to_ml else round(float(analysis_probs[i]), 6),
            None if collapsed_to_ml else round(float(place_probs[i]), 6),
            None if collapsed_to_ml else round(float(show_probs[i]), 6),
            round(float(feat_df.iloc[i]["pace_fit_score"]) if feat_df.iloc[i]["pace_fit_score"] is not None else 0.0, 4),
            round(float(form_arr[i]), 4),
            round(float(surf_dist_arr[i]), 4),
            round(float(model_edge[i]), 4) if np.isfinite(model_edge[i]) else None,
            round(float(market_probs[i]), 6),
            round(float(market_probs[i]), 6),
            round(float(p_signal_pre_market[i]), 6) if np.isfinite(p_signal_pre_market[i]) else None,
            round(float(p_model_pre_market[i]), 6) if np.isfinite(p_model_pre_market[i]) else None,
            round(float(p_market_live[i]), 6) if p_market_live is not None else None,
            round(float(blended_probs[i]), 6),
            round(float(edge_vs_live_market[i]), 6) if np.isfinite(edge_vs_live_market[i]) else None,
            final_bet_tags[i],
            conf_flag, 1, int(low_conf_bet_block[i]), int(rank_arr[i]),
            erow.get("trainer", ""), erow.get("jockey", ""),
            round(float(chaos_scores[i]), 6) if chaos_applied else None,
            round(float(chaos_boosts[i]), 6) if chaos_applied else None,
            chaos_tiers[i] if chaos_applied else None, int(chaos_eligs[i]),
            conf_score, conf_bucket, conf_reasons,
            _policy_surf_norm, _policy_dist_cat, policy_field_bucket,
            policy_tier, policy_tier_reason,
            int(policy_chaos_default), policy_chaos_reason,
        )
        conn.execute(
            """
            INSERT INTO entry_scores (
                run_id, entry_id, horse_name, post_position,
                morning_line_odds, win_probability, place_probability, show_probability,
                pace_fit_score, form_score, surface_dist_fit, value_score,
                market_implied_prob, p_ml_implied, p_signal_pre_market,
                p_model_pre_market, p_market_live, p_model_blended,
                edge_vs_live_market, bet_tag,
                confidence_flag, missing_data_flag, low_conf_bet_block, rank,
                trainer_name, jockey_name,
                chaos_score, chaos_boost, chaos_tier, chaos_eligible,
                confidence_score, confidence_bucket, confidence_reasons,
                policy_surface, policy_dist_category, policy_field_size_bucket,
                policy_tier_selected, policy_tier_reason,
                policy_chaos_selected, policy_chaos_reason
            ) VALUES (""" + ",".join("?" for _ in score_values) + ")",
            score_values,
        )

    conn.commit()
    conn.close()

    # ── Build board DataFrame ──────────────────────────────────────────────
    board = entries_df[[
        "entry_id", "horse_name", "post_position",
        "trainer", "jockey", "morning_line_odds",
        "dist_starts",
    ]].copy().reset_index(drop=True)

    board["model_win_prob"]     = np.nan if collapsed_to_ml else analysis_probs
    board["model_win_prob_pct"] = np.round(board["model_win_prob"] * 100, 2)
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

    # ── Policy observability columns (race-level, repeated per row) ────────
    board["policy_surface"]           = _policy_surf_norm
    board["policy_dist_category"]     = _policy_dist_cat
    board["policy_field_size_bucket"] = policy_field_bucket
    board["policy_tier_selected"]     = policy_tier
    board["policy_tier_reason"]       = policy_tier_reason
    board["policy_chaos_selected"]    = int(policy_chaos_default)
    board["policy_chaos_reason"]      = policy_chaos_reason

    # Merge confidence columns
    conf_merge = conf_df[[
        "entry_id", "model_confidence", "missing_data_flags",
        "confidence_score", "confidence_bucket", "confidence_reasons",
    ]]
    board = board.merge(conf_merge, on="entry_id", how="left")
    board["dist_starts_raw"] = board["dist_starts"]  # for low-conf table

    board = board.sort_values("rank").reset_index(drop=True)

    # ── Write outputs ──────────────────────────────────────────────────────
    _write_board(
        board, run_id, model_id, artifact, metrics, score_ts,
        race_meta=_race_meta, run_dir=run_dir,
    )
    _write_eval_report(metrics, artifact, board, model_id, run_dir=run_dir)
    metadata_path = run_dir / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "run_key": run_key,
                "card_id": card_id,
                "track_abbrev": _race_meta["track"],
                "card_date": _race_meta["race_date"],
                "race_number": int(_race_meta["race_no"]),
                "surface": surface,
                "distance_furlongs": dist_furlongs,
                "model_name": artifact.model_name,
                "model_family": artifact.config["model_family"],
                "derby_override_active": derby_active,
                "effective_run_mode": effective_run_mode.value,
                "model_collapse_status": _collapse.status if _collapse else None,
                "max_abs_model_ml_delta": _collapse.max_abs_delta if _collapse else None,
                "mean_abs_model_ml_delta": _collapse.mean_abs_delta if _collapse else None,
                "scored_at": score_ts,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"  [output]   run assets -> {run_dir}")

    low_conf_n = int((board["model_confidence"] == "low").sum())
    blocked_n = metrics.get("blocked_bet_count", 0)
    print(f"  [scorer]   run_id={run_id}  sum_win_prob={metrics['sum_win_prob']:.6f}  "
          f"bets={metrics['bet_count']}  blocked={blocked_n}  "
          f"underlays={metrics['underlay_count']}  low_conf={low_conf_n}")

    # ── Shadow log ─────────────────────────────────────────────────────────
    if _serving_mode in ("shadow", "live"):
        _emit_shadow_log(
            card_id=card_id,
            race_meta=_race_meta,
            entries_df=entries_df,
            heuristic_probs=_heuristic_probs,
            ml_probs=_ml_win_probs,
            served_probs=blended_probs,
            ml_loaded_flag=_ml_loaded,
            mode=_serving_mode,
            model_version=artifact.version,
            dist_furlongs=dist_furlongs,
            surface=surface,
            derby_override=derby_active,
            segment=race_type_key,
            scored_at=score_ts,
            policy_surface=_policy_surf_norm,
            policy_dist_category=_policy_dist_cat,
            policy_field_size_bucket=policy_field_bucket,
            policy_tier_selected=policy_tier,
            policy_tier_reason=policy_tier_reason,
            policy_chaos_selected=int(policy_chaos_default),
            policy_chaos_reason=policy_chaos_reason,
        )

    return board


def score_derby() -> pd.DataFrame:
    """Alias for backwards compatibility with scripts/score.py."""
    return score_race(card_id=None)
