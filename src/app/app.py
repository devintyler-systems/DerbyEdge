"""
DerbyEdge V1  —  Operator Console
src/app/app.py

Run: streamlit run src/app/app.py
"""

import math
import os
import pickle
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.utils.db import get_connection
from src.derbyedge.odds_math import kelly_fraction, kelly_fraction_full
from src.derbyedge.chaos_patch import apply_derby_chaos_patch
from src.services.odds_intake import (
    delete_odds_for_race,
    generate_template,
    has_race_identity,
    ingest_new_race_odds_csv,
    ingest_odds_csv,
    load_latest_snapshot_meta,
    load_live_odds_by_pp,
)
from src.services.race_card_builder import (
    create_race_from_screenshot_result,
    find_race_card,
    find_or_create_race,
    parse_distance_yards as _rcb_parse_distance,
    norm_surface as _rcb_norm_surface,
)
from src.services.pdf_ingest import parse_race_pdf, parse_results_pdf
from src.services.pp_intake import (
    ingest_pp_rows,
    parse_pp_csv,
    preview_pp_match,
)
from src.services.results_intake import (
    delete_results_for_race,
    evaluate_score_run,
    ingest_results,
    load_outcomes_frame,
    load_results_summary,
    parse_results_csv,
    preview_results_match,
)
from src.services.screenshot_ingest import ingest_sportsbook_screenshot
from src.services.race_admin import (
    ensure_is_hidden_column,
    get_race_info as _admin_get_race_info,
    get_race_dependencies,
    update_race_card as _admin_update_race,
    soft_delete_race,
    hard_delete_race,
)
from src.services.firstbet_enrich import (
    ensure_firstbet_pp_table,
    enrich_runners_1stbet,
    enrich_entries_from_1stbet,
)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="DerbyEdge Operator Console",
    page_icon="⚙",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #0d1117; }
[data-testid="stHeader"]           { background: transparent; }
[data-testid="stSidebar"]          { background: #0d1117; border-right: 1px solid #30363d; }

.console-title  { font-size:1.6rem; font-weight:700; color:#e6edf3; letter-spacing:.5px; }
.console-sub    { font-size:.85rem; color:#8b949e; margin-top:-6px; }
.status-badge   { display:inline-block; padding:2px 10px; border-radius:12px;
                  font-size:.78rem; font-weight:600; letter-spacing:.4px; }
.badge-seed     { background:#2d333b; color:#f0883e; border:1px solid #f0883e55; }
.badge-proxy    { background:#1a2535; color:#c9a227; border:1px solid #c9a22755; }
.badge-xgb      { background:#162a1e; color:#3fb950; border:1px solid #3fb95055; }
.badge-bet      { background:#162a1e; color:#3fb950; border:1px solid #3fb950; }
.badge-underlay { background:#2d1c1f; color:#f85149; border:1px solid #f85149; }
.badge-neutral  { background:#1c2128; color:#8b949e; border:1px solid #30363d; }
.badge-med      { background:#1c2535; color:#4facfe; border:1px solid #4facfe55; }
.badge-low      { background:#2b2008; color:#d29922; border:1px solid #d2992255; }
.badge-impl     { background:#162a1e; color:#3fb950; }
.badge-deg      { background:#2b2008; color:#d29922; }
.badge-phld     { background:#1c2128; color:#6e7681; }
.warn-banner    { background:#2d1c1f; border-left:3px solid #f85149;
                  padding:8px 12px; border-radius:0 6px 6px 0;
                  font-size:.85rem; color:#f85149; margin-bottom:8px; }
.info-banner    { background:#1c2535; border-left:3px solid #4facfe;
                  padding:8px 12px; border-radius:0 6px 6px 0;
                  font-size:.85rem; color:#4facfe; margin-bottom:8px; }
.kv-row         { display:flex; justify-content:space-between;
                  padding:4px 0; border-bottom:1px solid #21262d; font-size:.85rem; }
.kv-key         { color:#8b949e; }
.kv-val         { color:#e6edf3; font-weight:500; }
</style>
""", unsafe_allow_html=True)

# ── Constants ─────────────────────────────────────────────────────────────────
TIER_BADGE = {
    "IMPLEMENTED": '<span class="status-badge badge-impl">IMPL</span>',
    "DEGRADED":    '<span class="status-badge badge-deg">DEG</span>',
    "PLACEHOLDER": '<span class="status-badge badge-phld">PHLD</span>',
}
TAG_BADGE = {
    "bet":     '<span class="status-badge badge-bet">BET</span>',
    "underlay":'<span class="status-badge badge-underlay">UL</span>',
    "neutral": '<span class="status-badge badge-neutral">—</span>',
}
CONF_BADGE = {
    "medium": '<span class="status-badge badge-med">MED</span>',
    "low":    '<span class="status-badge badge-low">LOW!</span>',
}
TAG_ICON  = {"bet": "🟢 BET", "underlay": "🔴 UL",  "neutral": "—"}
CONF_ICON = {1: "🔵 MED",    0: "🟡 LOW"}


# ── Data loaders ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=30)
def load_board(run_id: str | None = None) -> tuple[pd.DataFrame | None, dict | None]:
    try:
        conn = get_connection()
        if run_id is None:
            run = conn.execute(
                "SELECT run_id FROM score_runs ORDER BY run_timestamp DESC LIMIT 1"
            ).fetchone()
            if not run:
                conn.close()
                return None, None
            run_id = run["run_id"]

        df = pd.read_sql(
            """
            SELECT es.rank, es.horse_name, es.post_position,
                   es.morning_line_odds,
                   es.win_probability, es.place_probability, es.show_probability,
                   es.fair_odds, es.value_score, es.bet_tag,
                   es.pace_fit_score, es.form_score, es.surface_dist_fit,
                   es.market_implied_prob,
                   es.confidence_flag, es.missing_data_flag,
                   vel.trainer, vel.jockey, vel.sire, vel.dam, vel.owner,
                   vel.pace_style,
                   vel.career_starts, vel.career_wins, vel.career_places,
                   vel.career_shows, vel.career_earnings,
                   vel.last_race_days, vel.last_race_finish,
                   vel.best_speed_fig, vel.last_speed_fig,
                   vel.avg_speed_fig, vel.beyer_fig,
                   vel.dirt_starts, vel.dirt_wins,
                   vel.dist_starts, vel.dist_wins,
                   vel.workouts_30, vel.gate_class, vel.stamina_index
            FROM entry_scores es
            JOIN v_entries_live vel ON es.entry_id = vel.entry_id
            WHERE es.run_id = ?
            ORDER BY es.rank
            """,
            conn, params=(run_id,),
        )

        meta_row = conn.execute(
            """
            SELECT mr.model_id, mr.model_name, mr.model_family, mr.version,
                   mr.training_rows,
                   sr.run_id, sr.run_timestamp, sr.model_type,
                   sr.derby_override_active,
                   COALESCE(sr.quality_tier, 'seed_only') AS quality_tier
            FROM score_runs sr
            JOIN model_registry mr ON sr.model_id = mr.model_id
            WHERE sr.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        conn.close()
        meta = dict(meta_row) if meta_row else {}
        return (df if not df.empty else None), meta
    except Exception as exc:
        st.exception(exc)
        return None, None


@st.cache_data(ttl=30)
def load_features(card_id: int) -> pd.DataFrame:
    try:
        conn = get_connection()
        df = pd.read_sql(
            "SELECT * FROM feature_store WHERE card_id=? ORDER BY post_position",
            conn, params=(card_id,),
        )
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_race_index(include_hidden: bool = False) -> list[dict]:
    """Race cards for the Active Race selector.

    include_hidden=True returns all races (visible + soft-deleted).
    Always includes an is_hidden key in each dict (0 when the column is absent).
    """
    _cols = (
        "rc.card_id, rc.card_date, rc.race_number, rc.stakes_name, rc.race_class, "
        "rc.distance_furlongs, rc.surface, rc.field_size, "
        "t.abbrev AS track_abbrev, t.name AS track_name, t.city, t.state"
    )
    _order = "ORDER BY rc.card_date DESC, rc.race_number ASC"
    _join  = "FROM race_cards rc JOIN tracks t ON rc.track_id = t.track_id"
    try:
        conn = get_connection()
        try:
            _where = "" if include_hidden else "WHERE rc.is_hidden = 0"
            rows = conn.execute(
                f"SELECT {_cols}, rc.is_hidden {_join} {_where} {_order}"
            ).fetchall()
        except Exception:
            # is_hidden column not yet migrated — return all races without it
            rows = conn.execute(
                f"SELECT {_cols} {_join} {_order}"
            ).fetchall()
        conn.close()
        result = [dict(r) for r in rows]
        for row in result:
            row.setdefault("is_hidden", 0)
        return result
    except Exception:
        return []


@st.cache_data(ttl=30)
def load_race_info(card_id: int) -> dict:
    try:
        conn = get_connection()
        row = conn.execute(
            """SELECT rc.card_id, rc.card_date, rc.race_number,
                      rc.stakes_name, rc.purse, rc.distance_furlongs,
                      rc.surface, rc.race_class, rc.age_restriction,
                      rc.field_size,
                      t.name AS track_name, t.abbrev AS track_abbrev,
                      t.city, t.state
               FROM race_cards rc
               JOIN tracks t ON rc.track_id = t.track_id
               WHERE rc.card_id = ?""",
            (card_id,),
        ).fetchone()
        result = dict(row) if row else {}
        # Derive field_size from active entries when race_cards.field_size is NULL
        if result and not result.get("field_size"):
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE card_id=? AND scratch_flag=0",
                    (card_id,),
                ).fetchone()[0]
                if n:
                    result["field_size"] = n
            except Exception:
                pass
        conn.close()
        return result
    except Exception:
        return {}


@st.cache_data(ttl=30)
def load_db_stats() -> dict:
    try:
        conn = get_connection()
        stats = {}
        for tbl in ("horse_starts", "workouts", "track_bias", "trip_flags",
                    "feature_store", "entry_scores"):
            try:
                stats[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except Exception:
                stats[tbl] = -1
        conn.close()
        return stats
    except Exception:
        return {}


@st.cache_data(ttl=300)
def load_catalog() -> pd.DataFrame:
    path = ROOT / "output" / "feature_catalog.csv"
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


@st.cache_data(ttl=30)
def load_run_index(card_id: int) -> list[dict]:
    """Score runs for a specific race card, newest-first."""
    try:
        conn = get_connection()
        rows = conn.execute(
            """SELECT sr.run_id, sr.run_timestamp, sr.model_type,
                      mr.model_name, mr.version
               FROM score_runs sr
               JOIN model_registry mr ON sr.model_id = mr.model_id
               WHERE sr.card_id = ?
               ORDER BY sr.run_timestamp DESC""",
            (card_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=30)
def load_live_odds_cached(card_id: int) -> tuple[dict, str]:
    """Load live odds; returns (odds_dict, error_str). Never calls st.warning directly
    (anti-pattern inside @st.cache_data — side-effects only fire on cache miss)."""
    try:
        conn = get_connection()
        result = load_live_odds_by_pp(conn, card_id)
        conn.close()
        return result, ""
    except Exception as exc:
        return {}, str(exc)


def load_race_readiness(card_id: int) -> dict:
    """Live readiness signals for a race card. Not cached — always fresh."""
    try:
        conn = get_connection()
        n_entries = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE card_id=? AND scratch_flag=0",
            (card_id,),
        ).fetchone()[0]
        n_runs = conn.execute(
            "SELECT COUNT(*) FROM score_runs WHERE card_id=?",
            (card_id,),
        ).fetchone()[0]
        n_pp = conn.execute(
            """SELECT COUNT(*) FROM horse_starts hs
               JOIN entries e ON hs.entry_id = e.entry_id
               WHERE e.card_id=?""",
            (card_id,),
        ).fetchone()[0]
        if n_pp == 0:
            try:
                n_pp = conn.execute(
                    "SELECT COUNT(*) FROM firstbet_pp_starts WHERE card_id=?",
                    (card_id,),
                ).fetchone()[0]
            except Exception:
                pass
        # Live (post-time) odds — live_odds rows with is_morning_line=0
        n_live = 0
        for _q in [
            "SELECT COUNT(DISTINCT post_position) FROM live_odds WHERE card_id=? AND is_morning_line=0",
            "SELECT COUNT(DISTINCT post_position) FROM live_odds WHERE card_id=?",
        ]:
            try:
                n_live = conn.execute(_q, (card_id,)).fetchone()[0]
                break
            except Exception:
                continue
        # Morning-line odds — entries.morning_line_odds (from race-card import)
        # or official_odds_decimal from ingested results
        n_ml = 0
        try:
            n_ml = conn.execute(
                "SELECT COUNT(*) FROM entries"
                " WHERE card_id=? AND morning_line_odds IS NOT NULL AND scratch_flag=0",
                (card_id,),
            ).fetchone()[0]
        except Exception:
            pass
        if not n_ml:
            try:
                n_ml = conn.execute(
                    "SELECT COUNT(*) FROM race_results"
                    " WHERE card_id=? AND official_odds_decimal IS NOT NULL",
                    (card_id,),
                ).fetchone()[0]
            except Exception:
                pass
        conn.close()
        return {
            "runners_loaded":   n_entries,
            "live_odds_loaded": n_live > 0,
            "ml_odds_loaded":   n_ml > 0,
            "odds_loaded":      n_live > 0 or n_ml > 0,
            "pp_loaded":        n_pp > 0,
            "model_run":        n_runs > 0,
            "n_pp_rows":        n_pp,
            "n_odds_posts":     n_live,
        }
    except Exception:
        return {
            "runners_loaded": 0, "odds_loaded": False,
            "live_odds_loaded": False, "ml_odds_loaded": False,
            "pp_loaded": False, "model_run": False,
            "n_pp_rows": 0, "n_odds_posts": 0,
        }


@st.cache_resource
def load_artifact():
    path = ROOT / "saved_models" / "dirt_route_v1.pkl"
    if not path.exists():
        return None
    with open(path, "rb") as fh:
        return pickle.load(fh)


@st.cache_data(ttl=30)
def load_entries(card_id: int) -> pd.DataFrame:
    try:
        conn = get_connection()
        df = pd.read_sql(
            """SELECT e.post_position AS "Post", h.name AS "Horse",
                      e.morning_line_odds AS "ML Odds"
               FROM entries e
               JOIN horses h ON e.horse_id = h.horse_id
               WHERE e.card_id = ? AND e.scratch_flag = 0
               ORDER BY e.post_position""",
            conn, params=(card_id,),
        )
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_firstbet_pp_starts(card_id: int) -> pd.DataFrame:
    """Load firstbet_pp_starts rows joined to entries for a given race card."""
    try:
        conn = get_connection()
        try:
            df = pd.read_sql(
                """SELECT fps.start_rank, fps.race_date, fps.track_code,
                          fps.distance_text, fps.surface, fps.race_class,
                          fps.finish_position, fps.field_size, fps.odds_str,
                          fps.notes, e.post_position, h.name AS horse_name
                   FROM firstbet_pp_starts fps
                   JOIN entries e ON fps.entry_id = e.entry_id
                   JOIN horses  h ON e.horse_id   = h.horse_id
                   WHERE fps.card_id = ?
                   ORDER BY e.post_position, fps.start_rank""",
                conn, params=(card_id,),
            )
        except Exception:
            df = pd.DataFrame()
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_firstbet_career_stats(card_id: int) -> pd.DataFrame:
    """Load firstbet_career_stats rows for a given race card."""
    try:
        conn = get_connection()
        try:
            df = pd.read_sql(
                """SELECT fcs.entry_id, fcs.career_win_pct, fcs.career_place_pct,
                          fcs.career_itm_pct, fcs.recent_5_itm, fcs.recent_5_wins,
                          e.post_position, h.name AS horse_name
                   FROM firstbet_career_stats fcs
                   JOIN entries e ON fcs.entry_id = e.entry_id
                   JOIN horses  h ON e.horse_id   = h.horse_id
                   WHERE fcs.card_id = ?""",
                conn, params=(card_id,),
            )
        except Exception:
            df = pd.DataFrame()
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


def _run_pipeline_step(script_name: str, card_id: int) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / script_name),
         '--card-id', str(card_id)],
        capture_output=True, text=True, cwd=str(ROOT),
        timeout=180,
    )
    out = result.stdout
    if result.stderr.strip():
        out += "\n" + result.stderr
    return result.returncode == 0, out



# ── Helpers ───────────────────────────────────────────────────────────────────
def _no_data(msg: str = "No score data found.") -> None:
    st.markdown(
        f'<div class="warn-banner">⚠ {msg}</div>', unsafe_allow_html=True
    )
    st.code(
        "python scripts/init_db.py\n"
        "python scripts/ingest.py\n"
        "python scripts/build_features.py\n"
        "python scripts/score.py",
        language="bash",
    )


def _edge_str(v: float) -> str:
    return f"+{v:.3f}" if v > 0 else f"{v:.3f}"


def _conf_label(flag: int) -> str:
    return "medium" if flag == 1 else "low"


def _safe_num(val, ndigits: int = 4):
    """Round numeric values; return non-numeric values (strings, etc.) unchanged."""
    if val is None:
        return None
    if isinstance(val, float) and np.isnan(val):
        return None
    if isinstance(val, (int, float)):
        return round(float(val), ndigits)
    return val


def _plotly_dark() -> dict:
    return dict(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font_color="#e6edf3",
        margin=dict(l=10, r=40, t=20, b=10),
    )


def _style_board(df: pd.DataFrame) -> "pd.Styler":
    """Row background tint based on bet tag and confidence."""
    def _row(row):
        tag = row.get("bet_tag", "neutral")
        conf = row.get("confidence_flag", 1)
        if tag == "bet":
            bg = "background-color:rgba(46,160,67,.10)"
        elif tag == "underlay":
            bg = "background-color:rgba(248,81,73,.10)"
        elif conf == 0:
            bg = "background-color:rgba(210,153,34,.06)"
        else:
            bg = ""
        return [bg] * len(row)

    return df.style.apply(_row, axis=1)


def _apply_chaos(df: pd.DataFrame, chaos_index: float) -> pd.DataFrame:
    """Map board_df columns to chaos patch schema and apply. Returns df with added columns."""
    ch = df.copy()
    n = max(len(ch), 1)

    ch["WinProb_base"]       = ch["win_probability"]
    ch["PaceFit_score"]      = ch.get("pace_fit_score",    pd.Series(0.5, index=ch.index)).fillna(0.5) * 10
    ch["DevCurve_score"]     = ch.get("form_score",        pd.Series(0.5, index=ch.index)).fillna(0.5) * 10
    ch["FinishEnergy_score"] = ch.get("form_score",        pd.Series(0.5, index=ch.index)).fillna(0.5) * 10
    ch["DistanceProj_score"] = ch.get("surface_dist_fit",  pd.Series(0.5, index=ch.index)).fillna(0.5) * 10

    mkt = ch.get("market_implied_prob", pd.Series(1.0 / n, index=ch.index)).fillna(1.0 / n)
    eq  = 1.0 / n
    ch["Publicness_score"] = mkt.apply(
        lambda p: round(max(0.0, min(10.0, 5.0 + 2.5 * math.log2(max(p, 1e-6) / eq))), 2)
    )

    last_spd = ch.get("last_speed_fig", pd.Series(0, index=ch.index)).fillna(0).astype(float)
    avg_spd  = ch.get("avg_speed_fig",  pd.Series(0, index=ch.index)).fillna(0).astype(float)
    std_spd  = float(last_spd.std()) if last_spd.std() > 0 else 1.0
    ch["late_fig_z"] = ((last_spd - avg_spd) / std_spd).fillna(0.0)

    ps = ch.get("pace_style", pd.Series("stalker", index=ch.index)).fillna("stalker")
    med_pp = float(ch.get("post_position", pd.Series(10, index=ch.index)).median() or 10)
    pp_col = ch.get("post_position", pd.Series(10, index=ch.index))
    ch["FavRailCloserFlag"]    = (ps == "closer").astype(int)
    ch["FavTacticalInnerFlag"] = ((ps == "presser") & (pp_col <= med_pp)).astype(int)
    ch["FavTacticalOuterFlag"] = ((ps == "front")   | ((ps == "presser") & (pp_col > med_pp))).astype(int)

    try:
        patched = apply_derby_chaos_patch(ch, chaos_index=chaos_index)
        out = df.copy()
        out["chaos_win_prob"]  = patched["WinProb_final"].values
        out["dark_horse_flag"] = patched["DarkHorseFlag"].values
        out["dark_horse_tier"] = patched["DarkHorseTier"].values
    except Exception:
        out = df.copy()
        out["chaos_win_prob"]  = df["win_probability"]
        out["dark_horse_flag"] = False
        out["dark_horse_tier"] = "none"
    return out


_KELLY_SLIDER_MAX = 25.0   # slider value that equals full Kelly (multiplier 1.0)
_KELLY_SAFETY_CAP = 0.25   # hard ceiling: never bet > 25% of bankroll on one bet
_CHAOS_MIN_FIELD  = 10     # chaos patch designed for large fields; disable below this


def _add_kelly(
    df: pd.DataFrame,
    bankroll: float,
    kelly_pct: float,           # slider value 1-25 where 25 = full Kelly
    live_odds_by_pp: dict,
) -> pd.DataFrame:
    """Add kelly_frac / stake_dollar and debug columns to df.

    kelly_pct is a fractional-Kelly multiplier: kelly_pct/25 of the full
    Kelly fraction is used.  Every horse's stake scales proportionally —
    halving kelly_pct halves all stakes.  A hard safety cap of 25% of
    bankroll applies regardless of kelly_pct.

    Debug columns added: full_kelly_frac, dec_odds_used, raw_stake.
    """
    multiplier   = kelly_pct / _KELLY_SLIDER_MAX   # e.g. 10/25 = 0.40
    full_fracs, kelly_fracs, dec_odds_col, raw_stakes, stakes = [], [], [], [], []

    for _, row in df.iterrows():
        model_p = float(row.get("win_probability") or 0)
        pp      = row.get("post_position")
        lo      = live_odds_by_pp.get(pp) if pp else None

        if model_p <= 0:
            full_fracs.append(0.0); kelly_fracs.append(0.0)
            dec_odds_col.append(None); raw_stakes.append(0.0); stakes.append(0.0)
            continue

        if lo and lo.get("decimal_odds"):
            dec = float(lo["decimal_odds"])
        else:
            mkt = row.get("market_implied_prob")
            dec = (1.0 / mkt) if mkt and mkt > 0 else None

        if dec is None:
            full_fracs.append(0.0); kelly_fracs.append(0.0)
            dec_odds_col.append(None); raw_stakes.append(0.0); stakes.append(0.0)
            continue

        full_kf = kelly_fraction_full(model_p, dec)           # uncapped
        raw_kf  = full_kf * multiplier                        # scaled, pre-cap
        kf      = round(min(raw_kf, _KELLY_SAFETY_CAP), 4)   # safety cap

        full_fracs.append(full_kf)
        kelly_fracs.append(kf)
        dec_odds_col.append(round(dec, 3))
        raw_stakes.append(round(raw_kf * bankroll, 4))
        stakes.append(round(kf * bankroll, 2))

    out = df.copy()
    out["full_kelly_frac"] = full_fracs
    out["kelly_frac"]      = kelly_fracs
    out["dec_odds_used"]   = dec_odds_col
    out["raw_stake"]       = raw_stakes
    out["stake_dollar"]    = stakes
    return out


def _overlay_live_odds(df: pd.DataFrame, live_by_pp: dict) -> pd.DataFrame:
    """Attach live_decimal_odds and live_market_prob columns from the live_odds dict."""
    out = df.copy()
    dec_list, prob_list = [], []
    for _, row in out.iterrows():
        pp = row.get("post_position")
        lo = live_by_pp.get(int(pp)) if pp is not None else None
        if lo and lo.get("decimal_odds") and float(lo["decimal_odds"]) > 1.0:
            dec = float(lo["decimal_odds"])
            dec_list.append(dec)
            prob_list.append(round(1.0 / dec, 6))
        else:
            dec_list.append(None)
            prob_list.append(None)
    out["live_decimal_odds"] = dec_list
    out["live_market_prob"]  = prob_list
    return out


# ── One-time startup DDL ──────────────────────────────────────────────────────
if "startup_ddl_done" not in st.session_state:
    try:
        _startup_conn = get_connection()
        ensure_is_hidden_column(_startup_conn)
        ensure_firstbet_pp_table(_startup_conn)
        _startup_conn.close()
    except Exception:
        pass
    st.session_state["startup_ddl_done"] = True

# ── Persistent session state ──────────────────────────────────────────────────
# active_card_id survives tab switches, button clicks, and file uploads.
# It is set by: (a) sidebar selectbox, (b) "Set as Active Race" button in Tab 5.
if "active_card_id" not in st.session_state:
    st.session_state["active_card_id"] = None

# Local aliases populated by the sidebar block below.
race_info:       dict       = {}
active_card_id:  int | None = None
selected_run_id: str | None = None
_rdns: dict = {
    "runners_loaded": 0, "odds_loaded": False,
    "pp_loaded": False, "model_run": False,
    "n_pp_rows": 0, "n_odds_posts": 0,
}

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown('<p class="console-title">⚙ DerbyEdge</p>', unsafe_allow_html=True)
    st.markdown('<p class="console-sub">Operator Console</p>', unsafe_allow_html=True)
    st.divider()

    # ── Active Race selector ───────────────────────────────────────────────
    if "show_hidden_races" not in st.session_state:
        st.session_state["show_hidden_races"] = False

    # Load all races; filter in Python so the toggle decision is instantaneous.
    _race_index_all = load_race_index(include_hidden=True)
    _has_hidden      = any(r.get("is_hidden") for r in _race_index_all)
    _show_hidden     = st.session_state.get("show_hidden_races", False)
    _race_index      = (
        _race_index_all if _show_hidden
        else [r for r in _race_index_all if not r.get("is_hidden")]
    )

    if not _race_index:
        st.warning("No race cards found. Run the ingest pipeline first.")
    else:
        def _rlabel(r: dict) -> str:
            name  = r.get("stakes_name") or f"Race {r.get('race_number', 1)}"
            label = f"{r['track_abbrev']} · {r['card_date']} · {name}"
            if r.get("is_hidden"):
                label = f"[HIDDEN] {label}"
            return label

        _cids = [r["card_id"] for r in _race_index]
        # Default selectbox to whatever session state says; fall back to first race.
        _ss_cid = st.session_state["active_card_id"]
        _default_ri = _cids.index(_ss_cid) if _ss_cid in _cids else 0

        if len(_race_index) > 1:
            st.markdown("**Active Race**")
            _rl = [_rlabel(r) for r in _race_index]
            _ri = st.selectbox(
                "Active race", range(len(_race_index)),
                index=_default_ri,
                format_func=lambda i: _rl[i],
                label_visibility="collapsed",
            )
            st.session_state["active_card_id"] = _race_index[_ri]["card_id"]
        else:
            st.session_state["active_card_id"] = _race_index[0]["card_id"]

        # Admin toggle — only visible when at least one race is soft-deleted.
        if _has_hidden:
            _new_show = st.checkbox(
                "Show hidden races",
                value=_show_hidden,
                key="show_hidden_checkbox",
                help="Admin only — reveals soft-deleted races in the selector above.",
            )
            if _new_show != _show_hidden:
                st.session_state["show_hidden_races"] = _new_show
                st.rerun()

        st.divider()

        active_card_id = st.session_state["active_card_id"]
        race_info = load_race_info(active_card_id)

        # Race info display
        if race_info:
            st.markdown("**Race**")
            _race_name = race_info.get("stakes_name") or f"Race {race_info.get('race_number', 1)}"
            _race_cls  = race_info.get("race_class")
            _cls_sfx   = f" ({_race_cls})" if _race_cls else ""
            st.markdown(f"**{_race_name}**{_cls_sfx}")
            _tname = race_info.get("track_name") or race_info.get("track_abbrev") or "Unknown"
            _city, _state = race_info.get("city"), race_info.get("state")
            if _city and _state:
                _loc = f"{_tname} · {_city}, {_state}"
            elif _city or _state:
                _loc = f"{_tname} · {_city or _state}"
            else:
                _loc = _tname
            st.caption(
                f"{_loc}  \n"
                f"{race_info.get('card_date') or 'Unknown'} · "
                f"{race_info.get('distance_furlongs') or '?'}f "
                f"{race_info.get('surface') or 'Unknown'} · "
                f"Field: {race_info.get('field_size') or '?'}"
            )

        # ── Race readiness badges ──────────────────────────────────────────
        _rdns = load_race_readiness(active_card_id)
        def _rbadge(ok: bool, label: str) -> str:
            cls = "badge-impl" if ok else "badge-phld"
            sym = "✓" if ok else "✗"
            return f'<span class="status-badge {cls}">{sym} {label}</span>'
        _odds_label = (
            "Odds"    if _rdns["live_odds_loaded"] else
            "ML Odds" if _rdns["ml_odds_loaded"]   else
            "Odds"
        )
        st.markdown(
            " ".join([
                _rbadge(_rdns["runners_loaded"] > 0, "Runners"),
                _rbadge(_rdns["odds_loaded"],        _odds_label),
                _rbadge(_rdns["pp_loaded"],          "PPs"),
                _rbadge(_rdns["model_run"],          "Scored"),
            ]),
            unsafe_allow_html=True,
        )
        if not _rdns["pp_loaded"]:
            st.caption("⚠ No PP history — model uses base-rate priors")
        st.divider()

        # ── Active Run selector ────────────────────────────────────────────
        _runs = load_run_index(active_card_id)
        if len(_runs) > 1:
            st.markdown("**Active Run**")
            _run_labels = [
                f"{r['run_timestamp'][:19]} · {r['run_id'][:8]}" for r in _runs
            ]
            _sel_idx = st.selectbox(
                "Score run", range(len(_runs)),
                format_func=lambda i: _run_labels[i],
                label_visibility="collapsed",
            )
            selected_run_id = _runs[_sel_idx]["run_id"]
            st.divider()
        elif _runs:
            selected_run_id = _runs[0]["run_id"]

        # ── Model run badge ────────────────────────────────────────────────
        _, _meta_sb = load_board(selected_run_id)
        if _meta_sb:
            st.markdown("**Model Run**")
            _mt  = _meta_sb.get("model_type", "N/A")
            _qt  = str(_meta_sb.get("quality_tier") or "seed_only")
            _derby = bool(_meta_sb.get("derby_override_active", 0))
            if _qt == "enriched_proxy":
                _bcls, _blbl = "badge-proxy", "ENRICHED-PROXY"
            elif "seed_only" in str(_mt):
                _bcls, _blbl = "badge-seed",  "SEED-ONLY"
            else:
                _bcls, _blbl = "badge-xgb",   "XGBOOST"
            _derbybadge = (
                ' <span class="status-badge" style="background:#1a2535;color:#c9a227;'
                'border:1px solid #c9a22788;">DERBY OVERRIDE</span>' if _derby else ""
            )
            st.markdown(
                f'<span class="status-badge {_bcls}">{_blbl}</span>{_derbybadge}',
                unsafe_allow_html=True,
            )
            st.caption(
                f"Run: `{_meta_sb.get('run_id', '')}` · "
                f"ID: {_meta_sb.get('model_id', '')}  \n"
                f"{_meta_sb.get('run_timestamp', '')[:19]}"
            )
            _sb_card = st.session_state.get("active_card_id")
            if _sb_card and st.button(
                "⚡ Rebuild features + rescore",
                use_container_width=True,
                key="rebuild_sb",
                help="Recompute features (including 1/ST BET overlay) then rescore.",
            ):
                with st.spinner(f"Rebuilding card_id={_sb_card}…"):
                    _rbok, _rbout = _run_pipeline_step("build_features.py", _sb_card)
                    if _rbok:
                        _rbok2, _rbout2 = _run_pipeline_step("score.py", _sb_card)
                        _rbout += "\n" + _rbout2
                        _rbok = _rbok2
                st.session_state["_last_pipeline_out"] = _rbout
                if _rbok:
                    st.success("Rebuild complete.")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.error("Rebuild failed — see pipeline output in Race Board tab.")
            st.divider()

    # ── Board Filters ──────────────────────────────────────────────────────
    st.markdown("**Board Filters**")
    filt_hide_ul  = st.checkbox("Hide underlays",         value=False)
    filt_conf_med = st.checkbox("Medium confidence only", value=False)
    filt_bet_only = st.checkbox("Bet candidates only",    value=False)

    st.divider()
    st.markdown("**Bankroll & Kelly**")
    bankroll = st.number_input(
        "Bankroll ($)", min_value=0, max_value=1_000_000, value=1_000, step=100,
        help="Total betting bankroll. Stake$ = bankroll × capped Kelly fraction.",
    )
    kelly_pct = st.slider(
        "Kelly fraction", 1, 25, 5, 1,
        help=(
            "Fractional Kelly multiplier (1–25 where 25 = full Kelly). "
            "25 ≈ full Kelly · 12 ≈ half Kelly · 6 ≈ quarter Kelly · 5 ≈ fifth Kelly. "
            "All stakes scale proportionally — halving this halves every stake. "
            "Hard cap: 25% of bankroll per bet regardless."
        ),
    )

    st.divider()
    st.markdown("**Derby Chaos Overlay**")
    _n_runners = _rdns.get("runners_loaded", 0)
    _chaos_race_ok = _n_runners >= _CHAOS_MIN_FIELD
    chaos_on = st.toggle(
        "Apply chaos patch", value=False,
        disabled=not _chaos_race_ok,
        help=(
            "Reallocate win mass toward dark-horse beneficiaries. "
            f"Requires ≥{_CHAOS_MIN_FIELD} starters (3yo G1 dirt-route scope)."
        ),
    )
    if not _chaos_race_ok:
        chaos_on = False  # override: widget may hold stale True from a prior race
        _msg = (
            f"Disabled — {_n_runners} starter(s). "
            f"Chaos patch is designed for large-field stakes races (≥{_CHAOS_MIN_FIELD})."
        ) if _n_runners > 0 else f"Disabled — no runners loaded (≥{_CHAOS_MIN_FIELD} required)."
        st.caption(_msg)
    chaos_idx = st.slider(
        "Chaos index", 0.0, 1.0, 0.85, 0.05,
        disabled=not chaos_on,
        help="0 = deterministic chalk · 1 = maximum chaos. Activates at ≥ 0.70.",
    )

    st.divider()
    if st.button("↺ Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# Resolve local alias from session state (handles the no-races-found case too).
active_card_id = st.session_state["active_card_id"]

# ── Load all data ──────────────────────────────────────────────────────────────
board_df, meta = load_board(selected_run_id) if selected_run_id else (None, None)
feat_df  = load_features(active_card_id) if active_card_id else pd.DataFrame()
catalog  = load_catalog()
artifact = load_artifact()
db_stats = load_db_stats()

# Live odds — returns (dict, error_str); st.warning called here, not inside cached fn
_live_odds_by_pp, _live_odds_err = (
    load_live_odds_cached(int(active_card_id)) if active_card_id else ({}, "")
)
if _live_odds_err:
    st.warning(f"Live odds error: {_live_odds_err}")

# Overlay live decimal odds + implied prob onto board_df so all tabs can use them
if board_df is not None and _live_odds_by_pp:
    board_df = _overlay_live_odds(board_df, _live_odds_by_pp)

# ── Header ─────────────────────────────────────────────────────────────────────
if race_info:
    _race_name_hdr = (
        race_info.get("stakes_name") or f"Race {race_info.get('race_number', 1)}"
    )
    _hdr_label = (
        f"{race_info.get('track_abbrev') or 'Unknown'} · "
        f"{race_info.get('card_date') or 'Unknown'} · "
        f"{_race_name_hdr}"
    )
else:
    _hdr_label = "DerbyEdge Operator Console"
st.markdown(f'<p class="console-title">⚙ {_hdr_label}</p>', unsafe_allow_html=True)
if meta:
    st.caption(
        f"Model `{meta.get('model_name', '')}` v{meta.get('version', '')} · "
        f"Run `{meta.get('run_id', '')}` · "
        f"{meta.get('run_timestamp', '')[:19]} UTC"
    )

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9 = st.tabs([
    "📋 Race Board",
    "🔍 Entry Details",
    "🧪 Model Diagnostics",
    "📖 Methodology",
    "📥 Market Intake",
    "📊 PP Import",
    "🏁 Results Import",
    "⚙ Admin",
    "📈 Calibration",
])

# ── TAB 1: Race Board ──────────────────────────────────────────────────────────
with tab1:
    if board_df is None:
        # ── Unscored race: readiness + build/score actions ────────────────────
        _ru1, _ru2, _ru3, _ru4 = st.columns(4)
        _ru1.metric("Runners", _rdns["runners_loaded"] or "—",
                    delta="Loaded" if _rdns["runners_loaded"] else "Missing")
        _ru2.metric(
            "Live Odds" if _rdns["live_odds_loaded"] else "Odds",
            "✓" if _rdns["live_odds_loaded"] else ("ML ✓" if _rdns["ml_odds_loaded"] else "✗"),
        )
        _ru3.metric("PP History",
                    f"{_rdns['n_pp_rows']} rows" if _rdns["pp_loaded"] else "—")
        _ru4.metric("Score Run", "✓" if _rdns["model_run"] else "✗ Not yet")

        st.divider()

        if not active_card_id:
            st.warning("No active race. Select or create one in the sidebar.")
        elif not _rdns["runners_loaded"]:
            st.markdown(
                '<div class="warn-banner">⚠ No runners loaded. '
                'Go to <strong>Market Intake</strong> to upload an odds CSV '
                'or ingest a sportsbook screenshot.</div>',
                unsafe_allow_html=True,
            )
        else:
            st.subheader("Build & Score Actions")
            _ba1, _ba2, _ba3 = st.columns(3)

            with _ba1:
                if st.button("⚙ Build features", use_container_width=True,
                             key="bld_tab1"):
                    with st.spinner(
                        f"Building features for card_id={active_card_id}…"
                    ):
                        _ok1, _out1 = _run_pipeline_step(
                            "build_features.py", active_card_id
                        )
                    st.session_state["_last_pipeline_out"] = _out1
                    if _ok1:
                        st.success("Features built.")
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error("Build failed — see pipeline output below.")

            with _ba2:
                if st.button("🏁 Score this race", use_container_width=True,
                             key="scr_tab1"):
                    with st.spinner(f"Scoring card_id={active_card_id}…"):
                        _ok2, _out2 = _run_pipeline_step(
                            "score.py", active_card_id
                        )
                    st.session_state["_last_pipeline_out"] = _out2
                    if _ok2:
                        st.success("Race scored.")
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error("Score failed — run Build features first.")

            with _ba3:
                if st.button("⚡ Build + Score now", use_container_width=True,
                             type="primary", key="bns_tab1"):
                    with st.spinner(
                        f"Build + Score for card_id={active_card_id}…"
                    ):
                        _ok3a, _out3a = _run_pipeline_step(
                            "build_features.py", active_card_id
                        )
                        if _ok3a:
                            _ok3b, _out3b = _run_pipeline_step(
                                "score.py", active_card_id
                            )
                            _ok3, _out3 = _ok3b, _out3a + "\n" + _out3b
                        else:
                            _ok3, _out3 = False, _out3a
                    st.session_state["_last_pipeline_out"] = _out3
                    if _ok3:
                        st.success("Build + Score completed.")
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error("Pipeline failed — see output below.")

            if st.session_state.get("_last_pipeline_out"):
                with st.expander("Pipeline output"):
                    st.code(
                        st.session_state["_last_pipeline_out"], language="bash"
                    )

        if active_card_id and _rdns["runners_loaded"]:
            st.divider()
            st.subheader("Terminal commands")
            st.code(
                f"python scripts/build_features.py --card-id {active_card_id}\n"
                f"python scripts/score.py --card-id {active_card_id}",
                language="bash",
            )
    else:
        # Apply sidebar filters
        disp = board_df.copy()
        if filt_hide_ul:
            disp = disp[disp["bet_tag"] != "underlay"]
        if filt_conf_med:
            disp = disp[disp["confidence_flag"] == 1]
        if filt_bet_only:
            disp = disp[disp["bet_tag"] == "bet"]

        # ── Board summary stats ────────────────────────────────────────────────
        sum_wp    = board_df["win_probability"].sum()
        n_bets    = (board_df["bet_tag"] == "bet").sum()
        n_ul      = (board_df["bet_tag"] == "underlay").sum()
        n_low     = (board_df["confidence_flag"] == 0).sum()
        top_horse = board_df.iloc[0]

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Horses",        len(board_df))
        c2.metric("Bet-tagged",    int(n_bets),  delta=None)
        c3.metric("Underlays",     int(n_ul),    delta=None)
        c4.metric("Low confidence",int(n_low))
        c5.metric("Sum win prob",  f"{sum_wp:.4f}")

        st.caption(
            f"Top: **{top_horse['horse_name']}** "
            f"{top_horse['win_probability']*100:.1f}% · "
            f"fair {top_horse['fair_odds']:.1f}-1 · "
            f"edge {_edge_str(top_horse['value_score'])} · "
            f"{TAG_ICON.get(top_horse['bet_tag'], top_horse['bet_tag'])}"
        )
        st.divider()

        # ── Derby override banner ──────────────────────────────────────────────
        if meta and meta.get("derby_override_active"):
            st.markdown(
                '<div style="background:#1a2535;border-left:3px solid #c9a227;'
                'padding:8px 12px;border-radius:0 6px 6px 0;font-size:.85rem;'
                'color:#c9a227;margin-bottom:8px;">'
                '🏇 <strong>Derby Override Active</strong> — weights shifted: '
                'distance/surface +5pp · race shape +3pp · market prior −3pp · '
                'confidence tightened (route starts ≥ 3, or ≥ 2 + pedigree ≥ 0.75)'
                '</div>',
                unsafe_allow_html=True,
            )

        # ── Board thresholds reminder ──────────────────────────────────────────
        st.markdown(
            '<div class="info-banner">Bet thresholds: '
            '<strong>BET</strong> = edge ≥ +0.025 · '
            '<strong>UL</strong> = edge &lt; −0.015 · '
            '<strong>NEUTRAL</strong> otherwise</div>',
            unsafe_allow_html=True,
        )

        # ── Chaos overlay + Kelly ─────────────────────────────────────────────
        if chaos_on:
            disp = _apply_chaos(disp, chaos_idx)
            _chaos_delta = (disp["chaos_win_prob"] - disp["win_probability"]).abs().sum()
            _did_reallocate = _chaos_delta > 1e-4
            if _did_reallocate:
                st.markdown(
                    '<div style="background:#1a2a1a;border-left:3px solid #4caf50;'
                    'padding:8px 12px;border-radius:0 6px 6px 0;font-size:.85rem;'
                    'color:#81c784;margin-bottom:8px;">'
                    f'🌀 <strong>Chaos overlay active</strong> — index {chaos_idx:.2f} · '
                    'win mass reallocated toward dark-horse beneficiaries'
                    '</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div style="background:#1a1a1a;border-left:3px solid #888;'
                    'padding:8px 12px;border-radius:0 6px 6px 0;font-size:.85rem;'
                    'color:#aaa;margin-bottom:8px;">'
                    f'🌀 <strong>Chaos overlay — no effect</strong> (index {chaos_idx:.2f}) · '
                    'No dark-horse beneficiaries qualified. '
                    'Requirements: win prob 1.5–12%, form ≥ 0.70, pace/dist fit ≥ 0.60. '
                    'Win probabilities are unchanged.'
                    '</div>',
                    unsafe_allow_html=True,
                )

        disp = _add_kelly(disp, float(bankroll), float(kelly_pct), _live_odds_by_pp)
        has_live_odds = bool(_live_odds_by_pp)
        has_kelly = bankroll > 0

        if has_live_odds:
            _snap_ts_board = max(lo["captured_at"] for lo in _live_odds_by_pp.values())
            st.markdown(
                f'<div class="info-banner">📊 Live odds snapshot active — '
                f'<strong>{_snap_ts_board[:19]}</strong> UTC · '
                f'{len(_live_odds_by_pp)} entries · board, Kelly &amp; market chart use this snapshot only</div>',
                unsafe_allow_html=True,
            )

        # ── Build display frame ────────────────────────────────────────────────
        base_cols = [
            "rank", "horse_name", "post_position",
            "morning_line_odds", "win_probability",
            "fair_odds", "value_score", "bet_tag",
            "pace_fit_score", "form_score", "surface_dist_fit",
            "confidence_flag", "trainer", "jockey",
        ]
        optional_cols = []
        if chaos_on:
            optional_cols += ["chaos_win_prob", "dark_horse_flag", "dark_horse_tier"]
        if has_kelly:
            optional_cols += ["kelly_frac", "stake_dollar"]

        tbl = disp[base_cols + [c for c in optional_cols if c in disp.columns]].copy()

        tbl["Tag"]  = tbl["bet_tag"].map(TAG_ICON)
        tbl["Conf"] = tbl["confidence_flag"].map(CONF_ICON)
        tbl["Win%"] = (tbl["win_probability"] * 100).round(2)
        tbl["ML"]   = tbl["morning_line_odds"].apply(lambda x: f"{x:.0f}-1")
        tbl["Edge"] = tbl["value_score"].apply(_edge_str)

        display_cols: dict = {
            "rank":            "Rank",
            "horse_name":      "Horse",
            "post_position":   "Post",
            "trainer":         "Trainer",
            "jockey":          "Jockey",
            "ML":              "ML",
            "Win%":            "Win %",
            "fair_odds":       "Fair Odds",
            "Edge":            "Edge",
            "Tag":             "Tag",
            "Conf":            "Conf",
            "pace_fit_score":  "Pace Fit",
            "form_score":      "Form",
            "surface_dist_fit":"SuDist",
        }
        if chaos_on and "chaos_win_prob" in tbl.columns:
            tbl["Chaos%"] = (tbl["chaos_win_prob"] * 100).round(2)
            display_cols["Chaos%"] = "Chaos%"
        if chaos_on and "dark_horse_tier" in tbl.columns:
            display_cols["dark_horse_tier"] = "DH Tier"
        if has_kelly and "stake_dollar" in tbl.columns:
            tbl["Stake$"] = tbl["stake_dollar"].apply(
                lambda x: f"${x:,.2f}" if x > 0 else "—"
            )
            display_cols["Stake$"] = "Stake$"
            odds_src = "live odds" if has_live_odds else "ML proxy"
            st.markdown(
                f'<div class="info-banner">💰 Kelly stakes — bankroll ${bankroll:,} · '
                f'fraction {kelly_pct}/25 ({kelly_pct/25*100:.0f}% of full Kelly) · '
                f'odds source: <strong>{odds_src}</strong></div>',
                unsafe_allow_html=True,
            )

        col_order = [c for c in display_cols if c in tbl.columns]
        show = tbl[col_order].rename(columns=display_cols)

        st.dataframe(
            show,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Win %":     st.column_config.NumberColumn("Win %",     format="%.2f"),
                "Chaos%":    st.column_config.NumberColumn("Chaos%",    format="%.2f"),
                "Fair Odds": st.column_config.NumberColumn("Fair Odds", format="%.1f"),
                "Pace Fit":  st.column_config.NumberColumn("Pace Fit",  format="%.3f"),
                "Form":      st.column_config.NumberColumn("Form",      format="%.3f"),
                "SuDist":    st.column_config.NumberColumn("SuDist",    format="%.3f"),
            },
        )

        # ── Kelly debug view ──────────────────────────────────────────────────
        if has_kelly and st.checkbox("Show Kelly debug", key="kelly_debug", value=False):
            _mult = kelly_pct / _KELLY_SLIDER_MAX
            st.caption(
                f"bankroll=${bankroll:,} · Kelly {kelly_pct}/25 · "
                f"multiplier={_mult:.4f} · safety cap={_KELLY_SAFETY_CAP*100:.0f}%"
            )
            _dbg_cols = [
                "horse_name", "win_probability", "market_implied_prob",
                "dec_odds_used", "full_kelly_frac", "kelly_frac",
                "raw_stake", "stake_dollar",
            ]
            _dbg = disp[[c for c in _dbg_cols if c in disp.columns]].copy()
            _dbg.rename(columns={
                "horse_name":        "Horse",
                "win_probability":   "Model p",
                "market_implied_prob": "Market p",
                "dec_odds_used":     "Dec Odds",
                "full_kelly_frac":   "Full Kelly f*",
                "kelly_frac":        "Final Kelly f",
                "raw_stake":         "Raw Stake ($)",
                "stake_dollar":      "Stake$ (final)",
            }, inplace=True)
            st.dataframe(
                _dbg,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Model p":       st.column_config.NumberColumn(format="%.4f"),
                    "Market p":      st.column_config.NumberColumn(format="%.4f"),
                    "Dec Odds":      st.column_config.NumberColumn(format="%.3f"),
                    "Full Kelly f*": st.column_config.NumberColumn(format="%.6f"),
                    "Final Kelly f": st.column_config.NumberColumn(format="%.4f"),
                    "Raw Stake ($)": st.column_config.NumberColumn(format="%.4f"),
                    "Stake$ (final)":st.column_config.NumberColumn(format="%.2f"),
                },
            )

        # ── Win probability bar chart ──────────────────────────────────────────
        st.subheader("Win Probability vs Market")
        chart_df = disp.sort_values("win_probability", ascending=True).copy()
        chart_df["win_pct"] = chart_df["win_probability"] * 100

        if "live_market_prob" in chart_df.columns and chart_df["live_market_prob"].notna().any():
            chart_df["market_pct"] = chart_df["live_market_prob"].fillna(
                chart_df["market_implied_prob"]
            ) * 100
            mkt_series_label = "Live Market %"
        else:
            chart_df["market_pct"] = chart_df["market_implied_prob"] * 100
            mkt_series_label = "Market % (ML)"

        fig = go.Figure()
        bar_colors = [
            "#3fb950" if t == "bet" else "#f85149" if t == "underlay" else "#4facfe"
            for t in chart_df["bet_tag"]
        ]
        fig.add_trace(go.Bar(
            y=chart_df["horse_name"], x=chart_df["win_pct"],
            name="Model Win%", orientation="h",
            marker_color=bar_colors,
            text=chart_df["win_pct"].apply(lambda x: f"{x:.1f}%"),
            textposition="outside",
        ))
        if chaos_on and "chaos_win_prob" in chart_df.columns:
            fig.add_trace(go.Scatter(
                y=chart_df["horse_name"],
                x=(chart_df["chaos_win_prob"] * 100),
                name="Chaos%", mode="markers",
                marker=dict(symbol="star", size=10, color="#81c784"),
            ))
        fig.add_trace(go.Scatter(
            y=chart_df["horse_name"], x=chart_df["market_pct"],
            name=mkt_series_label, mode="markers",
            marker=dict(symbol="diamond", size=8, color="#f0883e"),
        ))
        fig.update_layout(
            height=540, barmode="overlay",
            legend=dict(orientation="h", y=1.02),
            **_plotly_dark(),
        )
        st.plotly_chart(fig, use_container_width=True)

        # ── Odds drift sparkline (if live odds uploaded) ───────────────────────
        if has_live_odds:
            st.subheader("Live Odds vs Morning Line")
            try:
                _conn_drift = get_connection()
                if active_card_id:
                    drift_rows = _conn_drift.execute(
                        """SELECT lo.post_position, h.name AS horse_name,
                                  lo.decimal_odds, lo.book_id, lo.captured_at
                           FROM live_odds lo
                           JOIN entries e ON lo.entry_id = e.entry_id
                           JOIN horses h ON e.horse_id = h.horse_id
                           WHERE lo.card_id = ?
                           ORDER BY lo.captured_at""",
                        (int(active_card_id),),
                    ).fetchall()
                    _conn_drift.close()
                    if drift_rows:
                        drift_df = pd.DataFrame(drift_rows, columns=[
                            "post_position", "horse_name", "decimal_odds", "book_id", "captured_at"
                        ])
                        drift_df["captured_at"] = pd.to_datetime(drift_df["captured_at"])
                        fig_drift = go.Figure()
                        for horse, grp in drift_df.groupby("horse_name"):
                            fig_drift.add_trace(go.Scatter(
                                x=grp["captured_at"], y=grp["decimal_odds"],
                                name=horse, mode="lines+markers",
                            ))
                        fig_drift.update_layout(
                            height=320,
                            yaxis=dict(autorange="reversed", title="Decimal odds (lower = shorter)"),
                            xaxis_title="Time",
                            legend=dict(orientation="h", y=-0.2),
                            **_plotly_dark(),
                        )
                        st.plotly_chart(fig_drift, use_container_width=True)
            except Exception:
                pass

        # ── Low-confidence notice ──────────────────────────────────────────────
        low_conf_horses = board_df[board_df["confidence_flag"] == 0]["horse_name"].tolist()
        if low_conf_horses:
            st.markdown(
                f'<div class="warn-banner">🟡 <strong>Low confidence</strong> '
                f'({len(low_conf_horses)} entries — dist_starts ≤ 1; distance_fit based on '
                f'stamina_index only): {", ".join(low_conf_horses)}</div>',
                unsafe_allow_html=True,
            )

# ── TAB 2: Entry Details ───────────────────────────────────────────────────────
with tab2:
    if board_df is None:
        st.info("No score run for this race yet.")
        if active_card_id:
            _ent_df = load_entries(int(active_card_id))
            if not _ent_df.empty:
                st.markdown("**Loaded entries** — scoring not yet run")
                st.dataframe(_ent_df, use_container_width=True, hide_index=True)
            else:
                st.warning(
                    "No entries found. Use Market Intake to add runners."
                )
        st.markdown(
            '<div class="info-banner">Use the <strong>Race Board</strong> tab '
            'to build features and score this race.</div>',
            unsafe_allow_html=True,
        )
    else:
        options = [
            f"{int(r['rank'])}. {r['horse_name']} (Post {int(r['post_position'])})"
            for _, r in board_df.iterrows()
        ]
        sel_label = st.selectbox("Select entry", options)
        sel_rank  = int(sel_label.split(".")[0])
        horse     = board_df[board_df["rank"] == sel_rank].iloc[0]

        # Load 1/ST BET enrichment data for this card (empty frames if not available)
        _fb_pp_df    = load_firstbet_pp_starts(int(active_card_id))    if active_card_id else pd.DataFrame()
        _fb_stats_df = load_firstbet_career_stats(int(active_card_id)) if active_card_id else pd.DataFrame()
        _fb_horse_pp = (
            _fb_pp_df[_fb_pp_df["horse_name"] == horse["horse_name"]]
            if not _fb_pp_df.empty else pd.DataFrame()
        )
        _fb_horse_stats = (
            _fb_stats_df[_fb_stats_df["horse_name"] == horse["horse_name"]]
            if not _fb_stats_df.empty else pd.DataFrame()
        )
        _fb_stat_row = _fb_horse_stats.iloc[0] if not _fb_horse_stats.empty else None

        # ── Horse card ─────────────────────────────────────────────────────────
        col_tag  = TAG_BADGE.get(horse["bet_tag"], horse["bet_tag"])
        col_conf = CONF_BADGE.get(_conf_label(horse["confidence_flag"]), "")

        st.markdown(
            f"## #{int(horse['post_position'])} {horse['horse_name']}  "
            f"{col_tag} {col_conf}",
            unsafe_allow_html=True,
        )

        m1, m2, m3, m4, m5 = st.columns(5)
        _live_mkt = horse.get("live_market_prob")
        _mkt_prob = float(_live_mkt) if _live_mkt else float(horse["market_implied_prob"])
        _mkt_label = "Live Mkt %" if _live_mkt else "Market %"
        m1.metric("Win %",      f"{horse['win_probability']*100:.1f}%")
        m2.metric("Fair Odds",  f"{horse['fair_odds']:.1f}-1")
        m3.metric("Model Edge", _edge_str(horse["value_score"]))
        m4.metric("ML Odds",    f"{horse['morning_line_odds']:.0f}-1")
        m5.metric(_mkt_label,   f"{_mkt_prob*100:.1f}%")

        st.divider()
        left, right = st.columns([1, 1])

        with left:
            st.markdown("**Connections & Profile**")

            def _hf(key):
                """Horse field → float; None for any null/NaN."""
                v = horse.get(key)
                if v is None:
                    return None
                try:
                    f = float(v)
                    return None if (f != f) else f   # catch NaN
                except (TypeError, ValueError):
                    return None

            cs  = _hf("career_starts");  cw  = _hf("career_wins")
            cp  = _hf("career_places");  csh = _hf("career_shows")
            ce  = _hf("career_earnings")
            ds  = _hf("dirt_starts");    dw  = _hf("dirt_wins")
            dts = _hf("dist_starts");    dtw = _hf("dist_wins")
            lrd = _hf("last_race_days"); lrf = _hf("last_race_finish")
            si  = _hf("stamina_index")

            if cs and cs > 0 and cw is not None:
                win_pct_str = f"{cw / cs * 100:.0f}%"
                itm_pct_str = f"{(cw + (cp or 0) + (csh or 0)) / cs * 100:.0f}%"
            elif _fb_stat_row is not None:
                # Fallback: use 1/ST BET career percentages when entries.career_starts is null
                _fb_wp = _fb_stat_row.get("career_win_pct")
                _fb_sp = _fb_stat_row.get("career_itm_pct")
                win_pct_str = f"{_fb_wp*100:.0f}% *" if _fb_wp is not None else "—"
                itm_pct_str = f"{_fb_sp*100:.0f}% *" if _fb_sp is not None else "—"
            else:
                win_pct_str = itm_pct_str = "—"

            career_rec = (
                f"{int(cs)}-{int(cw)}-{int(cp)}-{int(csh)}"
                if None not in (cs, cw, cp, csh)
                else "—"
            )

            # Last race days / finish: prefer entries, fall back to firstbet_pp_starts row 1
            if lrd is None and not _fb_horse_pp.empty:
                _fb_pp1 = _fb_horse_pp[_fb_horse_pp["start_rank"] == 1]
                if not _fb_pp1.empty:
                    _fb_row1 = _fb_pp1.iloc[0]
                    lrf = _fb_row1.get("finish_position")
                    # days calculation requires race_info date
                    _fb_pp1_date = _fb_row1.get("race_date")
                    _rc_date = race_info.get("card_date") if race_info else None
                    if _fb_pp1_date and _rc_date:
                        try:
                            from datetime import date as _date
                            _diff = (_date.fromisoformat(_rc_date) - _date.fromisoformat(_fb_pp1_date)).days
                            lrd = _diff if _diff >= 0 else None
                        except (ValueError, TypeError):
                            pass

            for k, v in [
                ("Trainer",       horse.get("trainer") or "—"),
                ("Jockey",        horse.get("jockey")  or "—"),
                ("Sire / Dam",    f"{horse.get('sire') or '—'} / {horse.get('dam') or '—'}"),
                ("Owner",         horse.get("owner") or "—"),
                ("Career record", career_rec),
                ("Win% / ITM%",   f"{win_pct_str} / {itm_pct_str}"),
                ("Earnings",      f"${int(ce):,}" if ce is not None else "—"),
                ("Dirt (last 5)", f"{int(ds)}S" if ds is not None else "—"),
                ("@ Distance (last 5)", f"{int(dts)}S" if dts is not None else "—"),
                ("Last race",     f"{int(lrd)}d ago, finished {int(lrf)}"
                                  if None not in (lrd, lrf) else "—"),
                ("Pace style",    str(horse.get("pace_style") or "—").title()),
                ("Stamina index", f"{si:.2f}" if si is not None else "—"),
            ]:
                st.markdown(
                    f'<div class="kv-row"><span class="kv-key">{k}</span>'
                    f'<span class="kv-val">{v}</span></div>',
                    unsafe_allow_html=True,
                )

        with right:
            st.markdown("**Speed Figures**")
            _spd_raw = [
                horse.get("best_speed_fig"), horse.get("last_speed_fig"),
                horse.get("avg_speed_fig"),  horse.get("beyer_fig"),
            ]
            _spd_text = []
            for _sv in _spd_raw:
                try:
                    _spd_text.append(round(float(_sv), 1))
                except (TypeError, ValueError):
                    _spd_text.append("—")
            fig_spd = go.Figure(go.Bar(
                x=["Best", "Last", "Avg", "Beyer"],
                y=_spd_raw,
                marker_color=["#ffd700", "#4facfe", "#a8edea", "#f093fb"],
                text=_spd_text,
                textposition="outside",
            ))
            fig_spd.update_layout(
                yaxis_range=[70, 125],
                height=260,
                **_plotly_dark(),
            )
            st.plotly_chart(fig_spd, use_container_width=True)

            # Group score radar (from artifact)
            if artifact is not None and horse["horse_name"] in (feat_df["horse_name"].values if not feat_df.empty else []):
                gs   = artifact.group_scores
                h_idx = feat_df[feat_df["horse_name"] == horse["horse_name"]].index
                if len(h_idx) > 0:
                    i    = h_idx[0]
                    cats = list(gs.keys())
                    vals = [gs[g][i] for g in cats]
                    # close the polygon
                    fig_rad = go.Figure(go.Scatterpolar(
                        r=vals + [vals[0]],
                        theta=[g.replace("_", " ").title() for g in cats] + [cats[0].replace("_", " ").title()],
                        fill="toself",
                        fillcolor="rgba(79,172,254,.15)",
                        line=dict(color="#4facfe", width=2),
                    ))
                    fig_rad.update_layout(
                        polar=dict(
                            radialaxis=dict(visible=True, range=[0, 1]),
                            bgcolor="rgba(0,0,0,0)",
                        ),
                        showlegend=False,
                        height=280,
                        **_plotly_dark(),
                    )
                    st.plotly_chart(fig_rad, use_container_width=True)

        st.divider()

        # ── Feature breakdown table ────────────────────────────────────────────
        st.markdown("**Feature Audit**")
        st.caption(
            "IMPL = direct from seed · DEG = proxy/aggregate · PHLD = null (no historical data)"
        )

        if not feat_df.empty and not catalog.empty:
            h_feats = feat_df[feat_df["horse_name"] == horse["horse_name"]]
            if not h_feats.empty:
                hrow = h_feats.iloc[0]
                importances = artifact.feature_importances if artifact else {}

                meta_cols = {"entry_id", "horse_id", "card_id", "horse_name",
                             "post_position", "build_ts"}
                feat_rows = []
                for col in feat_df.columns:
                    if col in meta_cols:
                        continue
                    val  = hrow.get(col)
                    if isinstance(val, float) and np.isnan(val):
                        val = None
                    cat_row = catalog[catalog["feature_name"] == col]
                    tier = cat_row["tier"].iloc[0]   if not cat_row.empty else "UNKNOWN"
                    imp  = importances.get(col, 0.0)
                    feat_rows.append({
                        "feature":    col,
                        "value":      _safe_num(val),
                        "tier":       tier,
                        "in_model":   imp > 0,
                        "importance": imp,
                    })

                feat_tbl = pd.DataFrame(feat_rows)
                model_feats = feat_tbl[feat_tbl["in_model"]].sort_values(
                    "importance", ascending=False
                )
                other_feats = feat_tbl[~feat_tbl["in_model"] & (feat_tbl["tier"] != "PLACEHOLDER")]
                phld_feats  = feat_tbl[feat_tbl["tier"] == "PLACEHOLDER"]

                def _render_feat_table(df_sub: pd.DataFrame, show_imp: bool) -> None:
                    display = df_sub[["feature", "value", "tier", "importance"]].copy()
                    display.columns = ["Feature", "Value", "Tier", "Weight"]
                    if not show_imp:
                        display = display.drop(columns=["Weight"])
                    # Tier color rows
                    def _tier_style(row):
                        t = row.get("Tier", "")
                        if t == "IMPLEMENTED":
                            return ["background-color:rgba(46,160,67,.07)"] * len(row)
                        if t == "DEGRADED":
                            return ["background-color:rgba(210,153,34,.07)"] * len(row)
                        return ["color:#6e7681"] * len(row)
                    st.dataframe(
                        display.style.apply(_tier_style, axis=1),
                        use_container_width=True, hide_index=True,
                    )

                st.markdown(f"##### Model features ({len(model_feats)})")
                _render_feat_table(model_feats, show_imp=True)

                with st.expander(
                    f"Other seed features ({len(other_feats)}) — IMPL/DEG, not in model"
                ):
                    _render_feat_table(other_feats, show_imp=False)

                with st.expander(
                    f"Placeholder features ({len(phld_feats)}) — all null (no historical data)"
                ):
                    _render_feat_table(phld_feats, show_imp=False)

            else:
                st.info("Feature data not found for this entry.")
        else:
            st.info("Run build_features.py to populate the feature store.")

        # ── Derby sub-components ───────────────────────────────────────────────
        if meta and meta.get("derby_override_active") and not feat_df.empty:
            h_feats_derby = feat_df[feat_df["horse_name"] == horse["horse_name"]]
            if not h_feats_derby.empty:
                dr = h_feats_derby.iloc[0]
                derby_cols = [
                    ("classic_distance_projection", "Classic Distance Proj.",
                     "stamina_index + dist_win_pct; key Derby stamina ask"),
                    ("pedigree_route_proxy",         "Pedigree Route Aptitude",
                     "sire-line route score; 0.90=Tapit/Curlin, 0.72=default"),
                    ("traffic_resilience_proxy",     "Traffic Resilience",
                     "pace style + field-size experience; elevated weight in Derby"),
                    ("gate_reliability",             "Gate Reliability",
                     "gate_class normalized; high = clean break expected"),
                    ("derby_override_score",         "Derby Override Composite",
                     "weighted avg of the four sub-components above"),
                    ("public_underlay_penalty",      "Public Underlay Penalty",
                     "z-score of publicness; >0.5 = horse is overhyped vs. ability"),
                    ("jan_apr_improvement_curve",    "Jan–Apr Improvement Curve",
                     "PLACEHOLDER — needs sequential speed figs from horse_starts"),
                    ("churchill_readiness",          "Churchill Readiness",
                     "PLACEHOLDER — needs Churchill Downs historical form"),
                ]
                derby_rows = []
                for feat_col, label, note in derby_cols:
                    raw = dr.get(feat_col)
                    val = _safe_num(raw)
                    null_reason = "PLACEHOLDER — no historical data" if val is None else ""
                    derby_rows.append({
                        "Sub-Component": label,
                        "Value":         val,
                        "Note":          note,
                        "Status":        "❌ NULL" if val is None else "✅",
                    })
                with st.expander("🏇 Derby Override Sub-Components", expanded=True):
                    st.caption(
                        "Weight shifts vs base dirt_route: distance/surface +5pp · "
                        "race shape +3pp · derby_override +2pp · market prior −3pp"
                    )
                    derby_df = pd.DataFrame(derby_rows)
                    st.dataframe(derby_df, use_container_width=True, hide_index=True,
                                 column_config={
                                     "Value": st.column_config.NumberColumn(format="%.4f"),
                                 })

        # ── Past Performances (1/ST BET) ──────────────────────────────────────────
        if not _fb_horse_pp.empty or _fb_stat_row is not None:
            st.divider()
            st.markdown("**Past Performances (1/ST BET)**")
            if _fb_stat_row is not None:
                _stat_parts: list[str] = []
                for _lbl, _key in [("W", "career_win_pct"), ("P", "career_place_pct"), ("S", "career_itm_pct")]:
                    _v = _fb_stat_row.get(_key)
                    if _v is not None:
                        _stat_parts.append(f"{_lbl} {float(_v)*100:.0f}%")
                _r5i = _fb_stat_row.get("recent_5_itm")
                _r5w = _fb_stat_row.get("recent_5_wins")
                if _r5i is not None:
                    _stat_parts.append(f"Recent 5 ITM {int(_r5i)}/5")
                if _r5w is not None:
                    _stat_parts.append(f"Wins {int(_r5w)}")
                if _stat_parts:
                    st.caption("Career: " + " · ".join(_stat_parts)
                               + "  _(* = from 1/ST BET PDF, no race-count basis)_")
            if not _fb_horse_pp.empty:
                _pp_disp_cols = {
                    "start_rank":     "#",
                    "race_date":      "Date",
                    "track_code":     "Track",
                    "distance_text":  "Distance",
                    "surface":        "Surf",
                    "race_class":     "Class",
                    "finish_position":"Fin",
                    "field_size":     "Fld",
                    "odds_str":       "Odds",
                }
                _pp_disp = _fb_horse_pp[
                    [c for c in _pp_disp_cols if c in _fb_horse_pp.columns]
                ].rename(columns=_pp_disp_cols)
                st.dataframe(_pp_disp, use_container_width=True, hide_index=True)
            else:
                st.caption("No PP start blocks found in the 1/ST BET PDF.")

        # Missing data flags
        if horse["missing_data_flag"] == 1:
            conf_lbl = _conf_label(horse["confidence_flag"])
            is_derby_run = bool(meta.get("derby_override_active", 0)) if meta else False
            base_flags = "no_race_splits, no_workout_detail, no_connections_stats, no_track_form, no_post_bias"
            derby_flags = ", no_jan_apr_curve, no_churchill_readiness" if is_derby_run else ""
            single_start = ", dist_fit_single_start" if conf_lbl == "low" else ""
            flags_str = base_flags + derby_flags + single_start
            st.markdown(
                f'<div class="warn-banner">⚠ Missing data flags: {flags_str}</div>',
                unsafe_allow_html=True,
            )

# ── TAB 3: Model Diagnostics ───────────────────────────────────────────────────
with tab3:
    if meta is None:
        st.info(
            "No model run yet for this race. "
            "Score the race from the Race Board tab to see model diagnostics."
        )

    # ── Data source status (always shown) ──────────────────────────────────
    st.subheader("Data Source Status")
    empty_tables = [t for t, n in db_stats.items() if n == 0 and t != "feature_store"]
    if empty_tables:
        for tbl in empty_tables:
            st.markdown(
                f'<div class="warn-banner">⚠ <code>{tbl}</code> is empty — '
                f"features derived from this table are PLACEHOLDER (null).</div>",
                unsafe_allow_html=True,
            )
    st.markdown(
        '<div class="info-banner">ℹ Seed-only install: horse_starts, workouts, '
        "track_bias, and trip_flags are intentionally empty. "
        "12 of 46 features are null (PLACEHOLDER) until historical data is loaded.</div>",
        unsafe_allow_html=True,
    )

    src_data = {
        "horse_starts": (db_stats.get("horse_starts", 0), "Race history, form, speed figs"),
        "workouts":     (db_stats.get("workouts", 0),     "Bullet counts, days-since-work"),
        "track_bias":   (db_stats.get("track_bias", 0),   "Post bias, rail position"),
        "trip_flags":   (db_stats.get("trip_flags", 0),   "Trip trouble, recovery proxy"),
        "feature_store":(db_stats.get("feature_store", 0),"Computed features (46 per entry)"),
        "entry_scores": (db_stats.get("entry_scores", 0), "Model output scores"),
    }
    src_df = pd.DataFrame(
        [(t, n, desc, "✅" if n > 0 else "❌ EMPTY") for t, (n, desc) in src_data.items()],
        columns=["Table", "Rows", "Provides", "Status"],
    )
    st.dataframe(src_df, use_container_width=True, hide_index=True)

    if meta is not None:
        st.divider()

        # ── Model metadata ─────────────────────────────────────────────────────
        st.subheader("Model Metadata")
        diag_left, diag_right = st.columns(2)
        with diag_left:
            for k, v in [
                ("Model name",     meta.get("model_name")),
                ("Model ID",       meta.get("model_id")),
                ("Model family",   meta.get("model_family")),
                ("Version",        meta.get("version")),
                ("Model type",     meta.get("model_type")),
                ("Training rows",  f"{meta.get('training_rows', 0)} (need ≥50 for XGBoost)"),
                ("Run ID",         meta.get("run_id")),
                ("Scored at",      str(meta.get("run_timestamp", ""))[:19]),
            ]:
                st.markdown(
                    f'<div class="kv-row"><span class="kv-key">{k}</span>'
                    f'<span class="kv-val">{v}</span></div>',
                    unsafe_allow_html=True,
                )

        with diag_right:
            if artifact is not None:
                for k, v in [
                    ("Calibration method", "Temperature-scaled softmax"),
                    ("Temperature (T)",    artifact.temperature),
                    ("Calibration target", "Overround-adjusted morning line"),
                ]:
                    st.markdown(
                        f'<div class="kv-row"><span class="kv-key">{k}</span>'
                        f'<span class="kv-val">{v}</span></div>',
                        unsafe_allow_html=True,
                    )

            if board_df is not None:
                sum_wp  = board_df["win_probability"].sum()
                n_bets  = (board_df["bet_tag"] == "bet").sum()
                n_ul    = (board_df["bet_tag"] == "underlay").sum()
                n_low   = (board_df["confidence_flag"] == 0).sum()
                mkt_col = board_df["market_implied_prob"]
                mkt_adj = mkt_col / mkt_col.sum()
                wp      = board_df["win_probability"]
                from scipy.stats import kendalltau
                tau, _  = kendalltau(wp.values, mkt_adj.values)
                edges   = wp.values - mkt_adj.values
                kl      = float(np.sum(wp.values * np.log(
                    np.maximum(wp.values / np.maximum(mkt_adj.values, 1e-9), 1e-9)
                )))
                for k, v in [
                    ("Sum win prob",        f"{sum_wp:.6f}"),
                    ("Kendall tau vs ML",   f"{tau:.4f}"),
                    ("KL divergence",       f"{kl:.4f}"),
                    ("Mean abs edge",       f"{np.abs(edges).mean():.4f}"),
                    ("Bet candidates",      int(n_bets)),
                    ("Underlays",           int(n_ul)),
                    ("Low-confidence entries", int(n_low)),
                ]:
                    st.markdown(
                        f'<div class="kv-row"><span class="kv-key">{k}</span>'
                        f'<span class="kv-val">{v}</span></div>',
                        unsafe_allow_html=True,
                    )

        st.divider()

        # ── Derby override weight comparison ───────────────────────────────────
        if meta and meta.get("derby_override_active") and artifact is not None:
            st.subheader("🏇 Derby Override — Weight Shifts vs Base dirt_route")
            st.caption(
                "Confidence tightened: medium requires dist_starts ≥ 3, "
                "or dist_starts = 2 with pedigree_route_proxy ≥ 0.75"
            )
            from src.models.trainer import TRAIN_CONFIGS
            base_groups  = TRAIN_CONFIGS["dirt_route"]["feature_groups"]
            derby_groups = artifact.config.get("feature_groups", {})
            wt_rows = []
            for gname in base_groups:
                bw = base_groups[gname]["group_weight"]
                dw = derby_groups.get(gname, {}).get("group_weight", bw)
                delta = dw - bw
                wt_rows.append({
                    "Group":      gname,
                    "Base":       f"{bw:.0%}",
                    "Derby":      f"{dw:.0%}",
                    "Δ":          f"{delta:+.0%}" if delta != 0 else "—",
                })
            st.dataframe(
                pd.DataFrame(wt_rows), use_container_width=True, hide_index=True
            )
            st.divider()

    else:
        st.divider()
        st.info("Score this race to unlock model metadata and diagnostics.")

    # ── Feature tier summary ───────────────────────────────────────────────
    st.subheader("Feature Tier Summary")
    tier_cols = st.columns(3)
    tier_counts = catalog["tier"].value_counts().to_dict() if not catalog.empty else {}
    for col, (tier, label, color, cls) in zip(tier_cols, [
        ("IMPLEMENTED", "IMPL — Direct seed columns",  "#3fb950", "badge-impl"),
        ("DEGRADED",    "DEG  — Proxy/aggregate formula","#d29922","badge-deg"),
        ("PLACEHOLDER", "PHLD — Null (no history)",    "#6e7681", "badge-phld"),
    ]):
        with col:
            n = tier_counts.get(tier, 0)
            st.markdown(
                f'<span class="status-badge {cls}">{tier}</span>',
                unsafe_allow_html=True,
            )
            st.metric(label, n)

    st.divider()

    # ── Top 10 feature importances ─────────────────────────────────────────
    st.subheader("Top Feature Importances")
    st.caption("Effective weight = within-group weight × group weight, normalized to sum 1.0")
    if artifact is not None:
        fi = sorted(artifact.feature_importances.items(), key=lambda x: -x[1])[:10]
        fi_df = pd.DataFrame(fi, columns=["Feature", "Weight"])
        fi_df["Tier"] = fi_df["Feature"].apply(
            lambda f: catalog[catalog["feature_name"] == f]["tier"].iloc[0]
                      if not catalog.empty and len(catalog[catalog["feature_name"] == f]) > 0
                      else "DEGRADED"
        )
        bar_colors_fi = [
            "#3fb950" if t == "IMPLEMENTED" else
            "#d29922" if t == "DEGRADED"    else "#6e7681"
            for t in fi_df["Tier"]
        ]
        fig_fi = go.Figure(go.Bar(
            x=fi_df["Weight"], y=fi_df["Feature"],
            orientation="h",
            marker_color=bar_colors_fi,
            text=fi_df["Weight"].apply(lambda x: f"{x:.4f}"),
            textposition="outside",
        ))
        fig_fi.update_layout(
            height=340,
            yaxis=dict(autorange="reversed"),
            **_plotly_dark(),
        )
        st.plotly_chart(fig_fi, use_container_width=True)
        st.caption("🟢 IMPLEMENTED · 🟡 DEGRADED · ⚫ PLACEHOLDER")

# ── TAB 4: Methodology + Limitations ──────────────────────────────────────────
with tab4:
    st.subheader("How the Baseline Works")
    st.markdown("""
This model is a **seed-only weighted baseline** — a principled composite of
46 features from the feature catalog, calibrated to the morning line spread.
It is not a trained statistical model; it has no access to historical race outcomes.
    """)

    st.subheader("Feature Group Weights (dirt_route family)")
    if artifact is not None:
        groups = artifact.config.get("feature_groups", {})
        gdf = pd.DataFrame([
            {
                "Group":   g,
                "Weight":  f"{v['group_weight']:.0%}",
                "Features": ", ".join(v["features"].keys()),
            }
            for g, v in groups.items()
        ])
        st.dataframe(gdf, use_container_width=True, hide_index=True)

    st.subheader("Calibration Method")
    st.markdown("""
After computing the weighted composite score, a **temperature-scaled softmax**
is applied:

```
win_prob[i] = exp(score[i] * T) / sum(exp(score[j] * T))
```

The temperature `T` is found by minimizing mean-squared error between the
model's probability distribution and the overround-adjusted morning line —
a soft calibration that anchors probability spread to market norms without
fully collapsing to the public's opinion.

**This is not isotonic regression calibrated against actual outcomes.**
Post-race calibration will become available when race results are loaded
into `horse_starts`.
    """)

    st.subheader("Bet Tags and Edge Thresholds")
    st.markdown("""
| Tag | Condition | Meaning |
|-----|-----------|---------|
| **BET** | model_edge ≥ +0.025 | Model believes horse is materially underpriced |
| **UL** (underlay) | model_edge < −0.015 | Model believes horse is materially overpriced |
| **—** (neutral) | −0.015 ≤ edge < +0.025 | Model and market roughly agree |

`model_edge = model_win_prob − market_adjusted_prob`

`fair_odds = (1 / model_win_prob) − 1`
    """)

    st.subheader("When XGBoost Activates")
    st.markdown("""
The dispatcher in `train_or_build()` automatically switches from the weighted
baseline to an XGBoost ranker when `horse_starts` contains ≥ 50 rows for the
target `race_type_key` (e.g. `dirt_route`).

When that happens:
- Features will be computed from `horse_starts` race-by-race (replacing DEGRADED proxies)
- Validation uses **rolling time splits** (never random row splits) to prevent data leakage
- Calibration switches to **isotonic regression** on out-of-fold predictions
- Model artifacts are versioned and registered in `model_registry`
- Post-race metrics (log_loss, Brier, top-1 hit rate) become available
    """)

    st.subheader("Pipeline Commands")
    st.code(
        "python scripts/init_db.py        # 1. Create V1 schema\n"
        "python scripts/ingest.py         # 2. Load Derby seed CSV\n"
        "python scripts/build_features.py # 3. Compute 46-feature store\n"
        "python scripts/score.py          # 4. Score field + write board\n"
        "streamlit run src/app/app.py     # 5. Launch operator console",
        language="bash",
    )

    st.divider()
    st.subheader("Model Limitations")
    st.markdown(
        """
> This baseline uses seed-aggregate features and has not been validated on
> historical Derby preps. Fair odds and value scores are **directional only**.
> The following features are unavailable until real historical data is loaded:
> race-by-race speed splits, bullet workout counts, trainer/jockey conditioned
> stats, Churchill Downs track form, post-position win bias, trip trouble flags.
>
> **Do not wager without manual audit of speed figures, trip notes, and
> trainer intent.**
        """
    )
    st.caption(
        "DerbyEdge V1 · seed_only_baseline · "
        "No cloud. No APIs. No subscriptions. For operator use only."
    )

# ── TAB 5: Market Intake ───────────────────────────────────────────────────────
with tab5:
    st.markdown('<p class="console-title">Market Intake</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="console-sub">Upload live odds · create new race · ingest screenshot</p>',
        unsafe_allow_html=True,
    )

    _conn5 = get_connection()
    _card_id5 = active_card_id   # may be None if no races exist yet

    # ── Section 1: PDF Race Import (Primary) ──────────────────────────────
    st.subheader("1 · PDF Race Import")
    st.markdown(
        '<div class="info-banner">ℹ Upload a text-based PDF race page, sportsbook '
        'printout, or Equibase race card. Scanned / image-only PDFs require the '
        'Screenshot Ingest tool in the advanced section below '
        '(requires <code>ANTHROPIC_API_KEY</code>). '
        'Requires: <code>pip install pdfplumber</code>.</div>',
        unsafe_allow_html=True,
    )

    _pdf5_file = st.file_uploader(
        "Upload race PDF", type=["pdf"],
        key="pdf_race_uploader", label_visibility="collapsed",
    )

    if _pdf5_file is not None:
        with st.spinner("Extracting race data from PDF…"):
            _pr5 = parse_race_pdf(_pdf5_file.getvalue())

        if not _pr5["ok"]:
            st.markdown(
                f'<div class="warn-banner">⚠ PDF parse failed: {_pr5["error"]}</div>',
                unsafe_allow_html=True,
            )
        else:
            if _pr5["warnings"]:
                with st.expander(
                    f"⚠ Parse warnings ({len(_pr5['warnings'])})", expanded=True
                ):
                    for _pw5 in _pr5["warnings"]:
                        st.caption(f"• {_pw5}")

            # Race preview
            st.markdown("**Race Preview**")
            _p51, _p52, _p53, _p54, _p55 = st.columns(5)
            _p51.metric("Track",    _pr5.get("track_code") or _pr5.get("track_name") or "?")
            _p52.metric("Date",     _pr5.get("race_date") or "?")
            _p53.metric("Race",     f"R{_pr5['race_number']}" if _pr5.get("race_number") else "?")
            _p54.metric("Runners",  _pr5.get("field_size", len(_pr5.get("runners", []))))
            _p55.metric("Distance", _pr5.get("distance_text") or "?")
            _p5_detail = [p for p in [
                _pr5.get("surface"), _pr5.get("race_type"),
                f"${_pr5['purse_usd']:,}" if _pr5.get("purse_usd") else None,
            ] if p]
            if _p5_detail:
                st.caption(" · ".join(_p5_detail))

            # Runners preview
            if _pr5.get("runners"):
                st.markdown("**Runners**")
                _p5_rows = [{
                    "#":       r.get("program_number") or "?",
                    "Horse":   r.get("horse_name") or "?",
                    "Jockey":  r.get("jockey") or "—",
                    "Trainer": r.get("trainer") or "—",
                    "ML":      r.get("morning_line") or "—",
                    "SCR":     "✓" if r.get("is_scratched") else "",
                } for r in _pr5["runners"]]
                st.dataframe(
                    pd.DataFrame(_p5_rows),
                    use_container_width=True, hide_index=True,
                )

            # Race DB check
            _p5_exist_cid = None
            if _pr5.get("track_code") and _pr5.get("race_date") and _pr5.get("race_number"):
                _p5_exist_cid = find_race_card(
                    _conn5, _pr5["track_code"], _pr5["race_date"], int(_pr5["race_number"])
                )
            if _p5_exist_cid:
                st.markdown(
                    f'<div class="info-banner">✓ Race already in DB — '
                    f'card_id=<strong>{_p5_exist_cid}</strong>.</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div class="info-banner">ℹ Race not found in DB — '
                    '"Create race card" will add it.</div>',
                    unsafe_allow_html=True,
                )

            st.divider()

            # ── Create-button guardrail ────────────────────────────────────
            _p5_missing: list[str] = []
            if not _pr5.get("track_code"):
                _p5_missing.append("track code")
            if not _pr5.get("race_date"):
                _p5_missing.append("race date")
            if not _pr5.get("race_number"):
                _p5_missing.append("race number")
            if not _pr5.get("runners"):
                _p5_missing.append("runners (0 found)")
            _p5_can_create = len(_p5_missing) == 0

            if _p5_missing:
                st.markdown(
                    '<div class="warn-banner">⚠ Cannot create race card — '
                    "missing: <strong>" + ", ".join(_p5_missing) + "</strong>. "
                    "Check the warnings above. If the date or runners are missing, "
                    "the PDF may be unsupported — use the Screenshot Ingest tool below "
                    "or fix the PDF export format.</div>",
                    unsafe_allow_html=True,
                )

            _p5a1, _p5a2, _p5a3 = st.columns(3)

            with _p5a1:
                if st.button(
                    "✓ Re-sync entries" if _p5_exist_cid else "Create race card",
                    disabled=not _p5_can_create,
                    use_container_width=True, key="pdf5_create_btn",
                ):
                    _p5_dist_yd = _rcb_parse_distance(_pr5.get("distance_text"))
                    _p5_cid, _, _p5_n, _p5_warns = find_or_create_race(
                        _conn5,
                        _pr5["track_code"], _pr5["race_date"], int(_pr5["race_number"]),
                        _pr5.get("runners") or [],
                        distance_yards=_p5_dist_yd,
                        surface=_rcb_norm_surface(_pr5.get("surface") or ""),
                        stakes_name=_pr5.get("race_type") or None,
                        purse=_pr5.get("purse_usd") or None,
                    )
                    st.success(
                        f"{'Updated' if _p5_exist_cid else 'Created'} race card "
                        f"{_p5_cid} — {_p5_n} entries inserted."
                    )
                    for _p5w in (_p5_warns or []):
                        st.caption(_p5w)
                    # ── 1/ST BET enrichment ────────────────────────────────
                    if _pr5.get("is_1stbet") and _pr5.get("raw_text"):
                        with st.spinner("Enriching entries with PP data…"):
                            _p5_enriched = enrich_runners_1stbet(
                                _pr5["raw_text"],
                                _pr5.get("runners") or [],
                                race_date=_pr5["race_date"],
                                race_distance_yards=_p5_dist_yd or 1760,
                            )
                            _p5_er = enrich_entries_from_1stbet(
                                _conn5, _p5_cid, _p5_enriched,
                                race_date=_pr5["race_date"],
                                race_distance_yards=_p5_dist_yd or 1760,
                            )
                        if _p5_er["ok"]:
                            st.success(
                                f"1/ST BET enrichment: {_p5_er['n_enriched']} entries "
                                f"updated · {_p5_er['n_pp_rows']} PP rows · "
                                f"{_p5_er['n_stat_rows']} career-stat rows."
                            )
                        else:
                            st.warning("1/ST BET enrichment encountered errors.")
                        for _p5ew in (_p5_er.get("warnings") or []):
                            st.caption(_p5ew)
                    st.session_state["active_card_id"] = _p5_cid
                    st.cache_data.clear()
                    st.rerun()

            with _p5a2:
                _p5_odds_cid = _p5_exist_cid or st.session_state.get("active_card_id")
                if st.button(
                    "Save Morning Line Odds",
                    disabled=not _p5_odds_cid,
                    use_container_width=True, key="pdf5_odds_btn",
                ):
                    _p5_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    _n_p5_odds = 0
                    for _p5r in (_pr5.get("runners") or []):
                        if _p5r.get("is_scratched"):
                            continue
                        _p5_dec = _p5r.get("morning_line_decimal")
                        _p5_pp  = _p5r.get("post_position") or _p5r.get("program_number")
                        if not _p5_dec or not _p5_pp:
                            continue
                        _p5_erow = _conn5.execute(
                            "SELECT entry_id FROM entries WHERE card_id=? AND post_position=?",
                            (int(_p5_odds_cid), int(_p5_pp)),
                        ).fetchone()
                        if not _p5_erow:
                            continue
                        try:
                            _conn5.execute(
                                """INSERT INTO live_odds
                                   (captured_at, book_id, card_id, entry_id,
                                    post_position, decimal_odds, is_morning_line)
                                   VALUES (?, 'pdf_ml', ?, ?, ?, ?, 1)""",
                                (_p5_ts, int(_p5_odds_cid),
                                 _p5_erow[0], int(_p5_pp), float(_p5_dec)),
                            )
                            _n_p5_odds += 1
                        except Exception:
                            pass
                    if _n_p5_odds:
                        _conn5.commit()
                        st.success(f"Saved {_n_p5_odds} morning line odds rows.")
                        st.cache_data.clear()
                    else:
                        st.warning("No morning line odds found in PDF runners.")

            with _p5a3:
                _p5_set_cid = _p5_exist_cid or st.session_state.get("active_card_id")
                if st.button(
                    "Set as Active Race",
                    disabled=not _p5_set_cid,
                    use_container_width=True, key="pdf5_set_active_btn",
                ):
                    st.session_state["active_card_id"] = _p5_set_cid
                    st.cache_data.clear()
                    st.rerun()

    st.divider()
    st.markdown(
        '<div class="info-banner">📎 <strong>Advanced / Fallback</strong> — '
        'CSV templates, live odds CSV upload, and sportsbook screenshot ingest below.</div>',
        unsafe_allow_html=True,
    )
    st.divider()

    # ── Section 2: Templates ───────────────────────────────────────────────
    st.subheader("2 · CSV Templates")
    _tc1, _tc2 = st.columns(2)

    with _tc1:
        st.caption("Pre-filled template for the **active race** (update-odds mode)")
        if _card_id5:
            try:
                _tmpl_bytes = generate_template(_conn5, int(_card_id5))
                st.download_button(
                    "⬇ Download odds template CSV",
                    data=_tmpl_bytes,
                    file_name="odds_template.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
            except Exception as _te:
                st.warning(f"Template error: {_te}")
        else:
            st.info("No active race — select or create one first.")

    with _tc2:
        st.caption("Blank template for **creating a new race** (new-race mode)")
        _nr_tmpl = ROOT / "samples" / "new_race_odds_template.csv"
        if _nr_tmpl.exists():
            st.download_button(
                "⬇ Download new-race odds template",
                data=_nr_tmpl.read_bytes(),
                file_name="new_race_odds_template.csv",
                mime="text/csv",
                use_container_width=True,
            )

    st.markdown(
        '<div class="info-banner">'
        '<strong>Update-odds mode</strong> — CSV has only <code>post_position</code> + odds columns → '
        'odds are applied to the active race.<br>'
        '<strong>New-race mode</strong> — CSV also has <code>track_code</code>, '
        '<code>race_date</code>, <code>race_number</code> → race is created/found '
        'and a "Set as Active Race" prompt appears.'
        '</div>',
        unsafe_allow_html=True,
    )
    st.divider()

    # ── Section 3: Upload Live Odds CSV ───────────────────────────────────
    st.subheader("3 · Upload Live Odds CSV")

    replace_odds = st.checkbox(
        "Replace existing odds on upload (update-odds mode only)",
        value=False,
        help="New-race mode always appends. In update-odds mode this wipes existing snapshots first.",
    )

    odds_file = st.file_uploader(
        "Upload odds CSV", type=["csv"],
        key="odds_csv_uploader",
        label_visibility="collapsed",
    )

    if odds_file is not None:
        _raw5 = odds_file.getvalue()
        # Peek at header to decide mode
        _header_line = _raw5.decode("utf-8", errors="replace").split("\n")[0]
        _header_cols = [c.strip() for c in _header_line.split(",")]
        _is_new_race_csv = has_race_identity(_header_cols)

        if _is_new_race_csv:
            # ── New-race mode ──────────────────────────────────────────────
            st.markdown(
                '<div class="info-banner">📍 <strong>New-race mode detected</strong> — '
                'CSV has race identity columns (track_code · race_date · race_number). '
                'Races will be created/found automatically.</div>',
                unsafe_allow_html=True,
            )
            _nr_result = ingest_new_race_odds_csv(_raw5, _conn5)

            if _nr_result["errors"]:
                for _e in _nr_result["errors"]:
                    st.markdown(
                        f'<div class="warn-banner">⚠ {_e}</div>',
                        unsafe_allow_html=True,
                    )

            for _race_res in _nr_result["races"]:
                _rc_label = (f"{_race_res['track_code']} · "
                             f"{_race_res['race_date']} · "
                             f"R{_race_res['race_number']}")
                _created_badge = (
                    '<span class="status-badge badge-impl">NEW</span>'
                    if _race_res["created"] else
                    '<span class="status-badge badge-neutral">EXISTING</span>'
                )
                st.markdown(
                    f'<div class="info-banner">'
                    f'{_created_badge} &nbsp;<strong>{_rc_label}</strong> &nbsp;'
                    f'card_id={_race_res["card_id"]} &nbsp;·&nbsp; '
                    f'{_race_res["n_entries"]} entries &nbsp;·&nbsp; '
                    f'{_race_res["n_inserted"]} odds rows inserted'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                if _race_res["warnings"]:
                    with st.expander(f"Warnings ({len(_race_res['warnings'])})"):
                        for _w in _race_res["warnings"]:
                            st.caption(_w)
                if _race_res["skip_entry"]:
                    st.caption(
                        f"⚠ {len(_race_res['skip_entry'])} post_positions not resolved: "
                        f"{_race_res['skip_entry']}"
                    )
                # Set as Active Race button
                if st.button(
                    f"Set {_rc_label} as Active Race",
                    key=f"set_active_nr_{_race_res['card_id']}",
                    use_container_width=True,
                ):
                    st.session_state["active_card_id"] = _race_res["card_id"]
                    st.cache_data.clear()
                    st.rerun()

            if _nr_result["total_inserted"] > 0:
                st.cache_data.clear()

        else:
            # ── Update-odds mode (existing race) ──────────────────────────
            if not _card_id5:
                st.markdown(
                    '<div class="warn-banner">⚠ No active race. Upload a new-race CSV '
                    '(with track_code + race_date + race_number) or select a race in the sidebar.'
                    '</div>',
                    unsafe_allow_html=True,
                )
            else:
                try:
                    result = ingest_odds_csv(
                        _raw5, _conn5, int(_card_id5), replace=replace_odds
                    )
                    verb = "Replaced and inserted" if replace_odds else "Inserted"
                    st.success(f"{verb} {result['n_inserted']} odds rows.")
                    if result["skip_book"]:
                        st.markdown(
                            f'<div class="warn-banner">⚠ Skipped {len(result["skip_book"])} rows — '
                            f'unknown book_id(s): '
                            f'{", ".join(sorted({r["book"] for r in result["skip_book"]}))}. '
                            f'Use a valid book name.</div>',
                            unsafe_allow_html=True,
                        )
                    if result["skip_odds"]:
                        st.markdown(
                            f'<div class="warn-banner">⚠ Skipped {len(result["skip_odds"])} rows — '
                            f'no readable odds value.</div>',
                            unsafe_allow_html=True,
                        )
                    if result["skip_entry"]:
                        st.markdown(
                            f'<div class="warn-banner">⚠ Skipped {len(result["skip_entry"])} rows — '
                            f'post_position not in active race card: '
                            f'{result["skip_entry"]}.</div>',
                            unsafe_allow_html=True,
                        )
                    if result["n_inserted"] > 0:
                        st.cache_data.clear()
                        st.markdown(
                            '<div class="info-banner">ℹ Odds cache cleared — '
                            'Race Board will use the new snapshot on next render.</div>',
                            unsafe_allow_html=True,
                        )
                except ValueError as _ve:
                    st.markdown(
                        f'<div class="warn-banner">⚠ CSV format error: {_ve}</div>',
                        unsafe_allow_html=True,
                    )
                except Exception as _ue:
                    st.markdown(
                        f'<div class="warn-banner">⚠ Upload failed: {_ue}</div>',
                        unsafe_allow_html=True,
                    )

    # ── Snapshot status (active race only) ────────────────────────────────
    if _card_id5:
        _snap_meta = load_latest_snapshot_meta(_conn5, int(_card_id5))
        if _snap_meta["n_snapshots"] > 0:
            _m1, _m2, _m3 = st.columns(3)
            _m1.metric("Latest snapshot", f"{_snap_meta['latest_rows']} rows")
            _m2.metric("Historical snapshots", _snap_meta["n_snapshots"])
            _ts_display = _snap_meta["latest_ts"][:19] if _snap_meta["latest_ts"] else "—"
            _m3.metric("Last uploaded", _ts_display + " UTC")

            with st.expander(
                f"Snapshot history — {_snap_meta['total_rows']} total rows across "
                f"{_snap_meta['n_snapshots']} snapshot(s)"
            ):
                try:
                    _hist_rows = _conn5.execute(
                        """SELECT lo.captured_at, lo.book_id, lo.post_position,
                                  h.name AS horse, lo.decimal_odds, lo.american_odds
                           FROM live_odds lo
                           JOIN entries e ON lo.entry_id = e.entry_id
                           JOIN horses h ON e.horse_id = h.horse_id
                           WHERE lo.card_id = ? AND lo.is_morning_line = 0
                           ORDER BY lo.captured_at DESC, lo.post_position""",
                        (int(_card_id5),),
                    ).fetchall()
                    if _hist_rows:
                        _hist_df = pd.DataFrame(_hist_rows, columns=[
                            "Captured At", "Book", "Post", "Horse", "Dec Odds", "Am Odds"
                        ])
                        st.dataframe(_hist_df, use_container_width=True, hide_index=True)
                except Exception as _he:
                    st.warning(f"History load error: {_he}")
        else:
            st.info("No live odds uploaded yet for this race.")

        st.divider()
        with st.expander("⚠ Clear odds data (danger zone)"):
            _total_rows = _snap_meta.get("total_rows", 0) if _card_id5 else 0
            st.markdown(
                f'<div class="warn-banner">⚠ Permanently deletes ALL live_odds rows '
                f'for card_id <strong>{_card_id5}</strong> '
                f'({_total_rows} row{"s" if _total_rows != 1 else ""} total). '
                f'Drift history will be lost.</div>',
                unsafe_allow_html=True,
            )
            if "confirm_clear_odds" not in st.session_state:
                st.session_state["confirm_clear_odds"] = False

            if not st.session_state["confirm_clear_odds"]:
                if st.button("🗑 Clear all odds for active race",
                             use_container_width=True, disabled=_total_rows == 0):
                    st.session_state["confirm_clear_odds"] = True
                    st.rerun()
            else:
                st.error(f"Are you sure? This will delete {_total_rows} row(s). Cannot be undone.")
                _cc1, _cc2 = st.columns(2)
                if _cc1.button("Yes, delete all", type="primary", use_container_width=True):
                    _n_del = delete_odds_for_race(_conn5, int(_card_id5))
                    st.session_state["confirm_clear_odds"] = False
                    st.cache_data.clear()
                    st.success(f"Deleted {_n_del} row(s).")
                    st.rerun()
                if _cc2.button("Cancel", use_container_width=True):
                    st.session_state["confirm_clear_odds"] = False
                    st.rerun()

    st.divider()

    # ── Section 4: Screenshot ingest + promotion ───────────────────────────
    st.subheader("4 · Sportsbook Screenshot Ingest")
    st.markdown(
        '<div class="info-banner">ℹ Claude Vision extracts race identity and runner odds. '
        'After parse, you can create a new race card or attach to an existing one. '
        'Requires <code>ANTHROPIC_API_KEY</code>.</div>',
        unsafe_allow_html=True,
    )

    _api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        st.markdown(
            '<div class="warn-banner">⚠ ANTHROPIC_API_KEY not set — '
            'screenshot ingest disabled.</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="info-banner">ANTHROPIC_API_KEY detected.</div>',
            unsafe_allow_html=True,
        )

    screen_file = st.file_uploader(
        "Upload screenshot", type=["png", "jpg", "jpeg", "webp"],
        key="screenshot_uploader",
        label_visibility="collapsed",
        disabled=not _api_key,
    )

    if screen_file is not None and _api_key:
        with st.spinner("Parsing screenshot with Claude Vision…"):
            sr = ingest_sportsbook_screenshot(
                screen_file.getvalue(), _conn5, api_key=_api_key,
            )

        if not sr["ok"]:
            st.markdown(
                f'<div class="warn-banner">⚠ Parse failed: {sr["error"]}</div>',
                unsafe_allow_html=True,
            )
        else:
            # ── Race Preview panel ─────────────────────────────────────────
            _sr_track    = sr.get("track_id") or sr.get("track_name") or "Unknown"
            _sr_date     = sr.get("race_date") or "?"
            _sr_rnum     = sr.get("race_number")
            _sr_dist     = sr.get("distance_text") or "?"
            _sr_surf     = sr.get("surface") or "?"
            _sr_rtype    = sr.get("race_type") or ""
            _sr_post     = sr.get("post_time") or ""

            # Check if race already exists
            _sr_existing_cid: int | None = None
            if sr.get("track_id") and _sr_date and _sr_rnum:
                _sr_existing_cid = find_race_card(
                    _conn5, sr["track_id"], _sr_date, int(_sr_rnum)
                )

            st.markdown("**Race Preview**")
            _sp1, _sp2, _sp3, _sp4 = st.columns(4)
            _sp1.metric("Track", _sr_track)
            _sp2.metric("Date", _sr_date)
            _sp3.metric("Race", f"R{_sr_rnum}" if _sr_rnum else "?")
            _sp4.metric("Runners", len([r for r in sr["runners"] if not r["is_scratched"]]))

            _detail_parts = [p for p in [_sr_dist, _sr_surf, _sr_rtype, _sr_post] if p and p != "?"]
            if _detail_parts:
                st.caption(" · ".join(_detail_parts))

            # Missing-field warnings
            _missing_fields = [
                f for f, v in [
                    ("track_id", sr.get("track_id")),
                    ("race_date", _sr_date),
                    ("race_number", _sr_rnum),
                ] if not v
            ]
            if _missing_fields:
                st.markdown(
                    f'<div class="warn-banner">⚠ Vision could not extract: '
                    f'{", ".join(_missing_fields)}. '
                    f'Race card creation requires all three. '
                    f'You can still attach runners to an existing race.</div>',
                    unsafe_allow_html=True,
                )

            # Status badge
            if _sr_existing_cid:
                st.markdown(
                    f'<div class="info-banner">✓ Race already in DB — '
                    f'card_id=<strong>{_sr_existing_cid}</strong>. '
                    f'"Set as Active Race" will switch to it.</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div class="info-banner">ℹ Race not found in DB — '
                    '"Create race card" will add it.</div>',
                    unsafe_allow_html=True,
                )

            # ── Action buttons ─────────────────────────────────────────────
            _sa1, _sa2, _sa3 = st.columns(3)

            _can_create = bool(sr.get("track_id") and _sr_date and _sr_rnum)

            with _sa1:
                _create_label = (
                    "✓ Race exists — re-sync entries"
                    if _sr_existing_cid else "Create race card"
                )
                if st.button(
                    _create_label,
                    disabled=not _can_create,
                    use_container_width=True,
                    key="screenshot_create_race",
                ):
                    _build_result = create_race_from_screenshot_result(_conn5, sr)
                    if _build_result["error"]:
                        st.error(_build_result["error"])
                    else:
                        _verb = "Updated" if _sr_existing_cid else "Created"
                        st.success(
                            f"{_verb} race card {_build_result['card_id']} — "
                            f"{_build_result['n_entries']} entries inserted."
                        )
                        if _build_result["warnings"]:
                            for _bw in _build_result["warnings"]:
                                st.caption(_bw)
                        # Persist screenshot odds to live_odds so "Odds" badge lights up
                        _scr_cid = _build_result["card_id"]
                        _scr_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        _n_odds_saved = 0
                        for _scr_r in (sr.get("runners_raw") or []):
                            if _scr_r.get("is_scratched"):
                                continue
                            _scr_dec = _scr_r.get("current_odds_decimal")
                            _scr_pp = _scr_r.get("post_position") or _scr_r.get("program_number")
                            if not _scr_dec or not _scr_pp:
                                continue
                            _scr_erow = _conn5.execute(
                                "SELECT entry_id FROM entries WHERE card_id=? AND post_position=?",
                                (_scr_cid, int(_scr_pp)),
                            ).fetchone()
                            if not _scr_erow:
                                continue
                            try:
                                _conn5.execute(
                                    """INSERT INTO live_odds
                                       (captured_at, book_id, card_id, entry_id,
                                        post_position, decimal_odds, is_morning_line)
                                       VALUES (?, 'manual', ?, ?, ?, ?, 0)""",
                                    (_scr_ts, _scr_cid, _scr_erow[0],
                                     int(_scr_pp), float(_scr_dec)),
                                )
                                _n_odds_saved += 1
                            except Exception:
                                pass
                        if _n_odds_saved:
                            _conn5.commit()
                            st.caption(
                                f"Saved {_n_odds_saved} live odds rows from screenshot."
                            )
                        st.session_state["active_card_id"] = _build_result["card_id"]
                        st.cache_data.clear()
                        st.rerun()

            with _sa2:
                # Attach to existing race: selectbox of all DB races
                _all_races = load_race_index()
                if _all_races:
                    _attach_options = [_rlabel(r) for r in _all_races]
                    _attach_idx = st.selectbox(
                        "Attach to race",
                        range(len(_all_races)),
                        format_func=lambda i: _attach_options[i],
                        label_visibility="collapsed",
                        key="screenshot_attach_selectbox",
                    )
                    if st.button(
                        "Attach runners to selected race",
                        use_container_width=True,
                        key="screenshot_attach_btn",
                    ):
                        from src.services.race_card_builder import create_entries_from_runners
                        _attach_cid = _all_races[_attach_idx]["card_id"]
                        _runners_for_attach = [
                            {
                                "horse_name":        r.get("horse_name", ""),
                                "post_position":     r.get("post_position"),
                                "program_number":    r.get("program_number"),
                                "morning_line":      r.get("morning_line"),
                                "morning_line_decimal": r.get("current_odds_decimal"),
                            }
                            for r in (sr.get("runners_raw") or [])
                            if not r.get("is_scratched")
                        ]
                        _n_att, _att_warn = create_entries_from_runners(
                            _conn5, _attach_cid, _runners_for_attach
                        )
                        st.success(f"Attached {_n_att} entries to card_id={_attach_cid}.")
                        for _aw in _att_warn:
                            st.caption(_aw)
                        st.cache_data.clear()

            with _sa3:
                _set_cid = _sr_existing_cid or (
                    st.session_state.get("active_card_id")
                )
                if st.button(
                    "Set as Active Race",
                    disabled=not _set_cid,
                    use_container_width=True,
                    key="screenshot_set_active",
                ):
                    st.session_state["active_card_id"] = _set_cid
                    st.cache_data.clear()
                    st.rerun()

            # ── Runner table ───────────────────────────────────────────────
            st.divider()
            runners = sr["runners"]
            no_pp   = [r for r in runners if not r["has_pp_history"] and not r["is_scratched"]]
            with_pp = [r for r in runners if r["has_pp_history"]]

            if no_pp:
                st.markdown(
                    f'<div class="warn-banner">⚠ '
                    f'{len(no_pp)} runner{"s" if len(no_pp) != 1 else ""} without PP history: '
                    f'{", ".join(r["horse_name"] for r in no_pp)}</div>',
                    unsafe_allow_html=True,
                )

            runner_rows = []
            for r in runners:
                if r["is_scratched"]:
                    pp_badge = "SCRATCHED"
                elif r["has_pp_history"]:
                    pp_badge = f"PP OK ({len(r['last_5'])})"
                else:
                    pp_badge = "NO PP"
                runner_rows.append({
                    "#":       r["program_number"],
                    "Horse":   r["horse_name"],
                    "DB Match":r.get("matched_name") or "—",
                    "Score":   f"{r['match_score']:.2f}" if r["match_score"] else "—",
                    "ML":      r["morning_line"] or "—",
                    "Odds":    r["current_odds"] or "—",
                    "PP":      pp_badge,
                    "Warning": r["warning"] or "",
                })

            def _pp_style(row):
                pp = row.get("PP", "")
                if pp.startswith("NO PP"):
                    return ["background-color:rgba(210,153,34,.12)"] * len(row)
                if pp == "SCRATCHED":
                    return ["color:#6e7681"] * len(row)
                return [""] * len(row)

            st.dataframe(
                pd.DataFrame(runner_rows).style.apply(_pp_style, axis=1),
                use_container_width=True, hide_index=True,
            )

            if with_pp:
                with st.expander(f"PP history ({len(with_pp)} runners with data)"):
                    for r in with_pp:
                        if not r["last_5"]:
                            continue
                        st.markdown(f"**{r['horse_name']}** → matched `{r['matched_name']}`")
                        pp_df = pd.DataFrame(r["last_5"])
                        st.dataframe(pp_df, use_container_width=True, hide_index=True)

    _conn5.close()

# ── TAB 6: PP Import ───────────────────────────────────────────────────────────
with tab6:
    st.markdown('<p class="console-title">PP Import</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="console-sub">Upload past-performance CSV/TSV · preview matches · '
        'insert into horse_starts</p>',
        unsafe_allow_html=True,
    )

    if not active_card_id:
        st.markdown(
            '<div class="warn-banner">⚠ No race card loaded. '
            'Select or create a race in the sidebar.</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="info-banner">ℹ Upload a CSV or TSV with past-performance data. '
            'Required columns: <code>horse_name</code> (or <code>horse</code> / <code>name</code>) '
            'and <code>race_date</code> (or <code>date</code>). '
            'Optional: track_code, distance, surface, finish_position, jockey, '
            'speed_figure, beyer_figure, lengths_behind, earned_purse.</div>',
            unsafe_allow_html=True,
        )

        pp_file = st.file_uploader(
            "Upload PP file (CSV or TSV)",
            type=["csv", "tsv", "txt"],
            key="pp_csv_uploader",
            label_visibility="collapsed",
        )

        if pp_file is not None:
            _pp_raw = pp_file.getvalue()
            _parsed_rows, _parse_errors = parse_pp_csv(_pp_raw)

            if _parse_errors and not _parsed_rows:
                for err in _parse_errors[:5]:
                    st.markdown(
                        f'<div class="warn-banner">⚠ Parse error row {err["row"]}: '
                        f'{err["reason"]}</div>',
                        unsafe_allow_html=True,
                    )
            else:
                # Show parse summary
                _pc1, _pc2, _pc3 = st.columns(3)
                _pc1.metric("Rows parsed", len(_parsed_rows))
                _pc2.metric("Parse errors", len(_parse_errors))

                if _parse_errors:
                    with st.expander(f"Parse errors ({len(_parse_errors)})"):
                        _err_df = pd.DataFrame([
                            {"Row": e["row"], "Reason": e["reason"]}
                            for e in _parse_errors
                        ])
                        st.dataframe(_err_df, use_container_width=True, hide_index=True)

                if _parsed_rows:
                    # Preview match against active card
                    _conn6 = get_connection()
                    _preview = preview_pp_match(_conn6, _parsed_rows, active_card_id)
                    _conn6.close()

                    _pm1, _pm2, _pm3 = st.columns(3)
                    _pm1.metric("Matched", len(_preview["matched"]))
                    _pm2.metric("Unmatched", len(_preview["unmatched"]))
                    _pm3.metric("Duplicates (skip)", len(_preview["duplicates"]))

                    if _preview["matched"]:
                        st.markdown("**Matched rows (will insert)**")
                        _match_df = pd.DataFrame([{
                            "Horse (CSV)":   r["horse_name"],
                            "DB Match":      r["matched_name"],
                            "Score":         f"{r['match_score']:.3f}",
                            "In Card":       "✓" if r["in_card"] else "—",
                            "Race Date":     r["race_date"],
                            "Track":         r.get("track_code") or "—",
                            "Finish":        r.get("finish") or "—",
                            "Speed Fig":     r.get("speed_fig") or "—",
                        } for r in _preview["matched"]])

                        def _match_style(row):
                            score = float(row.get("Score", 1.0))
                            if score < 0.85:
                                return ["background-color:rgba(210,153,34,.10)"] * len(row)
                            return [""] * len(row)

                        st.dataframe(
                            _match_df.style.apply(_match_style, axis=1),
                            use_container_width=True, hide_index=True,
                        )

                    if _preview["unmatched"]:
                        with st.expander(f"Unmatched horses ({len(_preview['unmatched'])}) — will be skipped"):
                            _um_df = pd.DataFrame(_preview["unmatched"])
                            st.dataframe(_um_df, use_container_width=True, hide_index=True)

                    if _preview["duplicates"]:
                        with st.expander(f"Duplicates ({len(_preview['duplicates'])}) — already in horse_starts, will skip"):
                            _dup_df = pd.DataFrame([{
                                "Horse": r["horse_name"],
                                "Race Date": r["race_date"],
                                "Track": r.get("track_code") or "—",
                            } for r in _preview["duplicates"]])
                            st.dataframe(_dup_df, use_container_width=True, hide_index=True)

                    # Confirm insert
                    n_to_insert = len(_preview["matched"])
                    if n_to_insert > 0:
                        st.divider()
                        if st.button(
                            f"✅ Insert {n_to_insert} PP row(s) into horse_starts",
                            type="primary",
                            use_container_width=True,
                        ):
                            _conn6i = get_connection()
                            _result = ingest_pp_rows(
                                _conn6i, _parsed_rows, active_card_id
                            )
                            _conn6i.close()
                            st.success(
                                f"Inserted {_result['n_inserted']} rows · "
                                f"skipped {_result['n_skipped']} (not in card) · "
                                f"dupes {_result['n_duplicate']} · "
                                f"unmatched {_result['n_unmatched']}"
                            )
                            if _result["warnings"]:
                                with st.expander(f"Warnings ({len(_result['warnings'])})"):
                                    for w in _result["warnings"]:
                                        st.caption(w)
                            if _result["n_inserted"] > 0:
                                st.cache_data.clear()
                                st.markdown(
                                    '<div class="info-banner">ℹ Cache cleared — '
                                    'readiness badges will update on next render.</div>',
                                    unsafe_allow_html=True,
                                )
                    else:
                        st.info("No new rows to insert (all matched rows are duplicates or unmatched).")

# ── TAB 7: Results Import ──────────────────────────────────────────────────────
with tab7:
    st.markdown('<p class="console-title">Results Import</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="console-sub">Ingest official race outcomes · evaluate prior score run · '
        'produce labeled training examples</p>',
        unsafe_allow_html=True,
    )

    if not active_card_id:
        st.markdown(
            '<div class="warn-banner">⚠ No race card loaded. '
            'Select or create a race in the sidebar.</div>',
            unsafe_allow_html=True,
        )
    else:
        _conn7 = get_connection()
        _res_summary = load_results_summary(_conn7, active_card_id)

        # ── Section 1: PDF Results Import (Primary) ───────────────────────────
        st.subheader("1 · PDF Results Import")
        st.markdown(
            '<div class="info-banner">ℹ Upload an Equibase official chart PDF. '
            'Requires <code>pip install pdfplumber</code>. '
            'For scanned / image PDFs use the CSV import below.</div>',
            unsafe_allow_html=True,
        )

        _pdf7_file = st.file_uploader(
            "Upload results PDF", type=["pdf"],
            key="pdf_results_uploader", label_visibility="collapsed",
        )

        if _pdf7_file is not None:
            with st.spinner("Extracting results from PDF…"):
                _pr7 = parse_results_pdf(_pdf7_file.getvalue())

            if not _pr7["ok"]:
                st.markdown(
                    f'<div class="warn-banner">⚠ PDF parse failed: {_pr7["error"]}</div>',
                    unsafe_allow_html=True,
                )
            else:
                if _pr7["warnings"]:
                    with st.expander(
                        f"⚠ Parse warnings ({len(_pr7['warnings'])})", expanded=True
                    ):
                        for _pw7 in _pr7["warnings"]:
                            st.caption(f"• {_pw7}")

                # Race preview
                st.markdown("**Race Preview**")
                _r71, _r72, _r73, _r74 = st.columns(4)
                _r71.metric("Track",     _pr7.get("track_code") or _pr7.get("track_name") or "?")
                _r72.metric("Date",      _pr7.get("race_date") or "?")
                _r73.metric("Race",      f"R{_pr7['race_number']}" if _pr7.get("race_number") else "?")
                _r74.metric("Finishers", _pr7.get("field_size", len(_pr7.get("runners", []))))
                _r7_detail = [p for p in [
                    _pr7.get("surface"), _pr7.get("race_type"),
                    _pr7.get("track_condition"),
                    f"Final: {_pr7['final_time']}" if _pr7.get("final_time") else None,
                    f"${_pr7['purse_usd']:,}" if _pr7.get("purse_usd") else None,
                ] if p]
                if _r7_detail:
                    st.caption(" · ".join(_r7_detail))

                # Results preview
                if _pr7.get("runners"):
                    st.markdown("**Results**")
                    _r7_rows = [{
                        "#":       r.get("program_number") or "?",
                        "Horse":   r.get("horse_name") or "?",
                        "Finish":  r.get("official_finish") or "?",
                        "Odds":    r.get("official_odds") or "—",
                        "Jockey":  r.get("jockey") or "—",
                    } for r in _pr7["runners"]]
                    st.dataframe(
                        pd.DataFrame(_r7_rows),
                        use_container_width=True, hide_index=True,
                    )

                if _pr7.get("scratches"):
                    with st.expander(f"Scratches ({len(_pr7['scratches'])})"):
                        st.dataframe(
                            pd.DataFrame([{
                                "#": r.get("program_number"), "Horse": r.get("horse_name"),
                            } for r in _pr7["scratches"]]),
                            use_container_width=True, hide_index=True,
                        )

                # Match to race in DB
                _r7_exist_cid = None
                if _pr7.get("track_code") and _pr7.get("race_date") and _pr7.get("race_number"):
                    _r7_exist_cid = find_race_card(
                        _conn7, _pr7["track_code"], _pr7["race_date"], int(_pr7["race_number"])
                    )
                if _r7_exist_cid:
                    st.markdown(
                        f'<div class="info-banner">✓ Matched to card_id='
                        f'<strong>{_r7_exist_cid}</strong> in DB.</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<div class="warn-banner">⚠ Race not found in DB — '
                        'create it via Market Intake first.</div>',
                        unsafe_allow_html=True,
                    )

                st.divider()
                _r7a1, _r7a2 = st.columns(2)

                with _r7a1:
                    if st.button(
                        "Import Results from PDF",
                        disabled=not (_r7_exist_cid and _pr7.get("runners")),
                        type="primary",
                        use_container_width=True, key="pdf7_import_btn",
                    ):
                        # Build rows in the format ingest_results() expects
                        _pdf7_rows = []
                        _r7_tc  = _pr7.get("track_code") or ""
                        _r7_dt  = _pr7.get("race_date") or ""
                        _r7_rn  = int(_pr7["race_number"])
                        for _r7r in _pr7.get("runners") or []:
                            _pdf7_rows.append({
                                "horse_name":     _r7r.get("horse_name") or "",
                                "track_code":     _r7_tc,
                                "race_date":      _r7_dt,
                                "race_number":    _r7_rn,
                                "finish_position":_r7r.get("official_finish"),
                                "official_finish":_r7r.get("official_finish"),
                                "official_odds":  _r7r.get("official_odds_decimal"),
                                "post_position":  _r7r.get("post_position"),
                                "scratched":      False,
                                "disqualified":   False,
                                "beaten_lengths": None,
                                "speed_figure":   None,
                                "beyer_figure":   None,
                                "final_time":     _pr7.get("final_time"),
                                "earned_purse":   None,
                                "comment":        None,
                            })
                        for _r7s in (_pr7.get("scratches") or []):
                            _pdf7_rows.append({
                                "horse_name":     _r7s.get("horse_name") or "",
                                "track_code":     _r7_tc,
                                "race_date":      _r7_dt,
                                "race_number":    _r7_rn,
                                "finish_position":None,
                                "official_finish":None,
                                "official_odds":  None,
                                "post_position":  _r7s.get("program_number"),
                                "scratched":      True,
                                "disqualified":   False,
                                "beaten_lengths": None,
                                "speed_figure":   None,
                                "beyer_figure":   None,
                                "final_time":     None,
                                "earned_purse":   None,
                                "comment":        None,
                            })
                        _conn7i = get_connection()
                        _ing7 = ingest_results(_conn7i, _pdf7_rows)
                        _conn7i.close()
                        st.success(
                            f"Inserted {_ing7['n_inserted']} rows · "
                            f"scratches updated {_ing7['n_scratch_flag']} · "
                            f"dupes {_ing7['n_duplicate']} · "
                            f"unmatched {_ing7['n_unmatched_horse']}"
                        )
                        if _ing7["warnings"]:
                            with st.expander(f"Warnings ({len(_ing7['warnings'])})"):
                                for _iw7 in _ing7["warnings"]:
                                    st.caption(_iw7)
                        if _ing7["n_inserted"] > 0:
                            st.cache_data.clear()
                            st.rerun()

                with _r7a2:
                    if st.button(
                        "Set as Active Race",
                        disabled=not _r7_exist_cid,
                        use_container_width=True, key="pdf7_set_active_btn",
                    ):
                        st.session_state["active_card_id"] = _r7_exist_cid
                        st.cache_data.clear()
                        st.rerun()

        st.divider()
        st.markdown(
            '<div class="info-banner">📎 <strong>Advanced / Fallback</strong> — '
            'CSV template download and manual CSV upload below.</div>',
            unsafe_allow_html=True,
        )
        st.divider()

        # ── Section 2: Template download ──────────────────────────────────────
        st.subheader("2 · Download Results Template")
        _tmpl_path = ROOT / "samples" / "results_import_template.csv"
        if _tmpl_path.exists():
            st.download_button(
                label="⬇ Download results_import_template.csv",
                data=_tmpl_path.read_bytes(),
                file_name="results_import_template.csv",
                mime="text/csv",
                use_container_width=True,
            )
        st.markdown(
            '<div class="info-banner">'
            '<strong>Required columns:</strong> '
            '<code>race_date</code>, <code>track_code</code>, <code>race_number</code>, '
            '<code>horse_name</code>, <code>finish_position</code><br>'
            '<strong>Optional:</strong> '
            'official_odds, post_position, beaten_lengths, scratched, disqualified, '
            'speed_figure, beyer_figure, final_time, earned_purse, comment<br>'
            '<strong>Accepted date formats:</strong> MM/DD/YYYY · YYYY-MM-DD · DD-Mon-YYYY<br>'
            '<strong>Accepted odds formats:</strong> decimal (3.40) · fractional (9/2) · '
            'american (+340)<br>'
            '<strong>Scratched rows:</strong> set <code>scratched=1</code> and leave '
            '<code>finish_position</code> blank.'
            '</div>',
            unsafe_allow_html=True,
        )

        st.divider()

        # ── Current results status ─────────────────────────────────────────────
        if _res_summary["n_total"] > 0:
            _rs1, _rs2, _rs3 = st.columns(3)
            _rs1.metric("Results ingested", _res_summary["n_runners"])
            _rs2.metric("Total rows (incl. scratches)", _res_summary["n_total"])
            _ts7 = _res_summary["ingested_at"]
            _rs3.metric("Ingested at", (_ts7[:19] + " UTC") if _ts7 else "—")

        # ── Section 3: Upload & Preview ────────────────────────────────────────
        st.subheader("3 · Upload Results CSV")
        res_file = st.file_uploader(
            "Upload results file (CSV or TSV)",
            type=["csv", "tsv", "txt"],
            key="results_csv_uploader",
            label_visibility="collapsed",
        )

        if res_file is not None:
            _res_raw = res_file.getvalue()
            _res_parsed, _res_errors = parse_results_csv(_res_raw)

            _rp1, _rp2 = st.columns(2)
            _rp1.metric("Rows parsed", len(_res_parsed))
            _rp2.metric("Parse errors", len(_res_errors))

            if _res_errors:
                with st.expander(f"Parse errors ({len(_res_errors)})"):
                    _re_df = pd.DataFrame([
                        {"Row": e["row"], "Reason": e["reason"]}
                        for e in _res_errors
                    ])
                    st.dataframe(_re_df, use_container_width=True, hide_index=True)

            if not _res_parsed:
                st.markdown(
                    '<div class="warn-banner">⚠ No valid rows to preview. '
                    'Check parse errors above.</div>',
                    unsafe_allow_html=True,
                )
            else:
                _preview7 = preview_results_match(_conn7, _res_parsed)

                # Race resolution
                if _preview7["races_missing"]:
                    for _rm in _preview7["races_missing"]:
                        st.markdown(
                            f'<div class="warn-banner">⚠ Race not found in DB: '
                            f'<strong>{_rm["track_code"]}</strong> R{_rm["race_number"]} '
                            f'{_rm["race_date"]} — check track abbrev and date.</div>',
                            unsafe_allow_html=True,
                        )

                if _preview7["races_found"]:
                    st.markdown(
                        f'<div class="info-banner">✓ Resolved '
                        f'<strong>{len(_preview7["races_found"])}</strong> race(s) in DB: '
                        + ", ".join(
                            f'{r["track_code"]} R{r["race_number"]} {r["race_date"]}'
                            for r in _preview7["races_found"]
                        )
                        + "</div>",
                        unsafe_allow_html=True,
                    )

                # Horse match preview
                _pm1, _pm2, _pm3 = st.columns(3)
                _pm1.metric("Horses matched", len(_preview7["horses_matched"]))
                _pm2.metric("Unmatched",       len(_preview7["horses_unmatched"]))
                _pm3.metric("Duplicates (skip)",len(_preview7["horses_duplicate"]))

                if _preview7["horses_matched"]:
                    st.markdown("**Matched horses (will insert)**")

                    def _res_row_style(row):
                        score = float(row.get("Score", 1.0))
                        if row.get("Scr") == "✓":
                            return ["color:#6e7681"] * len(row)
                        if score < 0.85:
                            return ["background-color:rgba(210,153,34,.10)"] * len(row)
                        return [""] * len(row)

                    _hm_df = pd.DataFrame([{
                        "Horse (CSV)":  r["horse_name"],
                        "DB Match":     r["matched_name"],
                        "Score":        f"{r['match_score']:.3f}",
                        "Race":         f"{r['track_code']} R{r['race_number']} {r['race_date']}",
                        "Finish":       r["finish"] if r["finish"] else "—",
                        "Scr":          "✓" if r["scratched"] else "",
                    } for r in _preview7["horses_matched"]])
                    st.dataframe(
                        _hm_df.style.apply(_res_row_style, axis=1),
                        use_container_width=True, hide_index=True,
                    )

                if _preview7["horses_unmatched"]:
                    with st.expander(
                        f"Unmatched horses ({len(_preview7['horses_unmatched'])}) — will be skipped"
                    ):
                        st.dataframe(
                            pd.DataFrame(_preview7["horses_unmatched"]),
                            use_container_width=True, hide_index=True,
                        )

                if _preview7["horses_duplicate"]:
                    with st.expander(
                        f"Duplicates ({len(_preview7['horses_duplicate'])}) — already in race_results"
                    ):
                        st.dataframe(
                            pd.DataFrame([{
                                "Horse": r["horse_name"],
                                "Race":  f"{r['track_code']} R{r['race_number']} {r['race_date']}",
                                "Finish": r["finish"],
                            } for r in _preview7["horses_duplicate"]]),
                            use_container_width=True, hide_index=True,
                        )

                # Confirm insert
                _n_to_insert7 = len(_preview7["horses_matched"])
                if _n_to_insert7 > 0:
                    st.divider()
                    if st.button(
                        f"✅ Insert {_n_to_insert7} result row(s) into race_results",
                        type="primary",
                        use_container_width=True,
                        key="results_insert_btn",
                    ):
                        _conn7i = get_connection()
                        _ingest7 = ingest_results(_conn7i, _res_parsed)
                        _conn7i.close()
                        st.success(
                            f"Inserted {_ingest7['n_inserted']} rows · "
                            f"scratches updated {_ingest7['n_scratch_flag']} · "
                            f"dupes {_ingest7['n_duplicate']} · "
                            f"unmatched horses {_ingest7['n_unmatched_horse']} · "
                            f"unmatched races {_ingest7['n_unmatched_race']}"
                        )
                        if _ingest7["warnings"]:
                            with st.expander(f"Warnings ({len(_ingest7['warnings'])})"):
                                for _w7 in _ingest7["warnings"]:
                                    st.caption(_w7)
                        if _ingest7["n_inserted"] > 0:
                            st.cache_data.clear()
                            st.rerun()
                else:
                    if _preview7["races_found"]:
                        st.info("No new results to insert (all matched rows are already ingested).")

        st.divider()

        # ── Section 4: Post-Race Evaluation ───────────────────────────────────
        st.subheader("4 · Post-Race Evaluation")

        if _res_summary["n_total"] == 0:
            st.info("No race results ingested yet. Upload results above to unlock evaluation.")
        elif not selected_run_id:
            st.info("Select a score run in the sidebar to evaluate model performance.")
        else:
            _eval = evaluate_score_run(_conn7, selected_run_id, active_card_id)
            if _eval is None:
                st.info("No matching predictions found for the selected score run and race.")
            else:
                # Outcome summary
                st.markdown("**Race Outcome**")
                _ev1, _ev2, _ev3, _ev4 = st.columns(4)
                _ev1.metric("Winner", _eval["winner"])
                _top_fin = _eval["top_pick_finish"]
                _ev2.metric(
                    "Top Pick",
                    _eval["top_pick"],
                    delta="WON ✓" if _eval["top_pick_won"] else f"Finished {_top_fin}" if _top_fin else "Scratched",
                    delta_color="normal" if _eval["top_pick_won"] else "inverse",
                    help="Model's highest win-probability selection for this race",
                )
                _ptf = _eval.get("post_time_favorite_name")
                if _ptf:
                    _ev3.metric(
                        "Post-Time Favorite",
                        _ptf,
                        delta="WON ✓" if _eval["post_time_favorite_won"] else "Lost",
                        delta_color="normal" if _eval["post_time_favorite_won"] else "inverse",
                        help="Lowest official win odds at race time · source: race_results.official_odds_decimal",
                    )
                else:
                    _ev3.metric(
                        "Morning-Line Favorite",
                        _eval.get("ml_favorite_name") or "—",
                        delta="Pre-race only",
                        delta_color="off",
                        help="No official odds in results · showing pre-race morning-line favorite",
                    )
                _ev4.metric(
                    "Top-3 Hit Rate",
                    f"{_eval['top3_hit']}/3",
                    help="How many of the model's top-3 picks finished in the actual top 3",
                )

                st.divider()

                # Bet performance
                st.markdown("**BET-Tagged Performance**")
                _bv1, _bv2, _bv3, _bv4 = st.columns(4)
                _bv1.metric("BET candidates", _eval["n_bets"])
                _bv2.metric("Bets won (W)", _eval["n_bets_won"])
                _bv3.metric("Bets in-the-money (ITM)", _eval["n_bets_itm"])
                _roi = _eval["kelly_roi_pct"]
                _bv4.metric(
                    "Kelly ROI",
                    f"{_roi:+.1f}%" if _roi is not None else "N/A",
                    help=(
                        "Normalized return on $1,000 bankroll at 5% cap. "
                        "Uses live odds snapshot if available, else ML-implied odds. "
                        "N/A when no BET-tagged horse had computable odds."
                    ),
                    delta_color="normal" if (_roi or 0) >= 0 else "inverse",
                )

                if _eval["kelly_staked"] > 0:
                    _odds_src7 = "live snapshot" if _live_odds_by_pp else "ML proxy"
                    st.caption(
                        f"Kelly staked ${_eval['kelly_staked']:,.0f} "
                        f"(normalized $1k bankroll, 5% cap) · odds source: {_odds_src7}"
                    )

                st.divider()

                # Full results table
                st.markdown("**Full Results vs Predictions**")
                _fr = _eval["full_results"]
                if _fr:
                    _fr_df = pd.DataFrame([{
                        "Rank":     r["rank"],
                        "Horse":    r["horse_name"],
                        "Post":     r["post_position"],
                        "Win%":     f"{r['win_probability']*100:.1f}%" if r["win_probability"] else "—",
                        "Tag":      TAG_ICON.get(r["bet_tag"], "—"),
                        "Finish":   r["official_finish"] if r["official_finish"] else (
                                    "SCR" if r["is_scratched"] else
                                    "DQ"  if r["is_disqualified"] else
                                    r["finish_position"] if r["finish_position"] else "—"
                        ),
                        "Odds":     (f"{r['official_odds_decimal']:.2f}"
                                     if r["official_odds_decimal"] else "—"),
                        "Lengths":  (f"{r['beaten_lengths']:.2f}"
                                     if r["beaten_lengths"] is not None else "—"),
                    } for r in _fr])

                    def _result_row_style(row):
                        finish = row.get("Finish")
                        if finish == 1 or finish == "1":
                            return ["background-color:rgba(46,160,67,.12)"] * len(row)
                        if finish in ("SCR", "DQ"):
                            return ["color:#6e7681"] * len(row)
                        return [""] * len(row)

                    st.dataframe(
                        _fr_df.style.apply(_result_row_style, axis=1),
                        use_container_width=True, hide_index=True,
                    )

                    # Training label export hint
                    with st.expander("📦 Training label SQL (copy for batch retraining)"):
                        st.code(
                            f"""-- Labeled training examples for run_id = '{selected_run_id}'
    SELECT
        es.entry_id,
        es.horse_name,
        es.win_probability          AS pred_win_prob,
        es.value_score              AS pred_edge,
        es.bet_tag,
        rr.official_finish          AS actual_finish,
        CASE WHEN rr.official_finish = 1 THEN 1 ELSE 0 END AS won,
        rr.official_odds_decimal    AS actual_odds,
        rr.is_scratched,
        rr.is_disqualified
    FROM entry_scores es
    JOIN race_results rr ON es.entry_id = rr.entry_id
    WHERE es.run_id = '{selected_run_id}'
      AND rr.is_scratched = 0
      AND rr.is_disqualified = 0
    ORDER BY es.rank;""",
                            language="sql",
                        )
                        st.caption(
                            "Exclude scratched and DQ horses from calibration. "
                            "Accumulate rows across multiple finished races (different card_ids) "
                            "then retrain once horse_starts has ≥ 50 rows."
                        )

        # ── Danger zone: clear results ─────────────────────────────────────────
        if _res_summary["n_total"] > 0:
            st.divider()
            with st.expander("⚠ Clear results data (danger zone)"):
                st.markdown(
                    f'<div class="warn-banner">⚠ Permanently deletes all race_results rows '
                    f'for card_id <strong>{active_card_id}</strong> '
                    f'({_res_summary["n_total"]} row(s)). This removes official labels '
                    f'and cannot be undone.</div>',
                    unsafe_allow_html=True,
                )
                if "confirm_clear_results" not in st.session_state:
                    st.session_state["confirm_clear_results"] = False

                if not st.session_state["confirm_clear_results"]:
                    if st.button(
                        "🗑 Clear all results for active race",
                        use_container_width=True,
                        key="results_clear_btn",
                    ):
                        st.session_state["confirm_clear_results"] = True
                        st.rerun()
                else:
                    st.error(
                        f"Are you sure? This will delete {_res_summary['n_total']} row(s). "
                        "This cannot be undone."
                    )
                    _rc1, _rc2 = st.columns(2)
                    if _rc1.button("Yes, delete all results", type="primary",
                                   use_container_width=True, key="results_confirm_del"):
                        _n_del7 = delete_results_for_race(_conn7, active_card_id)
                        st.session_state["confirm_clear_results"] = False
                        st.cache_data.clear()
                        st.success(f"Deleted {_n_del7} row(s).")
                        st.rerun()
                    if _rc2.button("Cancel", use_container_width=True, key="results_cancel_del"):
                        st.session_state["confirm_clear_results"] = False
                        st.rerun()

        _conn7.close()

# ── TAB 8: Admin ───────────────────────────────────────────────────────────────
with tab8:
    if not active_card_id:
        st.warning("No active race. Select one in the sidebar.")
    else:
        _conn8 = get_connection()

        # Ensure is_hidden column exists (idempotent ALTER TABLE)
        if "is_hidden_col_ok" not in st.session_state:
            ensure_is_hidden_column(_conn8)
            st.session_state["is_hidden_col_ok"] = True

        _adm = _admin_get_race_info(_conn8, active_card_id)
        if not _adm:
            st.error(f"Race card_id={active_card_id} not found.")
        else:
            _adm_label = (
                f"{_adm['track_abbrev']} · {_adm['card_date']} · "
                f"Race {_adm['race_number']}"
            )
            st.subheader(f"⚙ Race Admin — {_adm_label}")

            # ── Section 1: Edit Metadata ───────────────────────────────────
            st.markdown("### 1 · Edit Race Metadata")

            _SURFACES = ["dirt", "turf", "synthetic", "all_weather"]
            _surf_idx = _SURFACES.index(_adm.get("surface") or "dirt")
            _dist_f_cur = round(float(_adm.get("distance_yards") or 1320) / 220.0, 2)

            with st.form("race_edit_form"):
                _fe1, _fe2 = st.columns(2)
                with _fe1:
                    _ed_abbrev   = st.text_input("Track code",      value=_adm.get("track_abbrev") or "")
                    _ed_tname    = st.text_input("Track name",       value=_adm.get("track_name")  or "")
                    _ed_date     = st.text_input("Race date (YYYY-MM-DD)", value=_adm.get("card_date") or "")
                    _ed_rnum     = st.number_input("Race number",    value=int(_adm.get("race_number") or 1),
                                                   min_value=1, step=1)
                with _fe2:
                    _ed_surface  = st.selectbox("Surface", _SURFACES, index=_surf_idx)
                    _ed_dist     = st.number_input("Distance (furlongs)", value=_dist_f_cur,
                                                   min_value=2.0, max_value=20.0, step=0.5, format="%.1f")
                    _ed_class    = st.text_input("Race class",       value=_adm.get("race_class") or "")
                    _ed_stakes   = st.text_input("Stakes name",      value=_adm.get("stakes_name") or "")
                _ff1, _ff2 = st.columns(2)
                with _ff1:
                    _ed_purse    = st.number_input("Purse ($)", value=int(_adm.get("purse") or 0),
                                                   min_value=0, step=1000)
                with _ff2:
                    _ed_field    = st.number_input("Field size", value=int(_adm.get("field_size") or 0),
                                                   min_value=0, step=1)
                _ed_age      = st.text_input("Age restriction", value=_adm.get("age_restriction") or "")

                _save_clicked = st.form_submit_button(
                    "💾 Save changes", type="primary", use_container_width=True
                )

            if _save_clicked:
                _upd = _admin_update_race(
                    _conn8, active_card_id,
                    track_abbrev=_ed_abbrev.strip() or None,
                    track_name=_ed_tname.strip() or None,
                    card_date=_ed_date.strip() or None,
                    race_number=int(_ed_rnum),
                    stakes_name=_ed_stakes.strip() or None,
                    purse=int(_ed_purse) if _ed_purse else None,
                    distance_yards=int(round(_ed_dist * 220)),
                    surface=_ed_surface,
                    race_class=_ed_class.strip() or None,
                    age_restriction=_ed_age.strip() or None,
                    field_size=int(_ed_field) if _ed_field else None,
                )
                if _upd["ok"]:
                    st.success("Race metadata saved.")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.error(f"Save failed: {_upd['error']}")

            st.divider()

            # ── Section 2: Delete Race ─────────────────────────────────────
            st.markdown("### 2 · Delete Race")

            _deps = get_race_dependencies(_conn8, active_card_id)
            _dep_labels = {
                "entries":       "Entries",
                "horse_starts":  "Horse starts",
                "feature_store": "Feature rows",
                "score_runs":    "Score runs",
                "entry_scores":  "Entry scores",
                "live_odds":     "Live odds",
                "race_results":  "Race results",
                "odds_snapshots":"Odds snapshots",
                "trip_flags":    "Trip flags",
            }
            _deps_nonzero = {k: v for k, v in _deps.items() if v > 0}

            if _deps_nonzero:
                _dc = st.columns(min(len(_deps_nonzero), 4))
                for _di, (_dk, _dv) in enumerate(_deps_nonzero.items()):
                    _dc[_di % 4].metric(_dep_labels.get(_dk, _dk), _dv)
                st.caption(
                    "Hard delete will permanently remove all rows above. "
                    "Soft delete hides the race without touching dependent data."
                )
            else:
                st.info("No dependent rows — this race can be deleted cleanly.")

            st.divider()

            st.markdown(
                '<div class="warn-banner">⚠ <strong>Soft delete is recommended.</strong> '
                "Hard delete permanently removes this race and every dependent row "
                "and <strong>cannot be undone</strong>.</div>",
                unsafe_allow_html=True,
            )

            _del_c1, _del_c2 = st.columns(2)

            with _del_c1:
                st.markdown("**Soft Delete (Hide)**")
                st.caption(
                    "Race is hidden from the selector and scoring but all data is "
                    "preserved. Can be reversed via a direct DB update."
                )
                if st.button("👁 Hide this race", use_container_width=True, key="adm_soft_del"):
                    _soft = soft_delete_race(_conn8, active_card_id)
                    if _soft["ok"]:
                        st.session_state["active_card_id"] = None
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(f"Soft delete failed: {_soft['error']}")

            with _del_c2:
                st.markdown("**Hard Delete (Cascade)**")
                _confirm_str = (
                    f"{_adm['track_abbrev']} R{_adm['race_number']} {_adm['card_date']}"
                )
                st.caption(f"Type **`{_confirm_str}`** to enable permanent deletion.")
                _confirm_typed = st.text_input(
                    "Confirm", key="adm_hard_del_confirm",
                    placeholder=_confirm_str,
                    label_visibility="collapsed",
                )
                _hard_ready = _confirm_typed.strip() == _confirm_str
                if st.button(
                    "🗑 Permanently delete race",
                    type="secondary",
                    use_container_width=True,
                    key="adm_hard_del_btn",
                    disabled=not _hard_ready,
                ):
                    _hard = hard_delete_race(_conn8, active_card_id)
                    if _hard["ok"]:
                        _hdel = _hard.get("deleted") or {}
                        _hdetail = ", ".join(
                            f"{tbl}: {n}" for tbl, n in _hdel.items() if n > 0
                        )
                        st.success(
                            f"Race permanently deleted — "
                            f"{_hard.get('total', 0)} rows removed."
                            + (f" ({_hdetail})" if _hdetail else "")
                        )
                        st.session_state["active_card_id"] = None
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        _hdel_partial = _hard.get("deleted") or {}
                        _hpartial = ", ".join(
                            f"{tbl}: {n}" for tbl, n in _hdel_partial.items() if n > 0
                        )
                        st.error(
                            f"Hard delete failed and was rolled back: {_hard['error']}"
                            + (f" (partial progress before rollback: {_hpartial})" if _hpartial else "")
                        )

        _conn8.close()

# ── TAB 9: Calibration & Outcomes ─────────────────────────────────────────────
with tab9:
    import pandas as _pd

    @st.cache_data(ttl=60)
    def _load_outcomes(limit: int) -> list[dict]:
        _c9 = get_connection()
        _rows = load_outcomes_frame(_c9, limit=limit)
        _c9.close()
        return _rows

    st.subheader("📈 Calibration & Outcomes")
    st.caption(
        "One row per scored race with results ingested. "
        "Compares model top pick and market favorites against actual outcome."
    )

    # ── Filter strip ──────────────────────────────────────────────────────────
    _cf1, _cf2, _cf3, _cf4 = st.columns([2, 2, 2, 1])
    with _cf1:
        _cal_tier = st.selectbox(
            "Quality tier",
            ["All", "enriched_proxy", "seed_only"],
            key="cal_tier",
        )
    with _cf2:
        _cal_limit = st.selectbox(
            "Show last N races",
            [25, 50, 100, 250],
            index=1,
            key="cal_limit",
        )

    _outcomes_raw = _load_outcomes(_cal_limit)

    # Filter by quality tier
    if _cal_tier != "All":
        _outcomes_raw = [r for r in _outcomes_raw if r.get("quality_tier") == _cal_tier]

    with _cf3:
        _all_tracks = sorted({r["track_code"] for r in _outcomes_raw if r.get("track_code")})
        _cal_track = st.selectbox(
            "Track",
            ["All"] + _all_tracks,
            key="cal_track",
        )
    with _cf4:
        if st.button("↺ Refresh", key="cal_refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    if _cal_track != "All":
        _outcomes_raw = [r for r in _outcomes_raw if r.get("track_code") == _cal_track]

    if not _outcomes_raw:
        st.info(
            "No calibration data yet. Ingest race results for at least one scored race "
            "to see model vs market outcomes here."
        )
    else:
        # ── Summary metrics ───────────────────────────────────────────────────
        _n_races = len(_outcomes_raw)
        _tp_won_n = sum(1 for r in _outcomes_raw if r.get("top_pick_won"))
        _tp_wr = round(100.0 * _tp_won_n / _n_races, 1) if _n_races else 0.0
        _ptf_eligible = [r for r in _outcomes_raw if r.get("post_time_favorite_name")]
        _ptf_won_n = sum(1 for r in _ptf_eligible if r.get("post_time_favorite_won"))
        _ptf_wr = round(100.0 * _ptf_won_n / len(_ptf_eligible), 1) if _ptf_eligible else 0.0

        _sm1, _sm2, _sm3 = st.columns(3)
        _sm1.metric("Races", _n_races)
        _sm2.metric(
            "Top Pick Win Rate",
            f"{_tp_wr}%",
            delta=f"{_tp_won_n} of {_n_races}",
            delta_color="off",
        )
        _sm3.metric(
            "Post-Time Fav Win Rate",
            f"{_ptf_wr}%" if _ptf_eligible else "—",
            delta=f"{_ptf_won_n} of {len(_ptf_eligible)}" if _ptf_eligible else "no odds",
            delta_color="off",
        )

        st.divider()

        # ── Build display dataframe ───────────────────────────────────────────
        _display_rows = []
        for _r in _outcomes_raw:
            _tp_won_sym = "✓" if _r.get("top_pick_won") else "✗"
            _ptf_won_sym = (
                "✓" if _r.get("post_time_favorite_won")
                else ("✗" if _r.get("post_time_favorite_name") else "—")
            )
            _dist = _r.get("distance_f")
            _dist_str = f"{_dist:.1f}f" if _dist else "?"
            _tp_prob = _r.get("top_pick_win_prob")
            _tp_prob_str = f"{_tp_prob:.0%}" if _tp_prob else "—"
            _ptf_odds = _r.get("post_time_favorite_odds")
            _ptf_odds_str = f"{_ptf_odds:.2f}" if _ptf_odds else "—"
            _w_odds = _r.get("winner_official_odds")
            _w_odds_str = f"{_w_odds:.2f}" if _w_odds else "—"
            _display_rows.append({
                "Date":       _r.get("race_date") or "",
                "Track":      _r.get("track_code") or "",
                "Race":       _r.get("race_number") or "",
                "Dist":       _dist_str,
                "Srf":        _r.get("surface_code") or "?",
                "Type":       (_r.get("race_type") or "")[:14],
                "Field":      _r.get("field_size") or "",
                "Tier":       _r.get("quality_tier") or "",
                "Top Pick":   _r.get("top_pick_name") or "—",
                "TP Prob":    _tp_prob_str,
                "TP Fin":     _r.get("top_pick_finish_pos") or "?",
                "TP Won":     _tp_won_sym,
                "PTF":        _r.get("post_time_favorite_name") or "—",
                "PTF Odds":   _ptf_odds_str,
                "PTF Won":    _ptf_won_sym,
                "Winner":     _r.get("winner_name") or "—",
                "W Odds":     _w_odds_str,
            })

        _cal_df = _pd.DataFrame(_display_rows)

        # ── Pandas Styler: color TP Won and PTF Won columns ───────────────────
        def _style_outcomes(df: "_pd.DataFrame") -> "_pd.DataFrame":
            styles = _pd.DataFrame("", index=df.index, columns=df.columns)
            for _i, _row in df.iterrows():
                if _row.get("TP Won") == "✓":
                    styles.at[_i, "TP Won"] = "color: #2ecc71; font-weight: bold"
                elif _row.get("TP Won") == "✗":
                    styles.at[_i, "TP Won"] = "color: #e74c3c"
                if _row.get("PTF Won") == "✓":
                    styles.at[_i, "PTF Won"] = "color: #2ecc71; font-weight: bold"
                elif _row.get("PTF Won") == "✗":
                    styles.at[_i, "PTF Won"] = "color: #e74c3c"
            return styles

        _cal_styled = _cal_df.style.apply(_style_outcomes, axis=None)

        st.dataframe(
            _cal_styled,
            use_container_width=True,
            hide_index=True,
            height=min(40 + 35 * len(_display_rows), 600),
        )

        st.caption(
            f"Showing {len(_display_rows)} race(s) · "
            "TP = model top pick · PTF = post-time market favorite · "
            "Odds are decimal (e.g. 3.80 = 2.80/1)"
        )
