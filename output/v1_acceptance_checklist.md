# DerbyEdge V1 — Acceptance Checklist

**Generated**: 2026-04-28  
**Release candidate**: `v1-derbyedge-local`  
**Regression**: 39/39 checks PASS (fresh-DB + checks-only runs)  
**Race**: 2026 Kentucky Derby (G1) · Churchill Downs · 2026-05-02  

---

## Summary

| Area | Status | Notes |
|------|--------|-------|
| Schema | PASS | 14 tables, 5 views, all indexes |
| ETL | PASS | 20 entries, 0 scratches, posts 1-20 |
| Feature store | PASS | 46 features: 22 IMPL, 12 DEG, 12 PHLD |
| Model baseline | PASS | seed_only_baseline, T=2.81, sum=1.000001 |
| Scoring board | PASS | 20 entries, ranks 1-20, derby_override=1 |
| Streamlit console | PASS | 4 tabs, app loads, no crash on TEXT features |
| Derby override | PASS | weights shifted, 9 low-conf, group sum=1.0 |

---

## 1. Schema

| Check | Result | Detail |
|-------|--------|--------|
| 14 tables present | PASS | tracks, race_cards, horses, people, entries, horse_starts, workouts, odds_snapshots, track_bias, trip_flags, model_registry, score_runs, entry_scores, feature_store |
| 5 views present | PASS | v_race_type, v_entries_live, v_horse_last_5, v_workout_30, v_connections_180 |
| score_runs.derby_override_active column | PASS | INTEGER NOT NULL DEFAULT 0, migration via ALTER TABLE |
| feature_store.run_style_bucket is TEXT | PASS | TEXT type; not REAL; safe from float coercion |
| CHECK constraints on model_type | PASS | 'xgboost','fallback','derby_override','seed_only_baseline' |
| score_runs FK to model_registry preserves model_id | PASS | INSERT OR IGNORE + UPDATE pattern; AUTOINCREMENT not bumped |

---

## 2. ETL

| Check | Result | Detail |
|-------|--------|--------|
| 20 entries ingested | PASS | 20 non-scratched entries via v_entries_live |
| Post positions 1-20 unique | PASS | No gaps, no duplicates |
| No scratched entries | PASS | scratch_flag=0 for all 20 |
| Ingest validation: 23 checks | PASS | 0 failures, 0 warnings |
| Seed aggregate columns populated | PASS | career_starts, speed_figs, stamina_index, etc. |
| Morning line overround | PASS | Dynamic 1 + 0.02*field_size; 20-horse Derby ~1.39 |

---

## 3. Feature Store

| Check | Result | Detail |
|-------|--------|--------|
| 20 rows in feature_store | PASS | One row per entry |
| 46 features in catalog | PASS | 22 IMPLEMENTED, 12 DEGRADED, 12 PLACEHOLDER |
| IMPLEMENTED features non-null | PASS | speed_last/best/avg, beyer_last, layoff_days, career_win/itm_pct, works_30d, market_implied_prob, morning_line_rank, pace_pressure, collapse_risk, early_intent, run_style_bucket, lone_speed_edge, pace_fit_score |
| PLACEHOLDER features all-null | PASS | pace_early_mean_3, pace_mid_mean_3, bullet_30d, days_since_last_work, trainer_jockey_itm_cond, jockey_route_cond, trainer_derby_cond, post_win_bias, trouble_recovery_proxy, field_strength_last, churchill_readiness, jan_apr_improvement_curve |
| run_style_bucket values valid | PASS | {closer, front, presser, stalker} |
| Two-pass computation | PASS | Race-level features (pace_pressure, pace_fit_score, morning_line_rank, public_underlay_penalty, derby_override_score) computed after full-field context |
| Derby override sub-components | PASS | classic_distance_projection, pedigree_route_proxy, traffic_resilience_proxy, gate_reliability, derby_override_score all populated |
| Sire route aptitude lookup | PASS | 18 sires mapped; unknown sires default to 0.72 |

---

## 4. Model Baseline (seed_only_baseline)

| Check | Result | Detail |
|-------|--------|--------|
| Model type | PASS | seed_only_baseline |
| Temperature calibration | PASS | T=2.81, grid search [1.0, 20.0] / 200 steps |
| Calibration target | PASS | Overround-adjusted morning line probs |
| Win probabilities sum to 1.0 | PASS | sum=1.000001 (within 1e-6 tolerance) |
| Feature importances normalized | PASS | Sum to 1.0; top: speed_best_3=0.10, pace_fit_score=0.0975 |
| Artifact saved | PASS | saved_models/derby_override_v1.pkl |
| Model registered | PASS | model_registry row with model_id preserved across re-runs |
| Base dirt_route weights | PASS | speed=0.25, form=0.18, dist_surf=0.17, race_shape=0.15, readiness=0.13, derby_ovr=0.07, market=0.05 (sum=1.00) |

---

## 5. Scoring Board

| Check | Result | Detail |
|-------|--------|--------|
| 20 entries in entry_scores | PASS | All entries scored |
| Ranks 1-20 unique | PASS | No ties, no gaps |
| win_probability sums to 1.0 +/- 1e-6 | PASS | 1.000001 |
| fair_odds matches 1/win_prob - 1 | PASS | max error 0.004891 (rounding) |
| model_edge matches win_prob - market_prob | PASS | max error 4.7e-05 |
| bet_tag values valid | PASS | {neutral, underlay}; no invalid values |
| confidence_flag is 0 or 1 | PASS | 0 bad rows |
| Bet threshold: edge >= +0.025 | PASS | 0 BET candidates (max edge +0.018) |
| Underlay threshold: edge < -0.015 | PASS | 2 underlays: Renegade (-0.034), Chief Wallabee (-0.019) |
| Low-confidence entries | PASS | 9 of 20 (Derby override tightened from base 7) |
| derby_override_active=1 | PASS | Correctly flagged in score_runs |
| Board CSV written | PASS | output/derby_2026_board.csv |
| Board MD written | PASS | output/derby_2026_board.md |
| Eval report written | PASS | output/model_evaluation_dirt_route.md |

---

## 6. Streamlit Operator Console

| Check | Result | Detail |
|-------|--------|--------|
| App starts without error | PASS | Streamlit 1.x, port 8501 |
| Tab 1 (Race Board) | PASS | 5-metric row, dataframe, bar chart |
| Tab 2 (Entry Details) | PASS | Horse selector, speed bar chart, group radar, feature audit |
| Tab 3 (Model Diagnostics) | PASS | Data source status, model metadata, feature tier summary, importance bar chart |
| Tab 4 (Methodology) | PASS | Group weights, calibration formula, thresholds, limitations |
| _safe_num on TEXT columns | PASS | run_style_bucket='closer' no longer raises ValueError |
| Derby override badge in sidebar | PASS | Gold "DERBY OVERRIDE" badge when active |
| Derby override banner in Tab 1 | PASS | Weight shift summary displayed |
| Derby sub-components in Tab 2 | PASS | 8 sub-components; PLACEHOLDERs marked NULL |
| Weight comparison table in Tab 3 | PASS | Base vs Derby vs delta columns |
| Cache TTL | PASS | 30s for board/features, 300s for catalog, permanent for artifact |
| Dark theme | PASS | .streamlit/config.toml: bg=#0d1117 |

---

## 7. Derby Override

| Check | Result | Detail |
|-------|--------|--------|
| is_derby_context() detection | PASS | dirt, >=9.5f, >=18 runners, "derby" in name, track=CD |
| Derby weights sum to 1.0 | PASS | 0.22+0.15+0.22+0.18+0.12+0.09+0.02 = 1.00 |
| distance_surface weight shifted up | PASS | 0.17 -> 0.22 (+5pp) |
| race_shape weight shifted up | PASS | 0.15 -> 0.18 (+3pp) |
| derby_override group weight shifted up | PASS | 0.07 -> 0.09 (+2pp) |
| market_prior weight shifted down | PASS | 0.05 -> 0.02 (-3pp) |
| traffic_resilience weight in race_shape | PASS | 0.35 -> 0.40 within group |
| Confidence tightening | PASS | dist_starts==2 + pedigree<0.75 = LOW; was MEDIUM in base |
| 9 low-confidence entries (was 7 base) | PASS | 2 additional entries caught by pedigree screen |
| Derby-specific missing flags | PASS | no_jan_apr_curve, no_churchill_readiness added |
| Artifact model name | PASS | derby_override_v1 (separate from dirt_route_v1) |
| Non-Derby races unaffected | PASS | is_derby_context() returns False; base dirt_route config used |

---

## Known Limitations (not defects)

| Item | Tier | Impact |
|------|------|--------|
| 12/46 features null (PLACEHOLDER) | Expected | horse_starts, workouts, track_bias, trip_flags tables empty |
| jan_apr_improvement_curve | PLACEHOLDER | Cannot compute improvement slope without sequential starts |
| churchill_readiness | PLACEHOLDER | No Churchill Downs historical data in DB |
| Calibration not outcome-validated | Expected | Temperature-scaled softmax vs morning line; no race results yet |
| No BET candidates | Noted | Max edge +0.018; none clear BET threshold of +0.025 |
| XGBoost path not implemented | Expected | Activates when horse_starts >= 50 rows |
| post_win_bias null | PLACEHOLDER | track_bias table empty |

---

## Regression Command

```bash
# Full fresh-DB pipeline + all checks:
python scripts/regression_test.py --fresh

# Checks only (existing DB):
python scripts/regression_test.py --checks-only
```

Output: `PASS=39  FAIL=0  WARN=0  SKIP=0` (fresh-DB run)
