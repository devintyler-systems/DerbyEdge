"""
DerbyEdge V1  —  Operator Console
src/app/app.py

Run: streamlit run src/app/app.py
"""

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
def load_board() -> tuple[pd.DataFrame | None, dict | None]:
    try:
        conn = get_connection()
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
def load_features() -> pd.DataFrame:
    try:
        conn = get_connection()
        df = pd.read_sql("SELECT * FROM feature_store ORDER BY post_position", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_race_info() -> dict:
    try:
        conn = get_connection()
        row = conn.execute(
            """
            SELECT rc.card_id, rc.card_date, rc.stakes_name, rc.purse,
                   rc.distance_furlongs, rc.surface, rc.race_class,
                   rc.age_restriction, rc.field_size,
                   t.name AS track_name, t.abbrev AS track_abbrev,
                   t.city, t.state
            FROM race_cards rc
            JOIN tracks t ON rc.track_id = t.track_id
            LIMIT 1
            """
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


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown('<p class="console-title">⚙ DerbyEdge</p>', unsafe_allow_html=True)
    st.markdown('<p class="console-sub">Operator Console v1</p>', unsafe_allow_html=True)
    st.divider()

    race_info = load_race_info()
    if race_info:
        st.markdown("**Race**")
        st.markdown(
            f"**{race_info.get('stakes_name', 'N/A')}** ({race_info.get('race_class', '')})"
        )
        st.caption(
            f"{race_info.get('track_name', '')} · {race_info.get('city', '')}, "
            f"{race_info.get('state', '')}  \n"
            f"{race_info.get('card_date', '')} · "
            f"{race_info.get('distance_furlongs', '')}f {race_info.get('surface', '')} · "
            f"Field: {race_info.get('field_size') or 20}"
        )
        st.divider()

    # Model run info
    _, meta = load_board()
    if meta:
        st.markdown("**Model Run**")
        model_type = meta.get("model_type", "N/A")
        is_seed    = "seed_only" in str(model_type)
        derby_on   = bool(meta.get("derby_override_active", 0))
        badge_cls  = "badge-seed" if is_seed else "badge-xgb"
        badge_lbl  = "SEED-ONLY" if is_seed else "XGBOOST"
        derby_badge = (
            ' <span class="status-badge" style="background:#1a2535;color:#c9a227;'
            'border:1px solid #c9a22788;">DERBY OVERRIDE</span>'
            if derby_on else ""
        )
        st.markdown(
            f'<span class="status-badge {badge_cls}">{badge_lbl}</span>{derby_badge}',
            unsafe_allow_html=True,
        )
        st.caption(
            f"Run: `{meta.get('run_id', '')}` · ID: {meta.get('model_id', '')}  \n"
            f"{meta.get('run_timestamp', '')[:19]}"
        )
        st.divider()

    # Filters
    st.markdown("**Board Filters**")
    filt_hide_ul  = st.checkbox("Hide underlays",          value=False)
    filt_conf_med = st.checkbox("Medium confidence only",  value=False)
    filt_bet_only = st.checkbox("Bet candidates only",     value=False)

    st.divider()
    if st.button("↺ Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── Load all data ──────────────────────────────────────────────────────────────
board_df, meta = load_board()
feat_df  = load_features()
catalog  = load_catalog()
artifact = load_artifact()
db_stats = load_db_stats()

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown('<p class="console-title">DerbyEdge — 2026 Kentucky Derby</p>',
            unsafe_allow_html=True)
if meta:
    st.caption(
        f"Model `{meta.get('model_name', '')}` v{meta.get('version', '')} · "
        f"Run `{meta.get('run_id', '')}` · "
        f"{meta.get('run_timestamp', '')[:19]} UTC"
    )

tab1, tab2, tab3, tab4 = st.tabs(
    ["📋 Race Board", "🔍 Entry Details", "🧪 Model Diagnostics", "📖 Methodology"]
)

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

    # ── Build display frame ────────────────────────────────────────────────
    tbl = disp[[
        "rank", "horse_name", "post_position",
        "morning_line_odds", "win_probability",
        "fair_odds", "value_score", "bet_tag",
        "pace_fit_score", "form_score", "surface_dist_fit",
        "confidence_flag", "trainer", "jockey",
    ]].copy()

    tbl["Tag"]  = tbl["bet_tag"].map(TAG_ICON)
    tbl["Conf"] = tbl["confidence_flag"].map(CONF_ICON)
    tbl["Win%"] = (tbl["win_probability"] * 100).round(2)
    tbl["ML"]   = tbl["morning_line_odds"].apply(lambda x: f"{x:.0f}-1")
    tbl["Edge"] = tbl["value_score"].apply(_edge_str)

    display_cols = {
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
    show = tbl[list(display_cols.keys())].rename(columns=display_cols)

    styled = _style_board(
        tbl[["rank", "bet_tag", "confidence_flag"]].rename(
            columns={"rank": "rank", "bet_tag": "bet_tag", "confidence_flag": "confidence_flag"}
        )
    )
    st.dataframe(
        show,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Win %":     st.column_config.NumberColumn("Win %",     format="%.2f"),
            "Fair Odds": st.column_config.NumberColumn("Fair Odds", format="%.1f"),
            "Pace Fit":  st.column_config.NumberColumn("Pace Fit",  format="%.3f"),
            "Form":      st.column_config.NumberColumn("Form",      format="%.3f"),
            "SuDist":    st.column_config.NumberColumn("SuDist",    format="%.3f"),
        },
    )

    # ── Win probability bar chart ──────────────────────────────────────────
    st.subheader("Win Probability vs Market")
    chart_df = disp.sort_values("win_probability", ascending=True).copy()
    chart_df["win_pct"]    = chart_df["win_probability"] * 100
    chart_df["market_pct"] = chart_df["market_implied_prob"] * 100

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
    fig.add_trace(go.Scatter(
        y=chart_df["horse_name"], x=chart_df["market_pct"],
        name="Market %", mode="markers",
        marker=dict(symbol="diamond", size=8, color="#f0883e"),
    ))
    fig.update_layout(
        height=540, barmode="overlay",
        legend=dict(orientation="h", y=1.02),
        **_plotly_dark(),
    )
    st.plotly_chart(fig, use_container_width=True)

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
    m1.metric("Win %",      f"{horse['win_probability']*100:.1f}%")
    m2.metric("Fair Odds",  f"{horse['fair_odds']:.1f}-1")
    m3.metric("Model Edge", _edge_str(horse["value_score"]))
    m4.metric("ML Odds",    f"{horse['morning_line_odds']:.0f}-1")
    m5.metric("Market %",   f"{horse['market_implied_prob']*100:.1f}%")

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
