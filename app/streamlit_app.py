"""DerbyEdge Engine — Streamlit UI v0.5

Race-day readiness:
- Race selector → edge sheet → chaos toggle → visualizations
- Kelly stake column with bankroll + max-Kelly sidebar controls
- Bet-tag multiselect filter (defaults to BET/STRONG)
- CSV export for the current race's edge sheet
- Model trust panel: reliability curve from out-of-fold predictions
- Live odds uploader: drop a FanDuel-shaped CSV, edges recompute

Run:
    cd /path/to/derbyedge
    streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

# Path setup for `derbyedge` package
APP_DIR = Path(__file__).parent
ROOT = APP_DIR.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from derbyedge.scoring import score_entries
from derbyedge.edge_calc import build_edge_table
from derbyedge.chaos_patch import apply_derby_chaos_patch
from derbyedge.odds_math import kelly_fraction
from derbyedge.odds_ingest import adapter_manual_csv, write_snapshots
from derbyedge.odds_features import build_odds_features, write_odds_features
from derbyedge.evaluation import calibration_table
from derbyedge.screenshot_ingest import ingest_screenshot


DB_PATH = ROOT / "data" / "processed" / "derbyedge.sqlite"
MODEL_PATH = ROOT / "models" / "baseline_v0.3.pkl"
MODEL_PATH_FALLBACK = ROOT / "models" / "baseline_v0.2.pkl"
OOF_PATH = ROOT / "data" / "processed" / "oof_predictions.parquet"
UPLOADED_ODDS_DIR = ROOT / "samples"

st.set_page_config(
    page_title="DerbyEdge Engine",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------- Cached data loaders ----------
@st.cache_resource(show_spinner=False)
def get_conn():
    if not DB_PATH.exists():
        st.error(f"Database not found: {DB_PATH}\nRun: python scripts/run_pipeline.py")
        st.stop()
    return sqlite3.connect(str(DB_PATH), check_same_thread=False)


def get_model_path() -> Path:
    if MODEL_PATH.exists():
        return MODEL_PATH
    if MODEL_PATH_FALLBACK.exists():
        return MODEL_PATH_FALLBACK
    st.error(
        f"No trained model found. Expected {MODEL_PATH} or {MODEL_PATH_FALLBACK}.\n"
        "Run: python scripts/train_baseline.py"
    )
    st.stop()


@st.cache_data(show_spinner="Loading races…")
def load_race_index() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query(
        """SELECT r.race_id, r.race_date, r.race_number,
                  r.track_id, t.track_name, t.country,
                  r.surface, r.distance_id, r.distance_unit,
                  r.race_type, r.grade, r.purse_usa, r.number_of_runners
           FROM races r
           LEFT JOIN tracks t ON r.track_id = t.track_id
           ORDER BY r.race_date, r.track_id, r.race_number""",
        conn,
    )
    df["label"] = df.apply(
        lambda r: f"{r['race_date']} • {r['track_id']} R{r['race_number']} "
                  f"({r['surface']} {r['distance_id']}{r['distance_unit']}, "
                  f"{r['number_of_runners']} runners)",
        axis=1,
    )
    return df


@st.cache_data(show_spinner="Scoring race…")
def score_race(race_id: str, model_path_str: str, _cache_buster: int = 0) -> pd.DataFrame:
    conn = get_conn()
    scored = score_entries(conn, model_path_str)
    edge = build_edge_table(conn, model_probs=scored[["entry_id", "model_prob"]],
                            min_edge=0.20, strong_edge=0.40)
    edge = edge[edge["race_id"] == race_id].copy()
    return edge.merge(scored[["entry_id", "raw_prob"]], on="entry_id", how="left")


# Cache buster for forcing recompute after CSV upload
if "odds_cache_buster" not in st.session_state:
    st.session_state["odds_cache_buster"] = 0


# ---------- Sidebar ----------
st.sidebar.title("⚙️ DerbyEdge Engine")
st.sidebar.caption("v0.5.1 • screenshot ingestor + race-day readiness")

races = load_race_index()
if races.empty:
    st.error("No races loaded in DB.")
    st.stop()

# Filters
tracks = ["(all)"] + sorted(races["track_id"].dropna().unique().tolist())
sel_track = st.sidebar.selectbox("Track", tracks, index=0)
view = races if sel_track == "(all)" else races[races["track_id"] == sel_track]

surfaces = ["(all)"] + sorted(view["surface"].dropna().unique().tolist())
sel_surface = st.sidebar.selectbox("Surface", surfaces, index=0)
if sel_surface != "(all)":
    view = view[view["surface"] == sel_surface]

if view.empty:
    st.sidebar.warning("No races match filters.")
    st.stop()

sel_label = st.sidebar.selectbox("Race", view["label"].tolist(), index=0)
sel_race = view[view["label"] == sel_label].iloc[0]
race_label_safe = (
    f"{sel_race['race_date']}_{sel_race['track_id']}_R{sel_race['race_number']}"
    .replace(" ", "_").replace("/", "-")
)

st.sidebar.divider()
st.sidebar.subheader("Bankroll & Kelly")
bankroll = st.sidebar.number_input(
    "Bankroll ($)", min_value=0, max_value=1_000_000, value=1000, step=100,
    help="Total betting bankroll. Stake$ = bankroll × capped Kelly.",
)
max_kelly_pct = st.sidebar.slider(
    "Max Kelly fraction (%)", 1, 25, 5, 1,
    help="Hard cap on per-bet bankroll percentage. Default 5% = quarter-Kelly.",
)

st.sidebar.divider()
st.sidebar.subheader("Chaos overlay")
chaos_on = st.sidebar.toggle("Apply Derby Chaos Patch", value=False,
                             help="Reallocate WinProb mass toward dark-horse beneficiaries")
chaos_idx = st.sidebar.slider("Chaos index", 0.0, 1.0, 0.85, 0.05, disabled=not chaos_on)

st.sidebar.divider()
st.sidebar.subheader("Edge thresholds")
min_edge_pct = st.sidebar.slider("Bet edge floor (%)", 5, 50, 20, 5)
strong_edge_pct = st.sidebar.slider("Strong edge floor (%)", 25, 75, 40, 5)

st.sidebar.divider()
st.sidebar.subheader("Filters")
ALL_TAGS = ["STRONG", "BET", "PASS", "FADE", "NO_MARKET"]
tag_filter = st.sidebar.multiselect(
    "Show tags", options=ALL_TAGS, default=["STRONG", "BET"],
    help="Filter the edge sheet. Default = playable tags only.",
)
hide_no_market = st.sidebar.checkbox("Hide entries with no market data", value=False)

st.sidebar.divider()
st.sidebar.subheader("Odds template for this race")
try:
    _tmpl_conn = get_conn()
    _entries_df = pd.read_sql_query(
        "SELECT program_number FROM entries WHERE race_id = ? ORDER BY program_number",
        _tmpl_conn, params=[sel_race["race_id"]],
    )
    if _entries_df.empty:
        st.sidebar.caption(f"No entries found for `{sel_race['race_id']}`.")
    else:
        _tmpl = _entries_df.copy()
        _tmpl.insert(0, "race_id", sel_race["race_id"])
        _tmpl.insert(0, "book_id", "fanduel")
        _tmpl["decimal_odds"] = ""
        _tmpl["american_odds"] = ""
        _tmpl["is_scratched"] = 0
        _tmpl["is_morning_line"] = 0
        _tmpl["captured_at"] = ""
        _tmpl = _tmpl[["book_id", "race_id", "program_number",
                        "decimal_odds", "american_odds",
                        "is_scratched", "is_morning_line", "captured_at"]]
        _tmpl_csv = _tmpl.to_csv(index=False).encode("utf-8")
        st.sidebar.caption(f"race_id: `{sel_race['race_id']}`")
        st.sidebar.download_button(
            label="⬇️ Download odds template (this race)",
            data=_tmpl_csv,
            file_name=f"odds_{race_label_safe}.csv",
            mime="text/csv",
        )
except Exception as _e:
    st.sidebar.caption(f"Template unavailable: {_e}")

st.sidebar.divider()
st.sidebar.subheader("Live odds upload")
st.sidebar.caption(
    "Drop a CSV in the odds_template.csv schema "
    "(book_id, race_id, program_number, decimal_odds…). "
    "On upload, snapshots are written and edges recompute."
)
uploaded = st.sidebar.file_uploader("Upload odds CSV", type=["csv"], key="odds_upload")
if uploaded is not None:
    try:
        UPLOADED_ODDS_DIR.mkdir(parents=True, exist_ok=True)
        target = UPLOADED_ODDS_DIR / "uploaded_odds.csv"
        target.write_bytes(uploaded.getvalue())
        conn = get_conn()
        recs = adapter_manual_csv(target, conn=conn)
        n_unresolved = sum(1 for r in recs if r.entry_id is None)
        n_snap = write_snapshots(conn, recs)
        feats = build_odds_features(conn)
        n_feat = write_odds_features(conn, feats)
        st.session_state["odds_cache_buster"] += 1
        score_race.clear()
        if n_unresolved:
            st.sidebar.warning(
                f"Ingested {n_snap} snapshots → {n_feat} feature rows. "
                f"⚠️ {n_unresolved}/{n_snap} rows had no matching entry "
                f"(race_id or program_number didn't match DB). "
                f"Those odds won't appear in the edge sheet."
            )
        else:
            st.sidebar.success(
                f"Ingested {n_snap} snapshots → {n_feat} feature rows. Edges refreshed."
            )
    except Exception as e:
        st.sidebar.error(f"Upload failed: {e}")

st.sidebar.divider()
st.sidebar.subheader("Sportsbook screenshot \U0001F4F8")
st.sidebar.caption(
    "Drop a BetOnline / FanDuel / DRF race-card screenshot. "
    "Vision (Claude) extracts the race + runners + odds. "
    "Requires `ANTHROPIC_API_KEY` env var. "
    "Note: ingested races have no PP history \u2192 model runs uniform prior. "
    "Use this as an odds dashboard, not for model edge."
)
ss_overwrite = st.sidebar.checkbox(
    "Overwrite if race exists", value=False, key="ss_overwrite",
    help="Re-ingest a race you've already loaded (replaces entries+odds).",
)
ss_uploaded = st.sidebar.file_uploader(
    "Upload screenshot", type=["jpg", "jpeg", "png", "webp"], key="ss_upload",
)
if ss_uploaded is not None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.sidebar.error(
            "ANTHROPIC_API_KEY not set. Run in PowerShell:\n"
            '`[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")`'
            " then restart Streamlit."
        )
    else:
        with st.sidebar.status("Parsing screenshot via Claude vision\u2026", expanded=False):
            try:
                conn = get_conn()
                summary = ingest_screenshot(
                    conn, ss_uploaded.getvalue(),
                    overwrite=ss_overwrite,
                )
                # Rebuild odds_features so the new race appears in edge_calc
                feats = build_odds_features(conn)
                write_odds_features(conn, feats)
                # Bust caches so the new race shows in the dropdown
                load_race_index.clear()
                score_race.clear()
                st.session_state["odds_cache_buster"] += 1
                st.sidebar.success(
                    f"Ingested {summary['race_id']} — "
                    f"{summary['n_runners']} runners, "
                    f"{summary['n_odds_rows']} odds rows. "
                    "Pick it from the Race dropdown above."
                )
            except ValueError as e:
                if "already exists" in str(e):
                    st.sidebar.warning(
                        f"{e} \u2014 tick \u2018Overwrite if race exists\u2019 to replace."
                    )
                else:
                    st.sidebar.error(f"Screenshot parse failed: {e}")
            except Exception as e:
                st.sidebar.error(f"Screenshot ingest failed: {type(e).__name__}: {e}")


# ---------- Main pane ----------
st.title("🏇 DerbyEdge Edge Sheet")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Track", sel_race["track_id"])
c2.metric("Race", f"R{sel_race['race_number']}")
c3.metric("Surface", sel_race["surface"] or "?")
c4.metric("Distance", f"{sel_race['distance_id']}{sel_race['distance_unit']}")
c5.metric("Runners", int(sel_race["number_of_runners"] or 0))

# No-PP-history badge: count how many entries in this race have ANY prior PPs.
# Screenshot-ingested races will have zero → model runs uniform prior.
try:
    _conn = get_conn()
    _pp_check = pd.read_sql_query(
        """SELECT COUNT(DISTINCT e.entry_id) AS n_entries,
                  COUNT(DISTINCT hs.entry_id) AS n_with_pp
           FROM entries e
           LEFT JOIN horse_starts hs ON hs.entry_id = e.entry_id
           WHERE e.race_id = ?""",
        _conn, params=[sel_race["race_id"]],
    ).iloc[0]
    if int(_pp_check["n_entries"]) > 0 and int(_pp_check["n_with_pp"]) == 0:
        st.warning(
            "\U0001F4F8 **No PP history for this race.** "
            "Looks like a screenshot-ingested race shell. The model has no "
            "horse history to learn from — `Model%` falls back to the base-rate "
            "prior and is essentially uniform. Use this view for odds/devig/Kelly "
            "math, not for genuine model edge."
        )
except Exception:
    pass

model_path = get_model_path()
edge = score_race(sel_race["race_id"], str(model_path),
                  _cache_buster=st.session_state["odds_cache_buster"])

if edge.empty:
    st.warning("No entries scored for this race.")
    st.stop()

# Apply chaos overlay (operates on a frame with WinProb_base)
disp = edge.copy()
disp = disp.rename(columns={"model_prob": "WinProb_base"})
try:
    feat = pd.read_parquet(ROOT / "data" / "processed" / "entry_features.parquet")
    chaos_cols = [
        "entry_id", "late_fig_z", "speed_trend_slope", "finish_energy_score",
        "pacefit_score", "distance_proj_score", "recent_form_score",
        "publicness_score", "run_style",
        "workout_score", "days_since_last_work", "n_workouts_30d",
        "has_recent_handily",
    ]
    have = [c for c in chaos_cols if c in feat.columns]
    disp = disp.merge(feat[have], on="entry_id", how="left")
    disp = disp.rename(columns={"speed_trend_slope": "devcurve"})
except Exception:
    pass

if chaos_on:
    needed = ["late_fig_z", "devcurve", "finish_energy_score", "pacefit_score",
              "distance_proj_score", "recent_form_score", "publicness_score", "run_style"]
    for n in needed:
        if n not in disp.columns:
            disp[n] = 0.0 if n != "run_style" else "P"
        disp[n] = disp[n].fillna(0.0 if n != "run_style" else "P")
    disp["horse"] = disp["horse_name"]
    try:
        disp = apply_derby_chaos_patch(disp, chaos_index=chaos_idx)
        disp["model_prob_display"] = disp["WinProb_final"]
    except Exception as e:
        st.warning(f"Chaos patch failed: {e}. Showing base probabilities.")
        disp["model_prob_display"] = disp["WinProb_base"]
        disp["DarkHorseFlag"] = False
        disp["DarkHorseTier"] = ""
else:
    disp["model_prob_display"] = disp["WinProb_base"]
    disp["DarkHorseFlag"] = False
    disp["DarkHorseTier"] = ""

# Recompute edge with current threshold sliders against the displayed model_prob
def _reedge(row):
    mp, mkt, odec = row.get("model_prob_display"), row.get("market_prob"), row.get("decimal_odds_used")
    if pd.isna(mp) or pd.isna(mkt) or mkt in (None, 0) or pd.isna(odec):
        return pd.Series({"edge_live": None, "ev_live": None, "tag_live": "NO_MARKET",
                          "kelly_frac": None, "stake_dollar": None})
    e = (mp - mkt) / mkt
    ev = mp * (odec - 1.0) - (1 - mp)
    if e >= strong_edge_pct / 100.0 and ev > 0:
        tag = "STRONG"
    elif e >= min_edge_pct / 100.0 and ev > 0:
        tag = "BET"
    elif e <= -min_edge_pct / 100.0:
        tag = "FADE"
    else:
        tag = "PASS"
    # Kelly: capped at user's max-Kelly slider (as a fraction of bankroll)
    cap = max_kelly_pct / 100.0
    kf = kelly_fraction(mp, odec, cap=cap) if tag in ("BET", "STRONG") else 0.0
    stake = kf * bankroll
    return pd.Series({
        "edge_live": round(e, 4),
        "ev_live": round(ev, 4),
        "tag_live": tag,
        "kelly_frac": round(kf, 4),
        "stake_dollar": round(stake, 2),
    })

# Drop any pre-existing edge/Kelly columns from build_edge_table so concat
# below doesn't create duplicates that break Styler.
for _c in ("edge", "ev", "kelly_frac", "bet_tag",
           "edge_live", "ev_live", "tag_live", "stake_dollar"):
    if _c in disp.columns:
        disp = disp.drop(columns=[_c])

reedge = disp.apply(_reedge, axis=1)
disp = pd.concat([disp.reset_index(drop=True), reedge.reset_index(drop=True)], axis=1)
# Belt-and-suspenders: dedupe any remaining duplicate columns.
disp = disp.loc[:, ~disp.columns.duplicated()]

# ---------- Edge sheet table ----------
st.subheader("Edge sheet")

table_cols = [
    "post_position", "program_number", "horse_name",
    "WinProb_base", "model_prob_display", "market_prob", "decimal_odds_used",
    "fair_decimal", "edge_live", "ev_live", "tag_live",
    "kelly_frac", "stake_dollar", "best_book_now",
    "workout_score", "days_since_last_work", "n_workouts_30d",
    "DarkHorseFlag", "DarkHorseTier",
]
table_cols = [c for c in table_cols if c in disp.columns]

view_df = disp[table_cols].copy()
if hide_no_market:
    view_df = view_df[view_df["tag_live"] != "NO_MARKET"]
if tag_filter:
    view_df = view_df[view_df["tag_live"].isin(tag_filter)]

view_df = view_df.sort_values("model_prob_display", ascending=False)

# Build raw export frame BEFORE pretty formatting
csv_bytes = view_df.to_csv(index=False).encode("utf-8")

st.download_button(
    label="⬇️ Export this race to CSV",
    data=csv_bytes,
    file_name=f"derbyedge_{race_label_safe}.csv",
    mime="text/csv",
    help="Raw numeric values, current filters applied.",
)

# Format
display_df = view_df.copy()
for c in ("WinProb_base", "model_prob_display", "market_prob"):
    if c in display_df.columns:
        display_df[c] = display_df[c].apply(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—")
for c in ("decimal_odds_used", "fair_decimal"):
    if c in display_df.columns:
        display_df[c] = display_df[c].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
for c in ("edge_live", "ev_live"):
    if c in display_df.columns:
        display_df[c] = display_df[c].apply(lambda x: f"{x:+.1%}" if pd.notna(x) else "—")
if "kelly_frac" in display_df.columns:
    display_df["kelly_frac"] = display_df["kelly_frac"].apply(
        lambda x: f"{x*100:.2f}%" if pd.notna(x) and x > 0 else "—"
    )
if "stake_dollar" in display_df.columns:
    display_df["stake_dollar"] = display_df["stake_dollar"].apply(
        lambda x: f"${x:,.2f}" if pd.notna(x) and x > 0 else "—"
    )
for c in ("workout_score",):
    if c in display_df.columns:
        display_df[c] = display_df[c].apply(
            lambda x: f"{x:.1f}" if pd.notna(x) else "—"
        )
for c in ("days_since_last_work", "n_workouts_30d"):
    if c in display_df.columns:
        display_df[c] = display_df[c].apply(
            lambda x: f"{int(x)}" if pd.notna(x) else "—"
        )

display_df = display_df.rename(columns={
    "post_position": "PP",
    "program_number": "#",
    "horse_name": "Horse",
    "WinProb_base": "Model%",
    "model_prob_display": "Display%",
    "market_prob": "Market%",
    "decimal_odds_used": "Odds",
    "fair_decimal": "Fair",
    "edge_live": "Edge",
    "ev_live": "EV",
    "tag_live": "Tag",
    "kelly_frac": "Kelly%",
    "stake_dollar": "Stake$",
    "best_book_now": "Book",
    "workout_score": "WrkScr",
    "days_since_last_work": "LastWk",
    "n_workouts_30d": "#Wks30d",
    "DarkHorseFlag": "DarkHorse",
    "DarkHorseTier": "Tier",
})


def _color_tag(val: str):
    return {
        "STRONG": "background-color: #2e7d32; color: white",
        "BET":    "background-color: #66bb6a; color: black",
        "FADE":   "background-color: #c62828; color: white",
        "PASS":   "background-color: #424242; color: white",
        "NO_MARKET": "background-color: #757575; color: white",
    }.get(val, "")


styled = display_df.style.map(_color_tag, subset=["Tag"]) if "Tag" in display_df.columns else display_df
st.dataframe(styled, use_container_width=True, hide_index=True)

# Bankroll summary line
if "stake_dollar" in view_df.columns:
    total_stake = view_df["stake_dollar"].fillna(0).sum()
    n_bets = (view_df["stake_dollar"].fillna(0) > 0).sum()
    if n_bets > 0:
        st.caption(
            f"💰 {n_bets} playable bet{'s' if n_bets != 1 else ''} • "
            f"total stake ${total_stake:,.2f} "
            f"({total_stake / bankroll * 100:.1f}% of bankroll)"
            if bankroll > 0 else f"💰 {n_bets} playable bets • total stake ${total_stake:,.2f}"
        )

# ---------- Visualizations ----------
st.subheader("Model vs Market")

viz_df = disp[["horse_name", "model_prob_display", "market_prob"]].dropna().copy()
viz_df = viz_df.rename(columns={"model_prob_display": "Model", "market_prob": "Market"})
if not viz_df.empty:
    long = viz_df.melt(id_vars="horse_name", value_vars=["Model", "Market"],
                       var_name="series", value_name="prob")
    chart = (
        alt.Chart(long)
        .mark_bar()
        .encode(
            y=alt.Y("horse_name:N", sort="-x", title=None),
            x=alt.X("prob:Q", axis=alt.Axis(format="%"), title="Win probability"),
            color=alt.Color("series:N",
                            scale=alt.Scale(domain=["Model", "Market"],
                                            range=["#1f77b4", "#ff7f0e"])),
            tooltip=["horse_name", "series", alt.Tooltip("prob:Q", format=".1%")],
        )
        .properties(height=max(220, 24 * len(viz_df)))
    )
    st.altair_chart(chart, use_container_width=True)
else:
    st.info("No market data available for this race — showing model probabilities only.")
    bar_df = disp[["horse_name", "model_prob_display"]].dropna()
    if not bar_df.empty:
        st.bar_chart(bar_df.set_index("horse_name"))

# ---------- Devig probabilities (separate panel) ----------
st.subheader("Devig market prob bars")
mkt_df = disp[["horse_name", "market_prob"]].dropna()
if not mkt_df.empty:
    mkt_df = mkt_df.sort_values("market_prob", ascending=False)
    chart2 = (
        alt.Chart(mkt_df)
        .mark_bar(color="#ff7f0e")
        .encode(
            y=alt.Y("horse_name:N", sort="-x", title=None),
            x=alt.X("market_prob:Q", axis=alt.Axis(format="%"), title="Devigged market prob"),
            tooltip=["horse_name", alt.Tooltip("market_prob:Q", format=".1%")],
        )
        .properties(height=max(180, 22 * len(mkt_df)))
    )
    st.altair_chart(chart2, use_container_width=True)
else:
    st.caption("No devig market data yet for this race.")

# ---------- Drift sparkline ----------
st.subheader("Odds drift (per horse, last snapshots)")
try:
    conn = get_conn()
    snaps = pd.read_sql_query(
        """SELECT entry_id, captured_at, decimal_odds, book_id
           FROM odds_snapshots
           WHERE entry_id IN (SELECT entry_id FROM entries WHERE race_id = ?)
           ORDER BY captured_at""",
        conn, params=[sel_race["race_id"]],
    )
    if snaps.empty:
        st.caption("No odds snapshots ingested for this race.")
    else:
        snaps["captured_at"] = pd.to_datetime(snaps["captured_at"])
        snaps = snaps.merge(
            disp[["entry_id", "horse_name"]], on="entry_id", how="left"
        )
        med = (snaps.groupby(["horse_name", "captured_at"])["decimal_odds"]
                    .median().reset_index())
        chart3 = (
            alt.Chart(med)
            .mark_line(point=True)
            .encode(
                x=alt.X("captured_at:T", title="Time"),
                y=alt.Y("decimal_odds:Q", scale=alt.Scale(reverse=True),
                        title="Decimal odds (lower = shorter)"),
                color=alt.Color("horse_name:N", legend=None),
                tooltip=["horse_name", "captured_at:T",
                         alt.Tooltip("decimal_odds:Q", format=".2f")],
            )
            .properties(height=320)
        )
        st.altair_chart(chart3, use_container_width=True)
except Exception as e:
    st.caption(f"Drift chart unavailable: {e}")


# ---------- Model trust / calibration ----------
with st.expander("📊 Model trust — reliability curve"):
    if not OOF_PATH.exists():
        st.caption(
            "No out-of-fold predictions found. Run "
            "`PYTHONPATH=src python scripts/train_baseline.py` to generate."
        )
    else:
        try:
            oof = pd.read_parquet(OOF_PATH)
            st.caption(
                f"Walk-forward OOF predictions: {len(oof):,} rows • "
                f"{oof['fold_id'].nunique()} folds • "
                f"base rate {oof['y_true'].mean()*100:.1f}%"
            )
            ctab = calibration_table(oof["y_true"].values, oof["y_pred"].values, n_bins=10)
            # Standardize column names from calibration_table
            cal = ctab.rename(columns={
                col: col.lower() for col in ctab.columns
            })
            # find pred / observed columns flexibly
            pred_col = next((c for c in cal.columns if "pred" in c or "p_mean" in c or c == "p"), None)
            obs_col = next((c for c in cal.columns if "obs" in c or "y_mean" in c or c == "y"), None)
            cnt_col = next((c for c in cal.columns if "n" == c or "count" in c), None)
            if pred_col is None or obs_col is None:
                st.caption(f"Calibration columns unrecognized: {list(cal.columns)}")
            else:
                cal_plot = cal[[pred_col, obs_col] + ([cnt_col] if cnt_col else [])].copy()
                cal_plot = cal_plot.rename(columns={
                    pred_col: "predicted",
                    obs_col: "observed",
                    **({cnt_col: "count"} if cnt_col else {}),
                })

                ref_line = pd.DataFrame({"x": [0, cal_plot["predicted"].max() * 1.05 if len(cal_plot) else 1]})
                ref_line["y"] = ref_line["x"]

                base = alt.Chart(cal_plot).encode(
                    x=alt.X("predicted:Q", title="Predicted win probability",
                            axis=alt.Axis(format="%")),
                    y=alt.Y("observed:Q", title="Observed win rate",
                            axis=alt.Axis(format="%")),
                )
                points = base.mark_circle(size=120, color="#1f77b4").encode(
                    tooltip=[
                        alt.Tooltip("predicted:Q", format=".1%"),
                        alt.Tooltip("observed:Q", format=".1%"),
                    ] + ([alt.Tooltip("count:Q", title="bin n")] if "count" in cal_plot.columns else []),
                )
                line = base.mark_line(color="#1f77b4", strokeDash=[2, 2])
                ref = (
                    alt.Chart(ref_line)
                    .mark_line(color="#666", strokeDash=[6, 4])
                    .encode(x="x:Q", y="y:Q")
                )
                st.altair_chart(ref + line + points, use_container_width=True)

                # ECE quick stat
                if "count" in cal_plot.columns and cal_plot["count"].sum() > 0:
                    w = cal_plot["count"] / cal_plot["count"].sum()
                    ece = float(np.abs(cal_plot["predicted"] - cal_plot["observed"]).mul(w).sum())
                else:
                    ece = float(np.abs(cal_plot["predicted"] - cal_plot["observed"]).mean())
                st.caption(
                    f"Expected Calibration Error (10-bin): **{ece:.3f}** • "
                    f"Above the diagonal = model under-confident; below = over-confident."
                )
        except Exception as e:
            st.caption(f"Calibration plot unavailable: {e}")


# ---------- Footer ----------
with st.expander("ℹ️ Model info"):
    st.write(f"**Model file:** `{model_path}`")
    st.write(f"**DB:** `{DB_PATH}`")
    st.write(
        "Walk-forward backtest baseline (2.1K rows, 6 quarterly folds): "
        "AUC 0.643 • log-loss 0.401 • Brier 0.117 • ECE 0.060."
    )
    st.write(
        "Connection priors (jockey, trainer, J/T) are wired and tested no-leak, "
        "but disabled by default at this corpus size. Set "
        "`DERBYEDGE_USE_CONNECTION_PRIORS=1` to activate."
    )
