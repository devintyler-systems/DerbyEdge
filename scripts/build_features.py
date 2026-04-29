"""
scripts/build_features.py — Build V1 feature store for a Derby field.

Usage
-----
    python scripts/build_features.py                   # Derby 2026 (default)
    python scripts/build_features.py --card-id 3       # specific card

Outputs
-------
    output/derby_2026_features.csv     — sample feature table (all 20 entries)
    output/feature_catalog.csv         — machine-readable feature definitions
    output/feature_store_report.md     — QA report: populated vs null by feature
"""

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.features.builder import build_features

OUTPUT_DIR = ROOT / "output"

# ---------------------------------------------------------------------------
# Feature catalog definition
# Each tuple: (feature_name, tier, level, formula, source_columns,
#              null_handling, leak_safe_flag, implemented, null_reason)
# ---------------------------------------------------------------------------
CATALOG: list[tuple] = [
    # ── Speed / pace / form ────────────────────────────────────────────────
    ("speed_last",          "IMPLEMENTED", "entry",  "last_speed_fig",
     "entries.last_speed_fig", "keep_null",  1, 1, ""),
    ("speed_best",          "IMPLEMENTED", "entry",  "best_speed_fig",
     "entries.best_speed_fig", "keep_null",  1, 1, ""),
    ("speed_avg",           "IMPLEMENTED", "entry",  "avg_speed_fig",
     "entries.avg_speed_fig",  "keep_null",  1, 1, ""),
    ("beyer_last",          "IMPLEMENTED", "entry",  "beyer_fig",
     "entries.beyer_fig",      "keep_null",  1, 1, ""),
    ("speed_best_3",        "DEGRADED",    "entry",
     "(best_speed_fig + last_speed_fig + avg_speed_fig) / 3",
     "entries.best_speed_fig, entries.last_speed_fig, entries.avg_speed_fig",
     "null_if_any_missing", 1, 1,
     "True best-of-3 needs race-by-race horse_starts figs; using mean of seed aggregates"),
    ("pace_early_mean_3",   "PLACEHOLDER", "entry",
     "mean(speed_at_call1) over last 3 starts",
     "horse_starts.speed_figure + call_fraction_data",
     "keep_null", 1, 0, "missing horse_starts call-fraction splits"),
    ("pace_mid_mean_3",     "PLACEHOLDER", "entry",
     "mean(speed_at_call2) over last 3 starts",
     "horse_starts.speed_figure + call_fraction_data",
     "keep_null", 1, 0, "missing horse_starts call-fraction splits"),
    ("finish_energy_proxy", "DEGRADED",    "entry",
     "0.5*(1-early_intent) + 0.5*clamp(1-(last_finish-1)/19)",
     "entries.pace_style, entries.last_race_finish",
     "null_if_any_missing", 1, 1,
     "Race-by-race finish splits not available from seed"),
    ("form_cycle_idx",      "DEGRADED",    "entry",
     "0.6*career_itm_pct + 0.4*clamp(1-(last_finish-1)/10)",
     "entries.career_wins/places/shows/starts, entries.last_race_finish",
     "null_if_any_missing", 1, 1,
     "Recency weight uses last_finish as proxy; true cycle needs sequential figs"),
    ("layoff_days",         "IMPLEMENTED", "entry",  "last_race_days",
     "entries.last_race_days", "keep_null", 1, 1, ""),
    ("career_win_pct",      "IMPLEMENTED", "entry",
     "career_wins / career_starts",
     "entries.career_wins, entries.career_starts",
     "null_if_starts_zero", 1, 1, ""),
    ("career_itm_pct",      "IMPLEMENTED", "entry",
     "(career_wins+career_places+career_shows) / career_starts",
     "entries.career_wins, entries.career_places, entries.career_shows, entries.career_starts",
     "null_if_starts_zero", 1, 1, ""),
    # ── Class / field strength ──────────────────────────────────────────────
    ("class_delta",         "DEGRADED",    "entry",
     "(career_earnings - field_mean_earnings) / field_std_earnings",
     "entries.career_earnings (field-level)",
     "zero_if_no_variance", 1, 1,
     "Career earnings are cumulative; does not reflect per-race class level"),
    ("field_strength_last", "PLACEHOLDER", "entry",
     "mean(speed_figure) of last-race competitors",
     "horse_starts, entries (opponents)",
     "keep_null", 1, 0, "missing horse_starts for last race field"),
    ("horses_beaten_pct_last", "DEGRADED", "entry",
     "(typical_field_size - last_finish) / (typical_field_size - 1), typical=10",
     "entries.last_race_finish",
     "null_if_missing", 1, 1,
     "Typical field size assumed 10; actual field size not in seed"),
    ("field_size_exp",      "DEGRADED",    "entry",
     "norm(career_starts, lo=1, hi=15)",
     "entries.career_starts",
     "null_if_zero_starts", 1, 1,
     "Career_starts as proxy for large-field experience; not Derby-specific"),
    # ── Workouts / readiness ────────────────────────────────────────────────
    ("works_30d",           "IMPLEMENTED", "entry",
     "workouts_30 (aggregate count from seed)",
     "entries.workouts_30",
     "keep_null", 1, 1, ""),
    ("bullet_30d",          "PLACEHOLDER", "entry",
     "COUNT(*) WHERE workouts.work_grade='B' AND date >= race_date - 30",
     "workouts.work_grade, workouts.workout_date",
     "keep_null", 1, 0, "missing real workout records; workouts table empty"),
    ("days_since_last_work","PLACEHOLDER", "entry",
     "julianday(race_date) - julianday(max(workout_date))",
     "workouts.workout_date",
     "keep_null", 1, 0, "missing real workout records; workouts table empty"),
    ("work_readiness_score","DEGRADED",    "entry",
     "0.6*clamp(works_30d/6) + 0.4*clamp(gate_class/5)",
     "entries.workouts_30, entries.gate_class",
     "null_if_any_missing", 1, 1,
     "Bullet count not available; uses aggregate count + gate_class as proxy"),
    # ── Connections ──────────────────────────────────────────────────────────
    ("trainer_intent_proxy","DEGRADED",    "entry",
     "0.5*clamp(works_30d/6) + 0.5*clamp(1-(layoff-14)/56)",
     "entries.workouts_30, entries.last_race_days",
     "null_if_any_missing", 1, 1,
     "Cannot distinguish trainer sharpening vs holding back without real workouts"),
    ("trainer_jockey_itm_cond","PLACEHOLDER","entry",
     "itm_pct(trainer_id, jockey_id, surface='dirt', dist>=9.5f, window=180d)",
     "horse_starts, entries, race_cards",
     "keep_null", 1, 0, "missing horse_starts; v_connections_180 empty"),
    ("jockey_route_cond",   "PLACEHOLDER", "entry",
     "win_pct(jockey_id, dist>=9.5f, surface='dirt', window=180d)",
     "horse_starts, entries, race_cards",
     "keep_null", 1, 0, "missing horse_starts"),
    ("trainer_derby_cond",  "PLACEHOLDER", "entry",
     "itm_pct(trainer_id, stakes_name LIKE '%Derby%', window=5yr)",
     "horse_starts, entries, race_cards",
     "keep_null", 1, 0, "missing horse_starts; no Churchill stakes history"),
    # ── Fit ──────────────────────────────────────────────────────────────────
    ("surface_fit",         "DEGRADED",    "entry",
     "dirt_wins/dirt_starts; halved if dirt_starts=1; null if dirt_starts=0",
     "entries.dirt_wins, entries.dirt_starts",
     "null_if_no_dirt_starts", 1, 1,
     "Aggregate win% not split by class or distance"),
    ("distance_fit",        "DEGRADED",    "entry",
     "0.55*(dist_wins/dist_starts) + 0.45*stamina_index; if no dist starts: 0.45*stamina_index",
     "entries.dist_wins, entries.dist_starts, entries.stamina_index",
     "null_if_no_stamina_index", 1, 1,
     "Dist starts covers +-0.5f; stamina_index is seed heuristic"),
    ("route_progression",   "DEGRADED",    "entry",
     "same as distance_fit (Derby always a route)",
     "entries.dist_wins, entries.dist_starts, entries.stamina_index",
     "null_if_no_stamina_index", 1, 1,
     "Cannot track race-by-race progression without horse_starts"),
    ("pedigree_route_proxy","DEGRADED",    "entry",
     "SIRE_ROUTE_SCORE.get(sire.lower(), 0.72)",
     "horses.sire",
     "default_0.72_if_unknown", 1, 1,
     "Static lookup table; does not account for dam-line or individual variation"),
    # ── Post / trip / bias ───────────────────────────────────────────────────
    ("post_win_bias",       "PLACEHOLDER", "entry",
     "historical win% at this post position, Churchill Downs dirt route",
     "track_bias.post_skew_json",
     "keep_null", 1, 0, "track_bias table empty; no Churchill 2026 post history"),
    ("gate_reliability",    "DEGRADED",    "entry",
     "clamp(gate_class / 5)",
     "entries.gate_class",
     "null_if_missing", 1, 1,
     "gate_class is a seed heuristic (1-5); not a timed gate-break measurement"),
    ("trouble_recovery_proxy","PLACEHOLDER","entry",
     "1 - mean(trip_flag.severity) / 3 over last 3 starts",
     "trip_flags.severity",
     "keep_null", 1, 0, "trip_flags table empty"),
    ("traffic_resilience_proxy","DEGRADED","entry",
     "0.5*(1-early_intent) + 0.5*field_size_exp",
     "entries.pace_style, entries.career_starts",
     "null_if_no_pace_style", 1, 1,
     "Proxy only; does not account for actual trip trouble incidents"),
    # ── Race shape ────────────────────────────────────────────────────────────
    ("early_intent",        "IMPLEMENTED", "entry",
     "PACE_EARLY[pace_style]: front=1.0 presser=0.70 stalker=0.40 closer=0.10",
     "entries.pace_style",
     "null_if_no_pace_style", 1, 1, ""),
    ("run_style_bucket",    "IMPLEMENTED", "entry",
     "pace_style pass-through",
     "entries.pace_style",
     "null_if_no_pace_style", 1, 1, ""),
    ("pace_pressure",       "IMPLEMENTED", "race",
     "(COUNT front + COUNT presser) / field_size",
     "entries.pace_style (full field)",
     "zero_if_no_styles", 1, 1, ""),
    ("lone_speed_edge",     "IMPLEMENTED", "race",
     "1 if run_style_bucket='front' AND COUNT(front)==1 ELSE 0",
     "entries.pace_style (full field)",
     "zero_if_no_styles", 1, 1, ""),
    ("collapse_risk",       "IMPLEMENTED", "race",
     "same as pace_pressure (semantic alias)",
     "entries.pace_style (full field)",
     "zero_if_no_styles", 1, 1, ""),
    ("pace_fit_score",      "IMPLEMENTED", "race",
     "style x pace_pressure matrix: front/lone=0.90, front/contested=0.55, "
     "presser/low-pressure=0.75, presser/contested=0.65, "
     "stalker/pressure>=0.30=0.70, stalker/low=0.60, "
     "closer/pressure>=0.40=0.80, closer/low=0.55",
     "entries.pace_style (full field)",
     "null_if_no_style", 1, 1, ""),
    # ── Market / publicness ───────────────────────────────────────────────────
    ("market_implied_prob", "IMPLEMENTED", "market",
     "1 / (morning_line_odds + 1)",
     "entries.morning_line_odds",
     "never_null", 1, 1, ""),
    ("morning_line_rank",   "IMPLEMENTED", "market",
     "rank(market_implied_prob DESC) within field",
     "entries.morning_line_odds (full field)",
     "never_null", 1, 1, ""),
    ("publicness_score",    "DEGRADED",    "market",
     "market_implied_prob / career_win_pct",
     "entries.morning_line_odds, entries.career_wins, entries.career_starts",
     "null_if_zero_win_pct", 1, 1,
     "career_win_pct is career-total; does not condition on recency"),
    ("public_underlay_penalty","DEGRADED", "market",
     "clamp(z_score(publicness_score) / 3 + 0.5) within field",
     "publicness_score (full field)",
     "null_if_insufficient_field_variance", 1, 1,
     "Depends on publicness_score; inherits its degradation"),
    # ── Derby override ─────────────────────────────────────────────────────────
    ("classic_distance_projection","DEGRADED","entry",
     "0.60*stamina_index + 0.40*(dist_wins/dist_starts); "
     "if no dist starts: 0.60*stamina_index",
     "entries.stamina_index, entries.dist_wins, entries.dist_starts",
     "null_if_no_stamina_index", 1, 1,
     "stamina_index is a seed heuristic; true projection needs pace figs at 1m+"),
    ("churchill_readiness", "PLACEHOLDER", "entry",
     "itm_pct(horse_id, track='Churchill Downs', window=3yr)",
     "horse_starts, race_cards (Churchill Downs)",
     "keep_null", 1, 0, "no Churchill Downs historical starts in DB"),
    ("jan_apr_improvement_curve","PLACEHOLDER","entry",
     "slope(speed_figure ~ race_date) for starts Jan-Apr of race year",
     "horse_starts.speed_figure, race_cards.card_date",
     "keep_null", 1, 0, "missing horse_starts; cannot compute improvement curve"),
    ("derby_override_score","DEGRADED",    "entry",
     "weighted_avg of available: classic_dist_proj*0.35, pedigree*0.20, "
     "pace_fit*0.20, work_readiness*0.15, gate_reliability*0.10; "
     "weights renormalized to available components",
     "classic_distance_projection, pedigree_route_proxy, pace_fit_score, "
     "work_readiness_score, gate_reliability",
     "null_if_no_components", 1, 1,
     "3 of 5 intended components (churchill_readiness, jan_apr_improvement_curve) "
     "are PLACEHOLDER; score degrades gracefully on remaining components"),
]

CATALOG_COLUMNS = [
    "feature_name", "tier", "level", "formula", "source_columns",
    "null_handling", "leak_safe_flag", "implemented", "null_reason",
]


def write_catalog(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CATALOG_COLUMNS)
        writer.writerows(CATALOG)
    print(f"  [catalog]  {len(CATALOG)} features -> {path}")


def write_feature_report(feat_df: pd.DataFrame, path: Path) -> None:
    """Write output/feature_store_report.md."""
    path.parent.mkdir(parents=True, exist_ok=True)

    feature_cols = [
        c for c in feat_df.columns
        if c not in ("entry_id", "horse_id", "card_id", "horse_name",
                     "post_position", "build_ts")
    ]
    total_features = len(feature_cols)
    n_rows = len(feat_df)

    # per-feature null rate
    null_rates: list[tuple[str, int, float, str]] = []
    catalog_lookup = {row[0]: row for row in CATALOG}
    for col in feature_cols:
        null_count = int(feat_df[col].isna().sum())
        null_pct   = null_count / n_rows * 100
        tier       = catalog_lookup.get(col, ("", "UNKNOWN"))[1]
        null_rates.append((col, null_count, null_pct, tier))

    null_rates.sort(key=lambda x: -x[2])

    implemented = [c for c in CATALOG if c[7] == 1]
    degraded    = [c for c in CATALOG if c[1] == "DEGRADED"]
    placeholder = [c for c in CATALOG if c[7] == 0]

    lines = [
        "# DerbyEdge Feature Store Report",
        "",
        f"**Generated**: {feat_df['build_ts'].iloc[0]}  ",
        f"**Race**     : 2026 Kentucky Derby (G1) — Churchill Downs  ",
        f"**Entries**  : {n_rows}",
        "",
        "## Feature Count",
        "",
        f"| Tier | Count |",
        f"|------|-------|",
        f"| Total features | {total_features} |",
        f"| IMPLEMENTED    | {sum(1 for c in CATALOG if c[1] == 'IMPLEMENTED' and c[7] == 1)} |",
        f"| DEGRADED       | {len(degraded)} |",
        f"| PLACEHOLDER    | {len(placeholder)} |",
        "",
        "## Tier Definitions",
        "",
        "| Tier | Meaning |",
        "|------|---------|",
        "| IMPLEMENTED | Computed directly from seed columns; formula is exact |",
        "| DEGRADED | Proxy computation from aggregate seed fields; honest but less precise than row-level history |",
        "| PLACEHOLDER | In catalog but null; source table (horse_starts / workouts / track_bias / trip_flags) is empty |",
        "",
        "## Populated vs Null by Feature",
        "",
        "| Feature | Tier | Null count | Null % |",
        "|---------|------|-----------|--------|",
    ]
    for col, nc, np_, tier in null_rates:
        lines.append(f"| `{col}` | {tier} | {nc} | {np_:.0f}% |")

    lines += [
        "",
        "## Features Degraded Due to Missing History",
        "",
        "These features return a value but use aggregate proxies instead of row-level history:",
        "",
    ]
    for c in degraded:
        lines.append(f"- **`{c[0]}`** — {c[8]}")

    lines += [
        "",
        "## Features Currently Unavailable (PLACEHOLDER)",
        "",
        "These features are in the catalog but return NULL for every entry until",
        "real historical data is imported into `horse_starts`, `workouts`,",
        "`track_bias`, or `trip_flags`:",
        "",
    ]
    for c in placeholder:
        lines.append(f"- **`{c[0]}`** — {c[8]}")

    lines += [
        "",
        "## Top Null-Rate Features",
        "",
        "| Rank | Feature | Null % | Tier |",
        "|------|---------|--------|------|",
    ]
    top10 = [r for r in null_rates if r[2] > 0][:10]
    for i, (col, nc, np_, tier) in enumerate(top10, 1):
        lines.append(f"| {i} | `{col}` | {np_:.0f}% | {tier} |")

    lines += [
        "",
        "## Sample Output (first 5 entries by post position)",
        "",
        "```",
    ]
    sample_cols = [
        "horse_name", "post_position", "speed_last", "speed_best_3",
        "distance_fit", "classic_distance_projection",
        "derby_override_score", "market_implied_prob", "morning_line_rank",
    ]
    available = [c for c in sample_cols if c in feat_df.columns]
    lines.append(feat_df[available].head(5).to_string(index=False))
    lines.append("```")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [report]   feature store report -> {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="DerbyEdge V1 feature store builder")
    ap.add_argument("--card-id", type=int, metavar="ID",
                    help="card_id to build (default: first Kentucky Derby card)")
    args = ap.parse_args()

    print("\nDerbyEdge feature store build")
    print("=" * 44)

    # 1. Build features and persist to DB
    feat_df = build_features(card_id=args.card_id)
    print(f"  [builder]  {len(feat_df)} entries, {len(feat_df.columns)} columns")

    feature_cols = [
        c for c in feat_df.columns
        if c not in ("entry_id", "horse_id", "card_id", "horse_name",
                     "post_position", "build_ts")
    ]
    total = len(feature_cols)

    catalog_lookup = {row[0]: row for row in CATALOG}
    implemented_count = sum(
        1 for c in feature_cols
        if catalog_lookup.get(c, ("", "", "", "", "", "", 1, 1))[7] == 1
    )
    placeholder_count = sum(
        1 for c in feature_cols
        if catalog_lookup.get(c, ("", "", "", "", "", "", 1, 1))[7] == 0
    )
    null_any = feat_df[feature_cols].isna().any(axis=0)
    fully_populated = int((~null_any).sum())

    print(f"  [builder]  Features total       : {total}")
    print(f"  [builder]  Implemented          : {implemented_count}")
    print(f"  [builder]  Placeholder (null)   : {placeholder_count}")
    print(f"  [builder]  Fully populated cols : {fully_populated}")

    # 2. Write feature catalog CSV
    catalog_path = OUTPUT_DIR / "feature_catalog.csv"
    write_catalog(catalog_path)

    # 3. Write sample features CSV
    sample_path = OUTPUT_DIR / "derby_2026_features.csv"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    feat_df.to_csv(sample_path, index=False)
    print(f"  [output]   {len(feat_df)} rows -> {sample_path}")

    # 4. Write QA report
    report_path = OUTPUT_DIR / "feature_store_report.md"
    write_feature_report(feat_df, report_path)

    # 5. Print top null-rate summary
    null_pct = feat_df[feature_cols].isna().mean().sort_values(ascending=False)
    top_null = null_pct[null_pct > 0].head(8)
    if not top_null.empty:
        print("\n  Top null-rate features:")
        for feat, rate in top_null.items():
            tier = catalog_lookup.get(feat, ("", "UNKNOWN"))[1]
            print(f"    {feat:<35} {rate*100:5.0f}%  [{tier}]")

    print("\n  Done. Query feature store:")
    print("    SELECT horse_name, derby_override_score, pace_fit_score")
    print("    FROM feature_store ORDER BY derby_override_score DESC;\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
