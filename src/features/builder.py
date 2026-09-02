"""
DerbyEdge V1  —  Feature Store Builder
src/features/builder.py

Feature tiers used throughout this module and in feature_catalog.csv:
  IMPLEMENTED  — computed directly from seed columns; no proxying
  DEGRADED     — proxy construction from aggregate seed fields;
                 formula is honest but less precise than row-level history
  PLACEHOLDER  — in catalog but null; source table (horse_starts /
                 workouts / track_bias / trip_flags) is empty for seed-only install
"""

import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Sire route-aptitude lookup  (Classic Distance Projection proxy)
# Scores reflect published sire-line bias toward routes >= 1.25 miles.
# Horses whose sire is absent receive _DEFAULT_SIRE_ROUTE.
# ---------------------------------------------------------------------------
SIRE_ROUTE_SCORE: dict[str, float] = {
    "tapit":               0.90,
    "curlin":              0.90,
    "flatter":             0.85,
    "medaglia d'oro":      0.85,
    "american pharoah":    0.85,
    "pioneerof the nile":  0.85,
    "bernardini":          0.85,
    "justify":             0.85,
    "giant's causeway":    0.80,
    "empire maker":        0.80,
    "candy ride":          0.80,
    "kitten's joy":        0.80,
    "street sense":        0.80,
    "into mischief":       0.75,
    "honor code":          0.75,
    "nyquist":             0.75,
    "california chrome":   0.75,
    "war front":           0.70,
    "speightstown":        0.60,
}
_DEFAULT_SIRE_ROUTE = 0.72   # field mean when sire not in table

# Tier 1: empirical win-rate by layoff bucket (days since last race).
# Buckets: 0=0-13d  1=14-27d  2=28-55d  3=56-119d  4=120+d
_LAYOFF_WIN_RATE: dict[int, float] = {0: 0.18, 1: 0.21, 2: 0.23, 3: 0.17, 4: 0.12}

# Pace style -> early-fraction commitment score (0 = pure closer, 1 = pure front)
PACE_EARLY: dict[str, float] = {
    "front": 1.00, "presser": 0.70, "stalker": 0.40, "closer": 0.10
}


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------
def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den > 0 else default


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _norm(x: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 0.5
    return _clamp((x - lo) / (hi - lo))


def _f(val) -> Optional[float]:
    """Return float or None; avoids NaN leaking into the DB."""
    if val is None:
        return None
    try:
        v = float(val)
        return None if np.isnan(v) else v
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# First pass: entry-level features (no cross-horse comparison yet)
# ---------------------------------------------------------------------------
def _entry_features(row: pd.Series, field_df: pd.DataFrame) -> dict:
    """
    Compute per-entry features that can be derived from a single row.
    Race-shape and publicness fields are left as None here and filled
    in _fill_race_level_features().
    """
    sire       = str(row.get("sire")       or "").strip().lower()
    pace_style = str(row.get("pace_style") or "").strip().lower()

    # ── Speed / pace / form ────────────────────────────────────────────────
    speed_last = _f(row.get("last_speed_fig"))
    speed_best = _f(row.get("best_speed_fig"))
    speed_avg  = _f(row.get("avg_speed_fig"))
    beyer_last = _f(row.get("beyer_fig"))

    # DEGRADED: true best-of-3 needs race-by-race figs; use average of the
    # three aggregate seed figures as the closest available proxy.
    if None not in (speed_last, speed_best, speed_avg):
        speed_best_3 = round((speed_best + speed_last + speed_avg) / 3.0, 2)
    else:
        speed_best_3 = None

    # PLACEHOLDER
    pace_early_mean_3 = None   # needs horse_starts call-fraction splits
    pace_mid_mean_3   = None   # needs horse_starts call-fraction splits

    # DEGRADED: finish_energy_proxy
    last_finish = _f(row.get("last_race_finish"))
    if pace_style in PACE_EARLY and last_finish is not None:
        style_reserve       = 1.0 - PACE_EARLY[pace_style]
        finish_perf         = _clamp(1.0 - (last_finish - 1.0) / 19.0)
        finish_energy_proxy = round(0.5 * style_reserve + 0.5 * finish_perf, 4)
    else:
        finish_energy_proxy = None

    layoff_days = row.get("last_race_days")
    if layoff_days is not None:
        try:
            layoff_days = int(layoff_days)
        except (TypeError, ValueError):
            layoff_days = None

    # T1: layoff_bucket_encoded — map rest period to empirical win-rate shape
    if layoff_days is not None:
        _lbucket = pd.cut(
            [layoff_days], bins=[-1, 13, 27, 55, 119, 9999], labels=[0, 1, 2, 3, 4]
        )[0]
        layoff_bucket_encoded: Optional[float] = (
            _LAYOFF_WIN_RATE.get(int(_lbucket), 0.20)
            if _lbucket is not None and not pd.isna(_lbucket)
            else 0.20
        )
    else:
        layoff_bucket_encoded = None

    cs = float(row.get("career_starts") or 0)
    cw = float(row.get("career_wins")   or 0)
    cp = float(row.get("career_places") or 0)
    ch = float(row.get("career_shows")  or 0)
    career_win_pct = round(_safe_div(cw, cs), 4) if cs > 0 else None
    career_itm_pct = round(_safe_div(cw + cp + ch, cs), 4) if cs > 0 else None

    # DEGRADED: form_cycle_idx
    if career_itm_pct is not None and last_finish is not None:
        recency_adj    = _clamp(1.0 - (last_finish - 1.0) / 10.0)
        form_cycle_idx = round(0.6 * career_itm_pct + 0.4 * recency_adj, 4)
    else:
        form_cycle_idx = None

    # ── Class / field strength ─────────────────────────────────────────────
    career_earnings = float(row.get("career_earnings") or 0)
    class_level     = round(np.log(career_earnings + 1), 4)   # T1: log-earnings proxy
    field_earn      = field_df["career_earnings"].dropna().astype(float)
    if len(field_earn) > 1 and field_earn.std() > 0:
        class_delta = round(
            (career_earnings - field_earn.mean()) / field_earn.std(), 4
        )
    else:
        class_delta = 0.0

    field_strength_last    = None   # PLACEHOLDER: needs competitors' horse_starts figs

    # DEGRADED: assume typical non-Derby field is 10 runners
    if last_finish is not None:
        typ = 10
        horses_beaten_pct_last = round(
            _clamp((typ - last_finish) / max(typ - 1, 1), -0.2, 1.0), 4
        )
    else:
        horses_beaten_pct_last = None

    # T1: horses_beaten_pct_actual — uses real field_size_last when available
    field_size_last = row.get("field_size_last")
    if field_size_last is not None and last_finish is not None:
        _fsz = int(field_size_last)
        _hbp = (float(_fsz) - float(last_finish)) / max(_fsz - 1, 1)
        horses_beaten_pct_actual = round(_clamp(_hbp, 0.0, 1.0), 4)
    else:
        horses_beaten_pct_actual = horses_beaten_pct_last   # fall back to degraded

    # DEGRADED: career starts proxy for large-field experience
    field_size_exp = round(_norm(cs, 1.0, 15.0), 4) if cs > 0 else None

    # ── Workouts / readiness ───────────────────────────────────────────────
    works_30d          = row.get("workouts_30")
    if works_30d is not None:
        try:
            works_30d = int(works_30d)
        except (TypeError, ValueError):
            works_30d = None

    bullet_30d           = None   # PLACEHOLDER: needs workouts table grade='B'
    days_since_last_work = None   # PLACEHOLDER: needs workouts table

    gate_class_raw = _f(row.get("gate_class"))
    if works_30d is not None and gate_class_raw is not None:
        work_count_score     = _clamp(works_30d / 6.0)
        gate_norm            = _clamp(gate_class_raw / 5.0)
        work_readiness_score = round(0.6 * work_count_score + 0.4 * gate_norm, 4)
    else:
        work_readiness_score = None

    # DEGRADED: trainer_intent_proxy
    if works_30d is not None and layoff_days is not None:
        work_load            = _clamp(works_30d / 6.0)
        freshness            = _clamp(1.0 - max(0.0, layoff_days - 14.0) / 56.0)
        trainer_intent_proxy = round(0.5 * work_load + 0.5 * freshness, 4)
    else:
        trainer_intent_proxy = None

    # PLACEHOLDER — conditioned historical stats
    trainer_jockey_itm_cond = None
    jockey_route_cond       = None
    trainer_derby_cond      = None

    # ── Fit ────────────────────────────────────────────────────────────────
    dirt_starts = float(row.get("dirt_starts") or 0)
    dirt_wins   = float(row.get("dirt_wins")   or 0)
    if dirt_starts >= 2:
        surface_fit = round(_safe_div(dirt_wins, dirt_starts), 4)
    elif dirt_starts == 1:
        surface_fit = round(0.5 * _safe_div(dirt_wins, dirt_starts), 4)
    else:
        surface_fit = None   # no dirt experience; leave null, not defaulted

    dist_starts   = float(row.get("dist_starts") or 0)
    dist_wins     = float(row.get("dist_wins")   or 0)
    stamina_index = _f(row.get("stamina_index"))
    if dist_starts >= 1 and stamina_index is not None:
        dist_win_pct = _safe_div(dist_wins, dist_starts)
        distance_fit = round(0.55 * dist_win_pct + 0.45 * stamina_index, 4)
    elif stamina_index is not None:
        distance_fit = round(0.45 * stamina_index, 4)
    else:
        distance_fit = None

    route_progression    = distance_fit   # same formula; Derby always a route
    pedigree_route_proxy = SIRE_ROUTE_SCORE.get(sire, _DEFAULT_SIRE_ROUTE)

    # ── Post / trip / bias ─────────────────────────────────────────────────
    post_win_bias          = None   # PLACEHOLDER: needs track_bias / post history
    gate_reliability       = round(_clamp(gate_class_raw / 5.0), 4) if gate_class_raw is not None else None
    trouble_recovery_proxy = None   # PLACEHOLDER: needs trip_flags

    # DEGRADED: closers in big fields face more traffic; experience mitigates it
    if pace_style in PACE_EARLY:
        style_exposure           = 1.0 - PACE_EARLY[pace_style]
        exp_score                = field_size_exp if field_size_exp is not None else 0.5
        traffic_resilience_proxy = round(
            0.5 * (1.0 - style_exposure) + 0.5 * exp_score, 4
        )
    else:
        traffic_resilience_proxy = None

    # ── Race shape: entry portion ──────────────────────────────────────────
    early_intent     = PACE_EARLY.get(pace_style)
    run_style_bucket = pace_style if pace_style in PACE_EARLY else None

    # ── Market / publicness ────────────────────────────────────────────────
    ml_odds             = float(row.get("morning_line_odds") or 1)
    market_implied_prob = round(1.0 / (ml_odds + 1.0), 6)

    if career_win_pct and career_win_pct > 0:
        publicness_score = round(market_implied_prob / career_win_pct, 4)
    else:
        publicness_score = None

    # ── Derby override: entry portion ─────────────────────────────────────
    if stamina_index is not None and dist_starts >= 1:
        classic_distance_projection = round(
            0.60 * stamina_index + 0.40 * _safe_div(dist_wins, dist_starts), 4
        )
    elif stamina_index is not None:
        classic_distance_projection = round(0.60 * stamina_index, 4)
    else:
        classic_distance_projection = None

    churchill_readiness       = None   # PLACEHOLDER: needs Churchill historical data
    jan_apr_improvement_curve = None   # PLACEHOLDER: needs sequential speed figs

    return {
        # identity
        "_last_race_finish":           last_finish,   # passthrough for firstbet overlay; dropped before DB write
        "speed_last":                  speed_last,
        "speed_best":                  speed_best,
        "speed_avg":                   speed_avg,
        "beyer_last":                  beyer_last,
        "speed_best_3":                speed_best_3,
        "pace_early_mean_3":           pace_early_mean_3,
        "pace_mid_mean_3":             pace_mid_mean_3,
        "finish_energy_proxy":         finish_energy_proxy,
        "form_cycle_idx":              form_cycle_idx,
        "layoff_days":                 layoff_days,
        "layoff_bucket_encoded":       layoff_bucket_encoded,      # T1
        "career_win_pct":              career_win_pct,
        "career_itm_pct":              career_itm_pct,
        "class_delta":                 class_delta,
        "class_level":                 class_level,                # T1 proxy
        "field_strength_last":         field_strength_last,
        "horses_beaten_pct_last":      horses_beaten_pct_last,
        "horses_beaten_pct_actual":    horses_beaten_pct_actual,   # T1
        "field_size_exp":              field_size_exp,
        "works_30d":                   works_30d,
        "bullet_30d":                  bullet_30d,
        "days_since_last_work":        days_since_last_work,
        "work_readiness_score":        work_readiness_score,
        "trainer_intent_proxy":        trainer_intent_proxy,
        "trainer_jockey_itm_cond":     trainer_jockey_itm_cond,
        "jockey_route_cond":           jockey_route_cond,
        "trainer_derby_cond":          trainer_derby_cond,
        "surface_fit":                 surface_fit,
        "distance_fit":                distance_fit,
        "route_progression":           route_progression,
        "pedigree_route_proxy":        pedigree_route_proxy,
        "post_win_bias":               post_win_bias,
        "gate_reliability":            gate_reliability,
        "trouble_recovery_proxy":      trouble_recovery_proxy,
        "traffic_resilience_proxy":    traffic_resilience_proxy,
        "early_intent":                early_intent,
        "run_style_bucket":            run_style_bucket,
        "speed_fig_adj":               None,   # filled in second pass
        "class_delta_v2":              None,   # filled in second pass
        "pace_pressure":               None,   # filled in second pass
        "pace_pressure_tier":          None,   # filled in second pass
        "lone_speed_edge":             None,   # filled in second pass
        "collapse_risk":               None,   # filled in second pass
        "collapse_risk_v2":            None,   # filled in second pass
        "pace_fit_score":              None,   # filled in second pass
        "morning_line_delta":          None,   # filled in second pass
        "market_implied_prob":         market_implied_prob,
        "morning_line_rank":           None,   # filled in second pass
        "publicness_score":            publicness_score,
        "public_underlay_penalty":     None,   # filled in second pass
        "classic_distance_projection": classic_distance_projection,
        "churchill_readiness":         churchill_readiness,
        "jan_apr_improvement_curve":   jan_apr_improvement_curve,
        "derby_override_score":        None,   # filled in second pass
    }


# ---------------------------------------------------------------------------
# Second pass: race-level features require full-field context
# ---------------------------------------------------------------------------
def _fill_race_level_features(
    df: pd.DataFrame,
    *,
    derby_active: bool,
) -> pd.DataFrame:
    """Mutates df in place; returns it."""

    # morning_line_rank (1 = shortest price)
    df["morning_line_rank"] = (
        df["market_implied_prob"]
        .rank(ascending=False, method="min")
        .astype(int)
    )

    # T1: speed_fig_adj — z-score of speed_last within field, clipped ±3
    _sl = pd.to_numeric(df["speed_last"], errors="coerce")
    if not _sl.isna().all():
        _sl_std = max(float(_sl.std()), 1.0)
        df["speed_fig_adj"] = ((_sl - float(_sl.mean())) / _sl_std).clip(-3, 3).round(4)
    else:
        df["speed_fig_adj"] = 0.0

    # pace shape counts
    front_count   = int((df["run_style_bucket"] == "front").sum())
    presser_count = int((df["run_style_bucket"] == "presser").sum())
    total         = len(df)
    pace_pressure = round((front_count + presser_count) / max(total, 1), 4)

    df["pace_pressure"] = pace_pressure
    df["collapse_risk"] = pace_pressure   # semantic alias

    # T1: pace_pressure_tier and collapse_risk_v2
    front_pct   = front_count / max(total, 1)
    presser_pct = presser_count / max(total, 1)
    if front_count == 1 and presser_pct < 0.15:
        _tier = 0   # lone speed
    elif front_pct < 0.15 and presser_pct < 0.20:
        _tier = 1   # soft
    elif front_pct <= 0.30:
        _tier = 2   # moderate
    else:
        _tier = 3   # contested
    df["pace_pressure_tier"] = _tier
    df["collapse_risk_v2"]   = round(front_pct * 1.0 + presser_pct * 0.5, 4)

    df["lone_speed_edge"] = df["run_style_bucket"].apply(
        lambda s: 1 if s == "front" and front_count == 1 else 0
    )

    def _pace_fit(row) -> float:
        style = row["run_style_bucket"]
        if not style:
            return 0.65   # neutral mid-field prior; no pace style in entries
        if style == "front":
            return 0.90 if front_count == 1 else 0.55
        if style == "presser":
            return 0.75 if pace_pressure < 0.35 else 0.65
        if style == "stalker":
            return 0.70 if pace_pressure >= 0.30 else 0.60
        if style == "closer":
            return 0.80 if pace_pressure >= 0.40 else 0.55
        return 0.65

    df["pace_fit_score"] = (
        pd.to_numeric(df.apply(_pace_fit, axis=1), errors="coerce")
        .fillna(0.65)
        .round(4)
    )

    n_pace_defaults = int(
        (df["run_style_bucket"].isna() | (df["run_style_bucket"] == "")).sum()
    )
    if n_pace_defaults:
        print(
            f"[builder] pace_fit_score defaulted to 0.65 for {n_pace_defaults} "
            f"row(s) — no pace style in entries (sparse/screenshot race)"
        )

    # public underlay penalty: z-score of publicness_score within field, scaled 0-1
    ps = df["publicness_score"].dropna()
    if len(ps) > 1 and ps.std() > 0:
        ps_mean = float(ps.mean())
        ps_std  = float(ps.std())

        def _underlay(row) -> float:
            v = row["publicness_score"]
            if v is None:
                return 0.5   # neutral when publicness data absent
            try:
                if np.isnan(float(v)):
                    return 0.5
            except (TypeError, ValueError):
                return 0.5
            z = (float(v) - ps_mean) / ps_std
            return round(_clamp(z / 3.0 + 0.5), 4)

        df["public_underlay_penalty"] = (
            pd.to_numeric(df.apply(_underlay, axis=1), errors="coerce")
            .fillna(0.5)
        )
    else:
        df["public_underlay_penalty"] = 0.5   # neutral numeric default

    # Derby-only projections must never cross into an ordinary race model.
    # Keep the schema stable, but persist these fields as NULL outside the
    # narrowly defined Kentucky Derby context.
    if not derby_active:
        for col in (
            "classic_distance_projection",
            "churchill_readiness",
            "jan_apr_improvement_curve",
            "derby_override_score",
        ):
            df[col] = None
    else:
        # derby_override_score: weighted composite of available proxies
        def _derby_override(row) -> Optional[float]:
            parts: list[tuple[float, float]] = []
            if row["classic_distance_projection"] is not None:
                parts.append((float(row["classic_distance_projection"]), 0.35))
            if row["pedigree_route_proxy"] is not None:
                parts.append((float(row["pedigree_route_proxy"]),        0.20))
            if row["pace_fit_score"] is not None:
                parts.append((float(row["pace_fit_score"]),              0.20))
            if row["work_readiness_score"] is not None:
                parts.append((float(row["work_readiness_score"]),        0.15))
            if row["gate_reliability"] is not None:
                parts.append((float(row["gate_reliability"]),            0.10))
            if not parts:
                return None
            total_w = sum(w for _, w in parts)
            score   = sum(v * w for v, w in parts) / total_w
            return round(score, 4)

        df["derby_override_score"] = pd.to_numeric(
            df.apply(_derby_override, axis=1), errors="coerce"
        )

    # T1: class_delta_v2 — z-score of log-earnings within field
    if "class_level" in df.columns:
        _le     = pd.to_numeric(df["class_level"], errors="coerce")
        _le_std = max(float(_le.std()), 0.01)
        df["class_delta_v2"] = ((_le - float(_le.mean())) / _le_std).round(4)

    # T1: morning_line_delta — deviation of market_implied_prob from uniform prior
    df["morning_line_delta"] = (df["market_implied_prob"] - 1.0 / len(df)).round(6)

    return df


def _is_derby_context(conn, card_id: int) -> bool:
    """True only for the Kentucky Derby profile that permits Derby features."""
    row = conn.execute(
        """
        SELECT rc.surface, rc.distance_furlongs, rc.field_size, rc.stakes_name,
               t.abbrev AS track_abbrev
        FROM race_cards rc
        JOIN tracks t ON t.track_id = rc.track_id
        WHERE rc.card_id = ?
        """,
        (card_id,),
    ).fetchone()
    return bool(row) and (
        str(row["surface"] or "").lower() == "dirt"
        and float(row["distance_furlongs"] or 0) >= 9.5
        and int(row["field_size"] or 0) >= 18
        and "derby" in str(row["stakes_name"] or "").lower()
        and str(row["track_abbrev"] or "").upper() == "CD"
    )


# ---------------------------------------------------------------------------
# 1/ST BET enrichment overlay
# Fills null feature cells from firstbet_career_stats / firstbet_pp_starts.
# Never overwrites a non-null value; silently skips when tables are absent.
# ---------------------------------------------------------------------------
def _isnull(v) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _parse_furlongs(dist_text: str) -> Optional[float]:
    """Convert PP distance text (e.g. '8.3F', '1M', '1 1/16M') to furlongs."""
    s = str(dist_text).strip().upper()
    try:
        if s.endswith("F"):
            return float(s[:-1])
        if s.endswith("M"):
            body = s[:-1].strip()
            parts = body.split()
            if len(parts) == 2:
                whole = float(parts[0])
                num, den = parts[1].split("/")
                return (whole + float(num) / float(den)) * 8.0
            return float(parts[0]) * 8.0
    except (ValueError, IndexError, ZeroDivisionError):
        pass
    return None


def _apply_firstbet_overlay(
    feat_df: pd.DataFrame,
    card_id: int,
    conn,
) -> tuple[pd.DataFrame, bool]:
    """Fill null feature cells from firstbet_career_stats and firstbet_pp_starts.

    Returns (updated feat_df copy, any_cells_filled: bool).
    """
    try:
        cs_df = pd.read_sql(
            "SELECT entry_id, career_win_pct, career_itm_pct "
            "FROM firstbet_career_stats WHERE card_id=?",
            conn, params=(card_id,),
        )
        pp_df = pd.read_sql(
            "SELECT entry_id, start_rank, finish_position, field_size, "
            "surface, distance_text "
            "FROM firstbet_pp_starts WHERE card_id=? ORDER BY entry_id, start_rank",
            conn, params=(card_id,),
        )
    except Exception:
        return feat_df, False

    if cs_df.empty and pp_df.empty:
        return feat_df, False

    try:
        rc = conn.execute(
            "SELECT distance_furlongs FROM race_cards WHERE card_id=?", (card_id,)
        ).fetchone()
        race_dist_f: Optional[float] = float(rc["distance_furlongs"]) if rc else None
    except Exception:
        race_dist_f = None

    any_filled = False
    feat_df = feat_df.copy()

    for idx in feat_df.index:
        eid = int(feat_df.at[idx, "entry_id"])

        # ── Career stats overlay ───────────────────────────────────────────
        cs_rows = cs_df[cs_df["entry_id"] == eid]
        if not cs_rows.empty:
            cs = cs_rows.iloc[0]

            if _isnull(feat_df.at[idx, "career_win_pct"]):
                val = cs.get("career_win_pct")
                if not _isnull(val):
                    feat_df.at[idx, "career_win_pct"] = round(float(val), 4)
                    any_filled = True

            if _isnull(feat_df.at[idx, "career_itm_pct"]):
                val = cs.get("career_itm_pct")
                if not _isnull(val):
                    feat_df.at[idx, "career_itm_pct"] = round(float(val), 4)
                    any_filled = True

            # form_cycle_idx: recompute now that career_itm_pct may be filled
            if _isnull(feat_df.at[idx, "form_cycle_idx"]):
                cip = feat_df.at[idx, "career_itm_pct"]
                lf  = feat_df.at[idx, "_last_race_finish"]
                if not _isnull(cip) and not _isnull(lf):
                    recency_adj = _clamp(1.0 - (float(lf) - 1.0) / 10.0)
                    feat_df.at[idx, "form_cycle_idx"] = round(
                        0.6 * float(cip) + 0.4 * recency_adj, 4
                    )
                    any_filled = True

            # publicness_score: recompute now that career_win_pct may be filled
            if _isnull(feat_df.at[idx, "publicness_score"]):
                cwp = feat_df.at[idx, "career_win_pct"]
                mip = feat_df.at[idx, "market_implied_prob"]
                if not _isnull(cwp) and float(cwp) > 0 and not _isnull(mip):
                    feat_df.at[idx, "publicness_score"] = round(
                        float(mip) / float(cwp), 4
                    )
                    any_filled = True

        # ── PP starts overlay ─────────────────────────────────────────────
        horse_pp = pp_df[pp_df["entry_id"] == eid]
        if horse_pp.empty:
            continue

        # surface_fit: wins on dirt / dirt starts (replaces entries.dirt_wins=0 default)
        dirt_mask = horse_pp["surface"].str.lower().str.startswith("dirt", na=False)
        dirt_pp   = horse_pp[dirt_mask]
        if len(dirt_pp) > 0:
            dirt_wins_pp = int((dirt_pp["finish_position"] == 1).sum())
            new_sf = round(_safe_div(dirt_wins_pp, len(dirt_pp)), 4)
            cur_sf = feat_df.at[idx, "surface_fit"]
            if _isnull(cur_sf) or float(cur_sf) == 0.0:
                feat_df.at[idx, "surface_fit"] = new_sf
                any_filled = True

        # distance_fit: wins at matching distance / dist starts from PP data
        if race_dist_f is not None and _isnull(feat_df.at[idx, "distance_fit"]):
            dist_wins_pp  = 0
            dist_total_pp = 0
            for _, prow in horse_pp.iterrows():
                f = _parse_furlongs(str(prow.get("distance_text") or ""))
                if f is not None and abs(f - race_dist_f) <= 1.0:
                    dist_total_pp += 1
                    if int(prow.get("finish_position") or 99) == 1:
                        dist_wins_pp += 1
            if dist_total_pp > 0:
                dfit = round(_safe_div(dist_wins_pp, dist_total_pp), 4)
                feat_df.at[idx, "distance_fit"]    = dfit
                feat_df.at[idx, "route_progression"] = dfit
                any_filled = True

        # horses_beaten_pct_last: use actual field size from most recent PP start
        lf = feat_df.at[idx, "_last_race_finish"]
        if not _isnull(lf):
            recent_pp = horse_pp[horse_pp["start_rank"] == 1]
            if not recent_pp.empty:
                fsz = recent_pp.iloc[0].get("field_size")
                if not _isnull(fsz) and int(fsz) > 1:
                    n = int(fsz)
                    feat_df.at[idx, "horses_beaten_pct_last"] = round(
                        _clamp((n - float(lf)) / max(n - 1, 1), -0.2, 1.0), 4
                    )
                    any_filled = True

    n_filled = sum(
        1 for col in [
            "career_win_pct", "career_itm_pct", "form_cycle_idx",
            "surface_fit", "distance_fit", "publicness_score",
        ]
        for idx in feat_df.index
        if not _isnull(feat_df.at[idx, col])
    )
    if any_filled:
        print(f"[builder] firstbet overlay: {n_filled} feature cells now non-null for card_id={card_id}")

    return feat_df, any_filled


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_features(card_id: Optional[int] = None) -> pd.DataFrame:
    """
    Build the feature store for one race card.

    If card_id is None uses the first Kentucky Derby card found.
    Deletes any previous feature_store rows for this card, then inserts fresh ones.
    Returns a DataFrame with one row per entry (20 for the Derby seed).
    """
    from src.utils.db import get_connection, get_derby_card_id

    conn = get_connection()

    if card_id is None:
        card_id = get_derby_card_id()
    if card_id is None:
        conn.close()
        raise RuntimeError("No Kentucky Derby card found — run ingest first.")

    df = pd.read_sql(
        "SELECT * FROM v_entries_live WHERE card_id = ? ORDER BY post_position",
        conn,
        params=(card_id,),
    )
    if df.empty:
        conn.close()
        raise RuntimeError(f"No entries for card_id={card_id} — run ingest first.")

    derby_active = _is_derby_context(conn, card_id)

    build_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = []
    for _, row in df.iterrows():
        feats = _entry_features(row, df)
        feats["entry_id"]      = int(row["entry_id"])
        feats["horse_id"]      = int(row["horse_id"])
        feats["card_id"]       = int(row["card_id"])
        feats["horse_name"]    = str(row["horse_name"])
        feats["post_position"] = int(row["post_position"])
        feats["build_ts"]      = build_ts
        rows.append(feats)

    feat_df = pd.DataFrame(rows)

    try:
        feat_df, _ = _apply_firstbet_overlay(feat_df, card_id, conn)
    except Exception as exc:
        print(f"[builder] firstbet overlay skipped: {exc}")

    feat_df = _fill_race_level_features(feat_df, derby_active=derby_active)

    # Drop helper columns that are not in feature_store schema
    _drop = [c for c in feat_df.columns if c.startswith("_")]
    if _drop:
        feat_df = feat_df.drop(columns=_drop)

    conn.execute("DELETE FROM feature_store WHERE card_id = ?", (card_id,))
    feat_df.to_sql("feature_store", conn, if_exists="append", index=False)
    conn.commit()
    conn.close()

    return feat_df
