"""
DerbyEdge V1  —  Operator Console
src/app/app.py

Run: streamlit run src/app/app.py
"""

import math
import os
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.utils.db import get_connection
from src.derbyedge.odds_math import kelly_fraction
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
)
from src.services.pp_intake import (
    ingest_pp_rows,
    parse_pp_csv,
    preview_pp_match,
)
from src.services.results_intake import (
    delete_results_for_race,
    evaluate_score_run,
    ingest_results,
    load_results_summary,
    parse_results_csv,
    preview_results_match,
)
from src.services.screenshot_ingest import ingest_sportsbook_screenshot

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
                   sr.derby_override_active
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
def load_race_index() -> list[dict]:
    """All race cards newest-first for the Active Race selector."""
    try:
        conn = get_connection()
        rows = conn.execute(
            """SELECT rc.card_id, rc.card_date, rc.race_number,
                      rc.stakes_name, rc.race_class, rc.distance_furlongs,
                      rc.surface, rc.field_size,
                      t.abbrev AS track_abbrev, t.name AS track_name,
                      t.city, t.state
               FROM race_cards rc
               JOIN tracks t ON rc.track_id = t.track_id
               ORDER BY rc.card_date DESC, rc.race_number ASC"""
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
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
        conn.close()
        return dict(row) if row else {}
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
        n_lo = 0
        for _q in [
            "SELECT COUNT(DISTINCT post_position) FROM live_odds WHERE card_id=? AND is_morning_line=0",
            "SELECT COUNT(DISTINCT post_position) FROM live_odds WHERE card_id=?",
        ]:
            try:
                n_lo = conn.execute(_q, (card_id,)).fetchone()[0]
                break
            except Exception:
                continue
        conn.close()
        return {
            "runners_loaded": n_entries,
            "odds_loaded":    n_lo > 0,
            "pp_loaded":      n_pp > 0,
            "model_run":      n_runs > 0,
            "n_pp_rows":      n_pp,
            "n_odds_posts":   n_lo,
        }
    except Exception:
        return {
            "runners_loaded": 0, "odds_loaded": False,
            "pp_loaded": False,  "model_run": False,
            "n_pp_rows": 0,      "n_odds_posts": 0,
        }


@st.cache_resource
def load_artifact():
    path = ROOT / "saved_models" / "dirt_route_v1.pkl"
    if not path.exists():
        return None
    with open(path, "rb") as fh:
        return pickle.load(fh)


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


def _add_kelly(
    df: pd.DataFrame,
    bankroll: float,
    max_kelly_pct: float,
    live_odds_by_pp: dict,
) -> pd.DataFrame:
    """Add kelly_frac and stake_dollar columns. Only BET-tagged rows get a non-zero stake."""
    cap = max_kelly_pct / 100.0
    kelly_fracs, stakes = [], []

    for _, row in df.iterrows():
        model_p = float(row.get("win_probability") or 0)
        pp      = row.get("post_position")
        lo      = live_odds_by_pp.get(pp) if pp else None

        if model_p <= 0:
            kelly_fracs.append(0.0)
            stakes.append(0.0)
            continue

        if lo and lo.get("decimal_odds"):
            dec = float(lo["decimal_odds"])
        else:
            mkt = row.get("market_implied_prob")
            dec = (1.0 / mkt) if mkt and mkt > 0 else None

        if dec is None:
            kelly_fracs.append(0.0)
            stakes.append(0.0)
            continue

        kf = kelly_fraction(model_p, dec, cap=cap)
        kelly_fracs.append(kf)
        stakes.append(round(kf * bankroll, 2))

    out = df.copy()
    out["kelly_frac"]   = kelly_fracs
    out["stake_dollar"] = stakes
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


# ── Persistent session state ──────────────────────────────────────────────────
# active_card_id survives tab switches, button clicks, and file uploads.
# It is set by: (a) sidebar selectbox, (b) "Set as Active Race" button in Tab 5.
if "active_card_id" not in st.session_state:
    st.session_state["active_card_id"] = None

# Local aliases populated by the sidebar block below.
race_info:       dict       = {}
active_card_id:  int | None = None
selected_run_id: str | None = None

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown('<p class="console-title">⚙ DerbyEdge</p>', unsafe_allow_html=True)
    st.markdown('<p class="console-sub">Operator Console</p>', unsafe_allow_html=True)
    st.divider()

    # ── Active Race selector ───────────────────────────────────────────────
    _race_index = load_race_index()
    if not _race_index:
        st.warning("No race cards found. Run the ingest pipeline first.")
    else:
        def _rlabel(r: dict) -> str:
            name = r.get("stakes_name") or f"Race {r.get('race_number', 1)}"
            return f"{r['track_abbrev']} · {r['card_date']} · {name}"

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
            st.divider()
        else:
            st.session_state["active_card_id"] = _race_index[0]["card_id"]

        active_card_id = st.session_state["active_card_id"]
        race_info = load_race_info(active_card_id)

        # Race info display
        if race_info:
            st.markdown("**Race**")
            _race_name = race_info.get("stakes_name") or f"Race {race_info.get('race_number', 1)}"
            st.markdown(f"**{_race_name}** ({race_info.get('race_class', '')})")
            st.caption(
                f"{race_info.get('track_name', '')} · "
                f"{race_info.get('city', '')}, {race_info.get('state', '')}  \n"
                f"{race_info.get('card_date', '')} · "
                f"{race_info.get('distance_furlongs', '')}f "
                f"{race_info.get('surface', '')} · "
                f"Field: {race_info.get('field_size') or '?'}"
            )

        # ── Race readiness badges ──────────────────────────────────────────
        _rdns = load_race_readiness(active_card_id)
        def _rbadge(ok: bool, label: str) -> str:
            cls = "badge-impl" if ok else "badge-phld"
            sym = "✓" if ok else "✗"
            return f'<span class="status-badge {cls}">{sym} {label}</span>'
        st.markdown(
            " ".join([
                _rbadge(_rdns["runners_loaded"] > 0, "Runners"),
                _rbadge(_rdns["odds_loaded"],         "Odds"),
                _rbadge(_rdns["pp_loaded"],           "PPs"),
                _rbadge(_rdns["model_run"],           "Scored"),
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
            _mt      = _meta_sb.get("model_type", "N/A")
            _is_seed = "seed_only" in str(_mt)
            _derby   = bool(_meta_sb.get("derby_override_active", 0))
            _bcls    = "badge-seed" if _is_seed else "badge-xgb"
            _blbl    = "SEED-ONLY" if _is_seed else "XGBOOST"
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
    max_kelly_pct = st.slider(
        "Kelly cap (%)", 1, 25, 5, 1,
        help="Hard cap per bet as % of bankroll. 5% ≈ quarter-Kelly for racing.",
    )

    st.divider()
    st.markdown("**Derby Chaos Overlay**")
    chaos_on = st.toggle(
        "Apply chaos patch", value=False,
        help="Reallocate win mass toward dark-horse beneficiaries.",
    )
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
        f"{race_info.get('track_abbrev', '')} · "
        f"{race_info.get('card_date', '')} · "
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

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "📋 Race Board",
    "🔍 Entry Details",
    "🧪 Model Diagnostics",
    "📖 Methodology",
    "📥 Market Intake",
    "📊 PP Import",
    "🏁 Results Import",
])

# ── TAB 1: Race Board ──────────────────────────────────────────────────────────
with tab1:
    if board_df is None:
        _no_data("No scored entries found. Run score.py first.")
        st.stop()

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
        st.markdown(
            '<div style="background:#1a2a1a;border-left:3px solid #4caf50;'
            'padding:8px 12px;border-radius:0 6px 6px 0;font-size:.85rem;'
            'color:#81c784;margin-bottom:8px;">'
            f'🌀 <strong>Chaos overlay active</strong> — index {chaos_idx:.2f} · '
            'win mass reallocated toward dark-horse beneficiaries'
            '</div>',
            unsafe_allow_html=True,
        )

    disp = _add_kelly(disp, float(bankroll), float(max_kelly_pct), _live_odds_by_pp)
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
            f'cap {max_kelly_pct}% · odds source: <strong>{odds_src}</strong></div>',
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
        _no_data()
        st.stop()

    options = [
        f"{int(r['rank'])}. {r['horse_name']} (Post {int(r['post_position'])})"
        for _, r in board_df.iterrows()
    ]
    sel_label = st.selectbox("Select entry", options)
    sel_rank  = int(sel_label.split(".")[0])
    horse     = board_df[board_df["rank"] == sel_rank].iloc[0]

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
        win_pct = horse["career_wins"] / max(horse["career_starts"], 1) * 100
        itm_pct = (
            (horse["career_wins"] + horse["career_places"] + horse["career_shows"])
            / max(horse["career_starts"], 1) * 100
        )
        for k, v in [
            ("Trainer",       horse["trainer"]),
            ("Jockey",        horse["jockey"]),
            ("Sire / Dam",    f"{horse['sire']} / {horse['dam']}"),
            ("Owner",         horse["owner"]),
            ("Career record", f"{int(horse['career_starts'])}-{int(horse['career_wins'])}-"
                              f"{int(horse['career_places'])}-{int(horse['career_shows'])}"),
            ("Win% / ITM%",   f"{win_pct:.0f}% / {itm_pct:.0f}%"),
            ("Earnings",      f"${int(horse['career_earnings']):,}"),
            ("Dirt",          f"{int(horse['dirt_wins'])}W / {int(horse['dirt_starts'])}S"),
            ("@ Distance",    f"{int(horse['dist_wins'])}W / {int(horse['dist_starts'])}S"),
            ("Last race",     f"{int(horse['last_race_days'])}d ago, "
                              f"finished {int(horse['last_race_finish'])}"),
            ("Pace style",    str(horse["pace_style"]).title()),
            ("Stamina index", f"{horse['stamina_index']:.2f}"),
        ]:
            st.markdown(
                f'<div class="kv-row"><span class="kv-key">{k}</span>'
                f'<span class="kv-val">{v}</span></div>',
                unsafe_allow_html=True,
            )

    with right:
        st.markdown("**Speed Figures**")
        fig_spd = go.Figure(go.Bar(
            x=["Best", "Last", "Avg", "Beyer"],
            y=[horse["best_speed_fig"], horse["last_speed_fig"],
               horse["avg_speed_fig"],  horse["beyer_fig"]],
            marker_color=["#ffd700", "#4facfe", "#a8edea", "#f093fb"],
            text=[horse["best_speed_fig"], horse["last_speed_fig"],
                  round(float(horse["avg_speed_fig"]), 1), horse["beyer_fig"]],
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
        _no_data()
        st.stop()

    # ── Data source status ─────────────────────────────────────────────────
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

    # ── Section 1: Templates ───────────────────────────────────────────────
    st.subheader("1 · Templates")
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

    # ── Section 2: Upload Live Odds CSV ───────────────────────────────────
    st.subheader("2 · Upload Live Odds CSV")

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

    # ── Section 3: Screenshot ingest + promotion ───────────────────────────
    st.subheader("3 · Sportsbook Screenshot Ingest")
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
            'Run the pipeline first.</div>',
            unsafe_allow_html=True,
        )
        st.stop()

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
            'Run the pipeline first.</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    _conn7 = get_connection()
    _res_summary = load_results_summary(_conn7, active_card_id)

    # ── Section 1: Template download ──────────────────────────────────────
    st.subheader("1 · Download Results Template")
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

    # ── Section 2: Upload & Preview ────────────────────────────────────────
    st.subheader("2 · Upload Results CSV")
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

    # ── Section 3: Post-Race Evaluation ───────────────────────────────────
    st.subheader("3 · Post-Race Evaluation")

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
            )
            _ev3.metric(
                "Favorite",
                _eval["fav_name"],
                delta="WON ✓" if _eval["fav_won"] else "Lost",
                delta_color="normal" if _eval["fav_won"] else "inverse",
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
