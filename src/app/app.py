import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.utils.db import get_connection

st.set_page_config(
    page_title="DerbyEdge Engine",
    page_icon="🏇",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    [data-testid="stAppViewContainer"] { background: #0d1117; }
    [data-testid="stHeader"] { background: transparent; }
    .metric-card {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 10px;
        padding: 16px;
        text-align: center;
    }
    .rank-gold   { color: #ffd700; font-weight: bold; font-size: 1.4em; }
    .rank-silver { color: #c0c0c0; font-weight: bold; font-size: 1.4em; }
    .rank-bronze { color: #cd7f32; font-weight: bold; font-size: 1.4em; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=30)
def load_data() -> pd.DataFrame | None:
    try:
        conn = get_connection()
        df = pd.read_sql(
            """
            SELECT dp.run_id, dp.rank, dp.horse_name, dp.post_position,
                   dp.morning_line_odds, dp.win_probability, dp.place_probability,
                   dp.show_probability, dp.composite_score, dp.model_type,
                   df.trainer, df.jockey, df.sire, df.dam, df.owner,
                   df.pace_style, df.stamina_index, df.career_starts,
                   df.career_wins, df.career_places, df.career_shows,
                   df.career_earnings, df.best_speed_figure, df.avg_speed_figure,
                   df.last_race_speed_figure, df.beyer_speed_figure,
                   df.dirt_starts, df.dirt_wins, df.last_race_days_ago,
                   df.last_race_finish, df.workouts_past_30, df.gate_class,
                   hf.speed_score, hf.form_score, hf.distance_score,
                   hf.class_score, hf.workout_score
            FROM derby_predictions dp
            JOIN derby_field df ON dp.horse_name = df.horse_name
            LEFT JOIN horse_features hf ON dp.horse_name = hf.horse_name
            ORDER BY dp.rank
            """,
            conn,
        )
        conn.close()
        return df if not df.empty else None
    except Exception:
        return None


def _no_data_screen() -> None:
    st.warning("No predictions loaded yet. Run the pipeline first:")
    st.code(
        "python scripts/init_db.py\n"
        "python scripts/ingest.py\n"
        "python scripts/build_features.py\n"
        "python scripts/score.py",
        language="bash",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

hdr_col, btn_col = st.columns([5, 1])
with hdr_col:
    st.markdown("# 🏇 DerbyEdge Engine")
    st.caption("2026 Kentucky Derby — AI-Powered Prediction System")
with btn_col:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("⟳ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

df = load_data()

tab1, tab2, tab3, tab4 = st.tabs(
    ["🏆 Leaderboard", "🐴 Horse Profiles", "📊 Analysis", "ℹ️ About"]
)

# ── Leaderboard ───────────────────────────────────────────────────────────────
with tab1:
    if df is None:
        _no_data_screen()
    else:
        model_type = df["model_type"].iloc[0]
        run_id = df["run_id"].iloc[0]
        st.caption(f"Model: **{model_type}** | Run: `{run_id}`")

        top3 = df.head(3)
        medals = ["🥇", "🥈", "🥉"]
        c1, c2, c3 = st.columns(3)
        for col, (_, horse), medal in zip([c1, c2, c3], top3.iterrows(), medals):
            with col:
                st.metric(
                    label=f"{medal} {horse['horse_name']}",
                    value=f"{horse['win_probability']:.1f}%",
                    delta=f"Post {int(horse['post_position'])} · {horse['morning_line_odds']:.0f}-1",
                )

        st.divider()

        disp = df[
            [
                "rank", "horse_name", "post_position", "morning_line_odds",
                "win_probability", "place_probability", "show_probability",
                "pace_style", "trainer", "jockey",
            ]
        ].copy()
        disp.columns = [
            "Rank", "Horse", "Post", "ML Odds",
            "Win %", "Place %", "Show %",
            "Pace", "Trainer", "Jockey",
        ]
        disp["ML Odds"] = disp["ML Odds"].apply(lambda x: f"{x:.0f}-1")
        disp["Win %"]   = disp["Win %"].apply(lambda x: f"{x:.1f}%")
        disp["Place %"] = disp["Place %"].apply(lambda x: f"{x:.1f}%")
        disp["Show %"]  = disp["Show %"].apply(lambda x: f"{x:.1f}%")

        st.dataframe(disp, use_container_width=True, hide_index=True)

        st.subheader("Win Probability by Horse")
        sorted_df = df.sort_values("win_probability")
        fig = px.bar(
            sorted_df,
            x="win_probability",
            y="horse_name",
            orientation="h",
            color="win_probability",
            color_continuous_scale="RdYlGn",
            text=sorted_df["win_probability"].apply(lambda x: f"{x:.1f}%"),
            labels={"win_probability": "Win %", "horse_name": ""},
        )
        fig.update_traces(textposition="outside")
        fig.update_layout(
            height=620,
            showlegend=False,
            coloraxis_showscale=False,
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#e6edf3",
            margin=dict(l=10, r=60, t=10, b=10),
        )
        st.plotly_chart(fig, use_container_width=True)

# ── Horse Profiles ────────────────────────────────────────────────────────────
with tab2:
    if df is None:
        _no_data_screen()
    else:
        options = [
            f"{int(r['rank'])}. {r['horse_name']} (Post {int(r['post_position'])})"
            for _, r in df.iterrows()
        ]
        selected_label = st.selectbox("Select a horse", options)
        selected_rank = int(selected_label.split(".")[0])
        horse = df[df["rank"] == selected_rank].iloc[0]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Win Probability",   f"{horse['win_probability']:.1f}%")
        c2.metric("Place Probability", f"{horse['place_probability']:.1f}%")
        c3.metric("Show Probability",  f"{horse['show_probability']:.1f}%")
        c4.metric("Morning Line",      f"{horse['morning_line_odds']:.0f}-1")

        st.divider()

        left, right = st.columns(2)
        with left:
            st.subheader("Profile")
            win_pct = horse["career_wins"] / max(horse["career_starts"], 1) * 100
            itm_pct = (
                (horse["career_wins"] + horse["career_places"] + horse["career_shows"])
                / max(horse["career_starts"], 1)
                * 100
            )
            st.markdown(f"**Trainer:** {horse['trainer']}")
            st.markdown(f"**Jockey:** {horse['jockey']}")
            st.markdown(f"**Sire / Dam:** {horse['sire']} / {horse['dam']}")
            st.markdown(f"**Owner:** {horse['owner']}")
            st.markdown(
                f"**Career Record:** {int(horse['career_starts'])}-"
                f"{int(horse['career_wins'])}-{int(horse['career_places'])}-"
                f"{int(horse['career_shows'])}"
            )
            st.markdown(f"**Win%:** {win_pct:.1f}%  |  **ITM%:** {itm_pct:.1f}%")
            st.markdown(f"**Career Earnings:** ${int(horse['career_earnings']):,}")
            st.markdown(
                f"**Dirt Record:** {int(horse['dirt_wins'])} wins / {int(horse['dirt_starts'])} starts"
            )
            st.markdown(
                f"**Last Race:** {int(horse['last_race_days_ago'])} days ago "
                f"(finished {int(horse['last_race_finish'])})"
            )
            st.markdown(f"**Pace Style:** {horse['pace_style'].title()}")
            st.markdown(f"**Stamina Index:** {horse['stamina_index']:.2f}")

        with right:
            st.subheader("Speed Figures")
            fig_spd = go.Figure(
                go.Bar(
                    x=["Best", "Last Race", "Avg", "Beyer"],
                    y=[
                        horse["best_speed_figure"],
                        horse["last_race_speed_figure"],
                        horse["avg_speed_figure"],
                        horse["beyer_speed_figure"],
                    ],
                    marker_color=["#ffd700", "#4facfe", "#a8edea", "#f093fb"],
                    text=[
                        horse["best_speed_figure"],
                        horse["last_race_speed_figure"],
                        round(horse["avg_speed_figure"], 1),
                        horse["beyer_speed_figure"],
                    ],
                    textposition="outside",
                )
            )
            fig_spd.update_layout(
                yaxis_range=[70, 125],
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#e6edf3",
                margin=dict(t=10, b=10),
            )
            st.plotly_chart(fig_spd, use_container_width=True)

            # Radar for feature scores
            if horse.get("speed_score") is not None and not pd.isna(horse["speed_score"]):
                cats = ["Speed", "Form", "Distance", "Class", "Workout"]
                vals = [
                    horse["speed_score"] * 100,
                    horse["form_score"] * 100,
                    horse["distance_score"] * 100,
                    horse["class_score"] * 100,
                    horse["workout_score"] * 100,
                ]
                fig_rad = go.Figure(
                    go.Scatterpolar(
                        r=vals + [vals[0]],
                        theta=cats + [cats[0]],
                        fill="toself",
                        fillcolor="rgba(255, 99, 71, 0.25)",
                        line=dict(color="#ff6347", width=2),
                    )
                )
                fig_rad.update_layout(
                    polar=dict(
                        radialaxis=dict(visible=True, range=[0, 100]),
                        bgcolor="rgba(0,0,0,0)",
                    ),
                    showlegend=False,
                    plot_bgcolor="rgba(0,0,0,0)",
                    paper_bgcolor="rgba(0,0,0,0)",
                    font_color="#e6edf3",
                    margin=dict(t=20, b=20),
                )
                st.plotly_chart(fig_rad, use_container_width=True)

# ── Analysis ──────────────────────────────────────────────────────────────────
with tab3:
    if df is None:
        _no_data_screen()
    else:
        df2 = df.copy()
        df2["implied_prob"] = 100.0 / (df2["morning_line_odds"] + 1)
        df2["value_edge"] = df2["win_probability"] - df2["implied_prob"]

        left, right = st.columns(2)

        with left:
            st.subheader("Model Win% vs Market Odds")
            fig_sc = px.scatter(
                df2,
                x="morning_line_odds",
                y="win_probability",
                text="horse_name",
                size="win_probability",
                color="pace_style",
                labels={
                    "morning_line_odds": "ML Odds (to 1)",
                    "win_probability": "Model Win %",
                },
            )
            fig_sc.update_traces(textposition="top center", textfont_size=8)
            fig_sc.update_layout(
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#e6edf3",
            )
            st.plotly_chart(fig_sc, use_container_width=True)

        with right:
            st.subheader("Value Horses (Model vs Market)")
            val_disp = df2[["horse_name", "morning_line_odds", "win_probability", "implied_prob", "value_edge"]].copy()
            val_disp = val_disp.sort_values("value_edge", ascending=False)
            val_disp.columns = ["Horse", "ML Odds", "Model Win%", "Market Win%", "Edge"]
            val_disp["ML Odds"]    = val_disp["ML Odds"].apply(lambda x: f"{x:.0f}-1")
            val_disp["Model Win%"] = val_disp["Model Win%"].apply(lambda x: f"{x:.1f}%")
            val_disp["Market Win%"]= val_disp["Market Win%"].apply(lambda x: f"{x:.1f}%")
            val_disp["Edge"]       = val_disp["Edge"].apply(lambda x: f"{x:+.1f}%")
            st.dataframe(val_disp, use_container_width=True, hide_index=True)

        st.subheader("Pace Style Distribution")
        pace_fig = px.pie(
            df2,
            names="pace_style",
            values="win_probability",
            title="Win Probability by Pace Style",
            hole=0.4,
        )
        pace_fig.update_layout(
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#e6edf3",
        )
        st.plotly_chart(pace_fig, use_container_width=True)

# ── About ─────────────────────────────────────────────────────────────────────
with tab4:
    st.header("About DerbyEdge Engine")
    st.markdown(
        """
**DerbyEdge Engine** is a fully local horse-racing prediction system for the
2026 Kentucky Derby. No cloud. No APIs. No subscriptions.

### Technology Stack
| Layer | Tool |
|-------|------|
| Storage | SQLite 3 |
| Data | pandas |
| ML | XGBoost + scikit-learn |
| UI | Streamlit + Plotly |

### Scoring Methodology

When fewer than 50 historical race entries are loaded the engine uses a
**weighted composite fallback model**:

| Factor | Weight | Signal |
|--------|--------|--------|
| Speed Figures | 25 % | Best / last / avg speed ratings |
| Market Odds | 20 % | Morning line (crowd-wisdom proxy) |
| Career Form | 15 % | Win % + ITM % |
| Distance / Stamina | 15 % | Derby-dist record + stamina index |
| Freshness | 10 % | Days-since-last-race curve |
| Earnings (Class) | 8 % | Career purse earnings |
| Workouts | 7 % | Recent work-tab volume × gate class |

Once you import 50 + historical `race_entries` rows the engine automatically
switches to a trained **XGBoost classifier**.

### Pipeline Commands
```bash
python scripts/init_db.py        # 1. Create schema
python scripts/ingest.py         # 2. Load seed CSV
python scripts/build_features.py # 3. Feature store
python scripts/score.py          # 4. Produce predictions
streamlit run src/app/app.py     # 5. Launch UI
```

---
*For entertainment purposes only.*
        """
    )
