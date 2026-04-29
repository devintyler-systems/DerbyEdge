# DerbyEdge Model Evaluation — dirt_route

**Generated** : 2026-04-29T00:48:34Z  
**Model name** : `dirt_route_v1`  
**Version**    : `1.0.0-seed-only`  
**Model type** : seed_only_baseline  

## Model Quality Assessment

> **SEED-ONLY BASELINE — principled weighted composite from 46-feature catalog; no historical training data; probabilities are model-informed estimates, not calibrated predictions**

| Criterion | Status |
|-----------|--------|
| Training rows | 0 (need >= 50 for XGBoost) |
| Calibration method | temperature-scaled softmax (T=2.81) |
| Calibration target | overround-adjusted morning line |
| Outcome validation | NOT POSSIBLE — race not yet run (2026-05-02) |

## Pre-Race Diagnostics

Outcome-based metrics (log_loss, Brier, top-1) require actual race results.
The metrics below are computable before the race.

| Metric | Value | Interpretation |
|--------|-------|----------------|
| `sum_win_prob` | 1.000000 | Should be 1.000000 |
| `kendall_tau_vs_ml` | 0.8724 | Rank correlation with market; 1=identical, 0=no overlap |
| `kl_div_vs_ml` | 0.0155 | KL(model \|\| market); 0=identical, higher=more divergent |
| `mean_edge_abs` | 0.0074 | Mean absolute model-market divergence per horse |
| `max_positive_edge` | 0.0191 | Best value play |
| `max_negative_edge` | -0.0291 | Worst underlay |
| `bet_count` | 0 | Horses with edge >= +0.025 |
| `underlay_count` | 1 | Horses with edge <= -0.020 |

## Post-Race Metrics (N/A — Race Not Run)

| Metric | Value |
|--------|-------|
| log_loss | N/A |
| brier_score | N/A |
| calibration_error | N/A |
| top1_hit_rate | N/A |
| edge_bucket_roi | N/A |

## Top Feature Importances

Effective weight = within-group weight x group weight, normalized to sum 1.0.

| Rank | Feature | Effective Weight |
|------|---------|-----------------|
| 1 | `speed_best_3` | 0.1000 |
| 2 | `pace_fit_score` | 0.0975 |
| 3 | `distance_fit` | 0.0935 |
| 4 | `speed_last` | 0.0875 |
| 5 | `surface_fit` | 0.0765 |
| 6 | `derby_override_score` | 0.0700 |
| 7 | `work_readiness_score` | 0.0650 |
| 8 | `form_cycle_idx` | 0.0630 |
| 9 | `beyer_last` | 0.0625 |
| 10 | `class_delta` | 0.0540 |
| 11 | `traffic_resilience_proxy` | 0.0525 |
| 12 | `market_implied_prob` | 0.0500 |
| 13 | `trainer_intent_proxy` | 0.0390 |
| 14 | `horses_beaten_pct_last` | 0.0360 |
| 15 | `career_win_pct` | 0.0270 |

## Group Weights

| Group | Weight |
|-------|--------|
| speed_quality | 0.25 |
| form_class | 0.18 |
| distance_surface | 0.17 |
| race_shape | 0.15 |
| readiness | 0.13 |
| derby_override | 0.07 |
| market_prior | 0.05 |

## Top 5 by Win Probability

| Rank | Horse | Win% | Fair Odds | Edge | Tag |
|------|-------|------|-----------|------|-----|
| 1 | Commandment | 12.2% | 7.2-1 | +0.019 | neutral |
| 2 | Renegade | 11.5% | 7.7-1 | -0.029 | underlay |
| 3 | Further Ado | 11.4% | 7.8-1 | +0.011 | neutral |
| 4 | Valiant Knight | 8.4% | 10.8-1 | +0.004 | neutral |
| 5 | The Puma | 6.6% | 14.1-1 | +0.001 | neutral |

## Top 3 by Value Score (Model Edge)

| Horse | ML Odds | Win% | Edge | Tag |
|-------|---------|------|------|-----|
| Commandment | 6-1 | 12.2% | +0.019 | neutral |
| Silver Bullet | 25-1 | 4.3% | +0.016 | neutral |
| Further Ado | 6-1 | 11.4% | +0.011 | neutral |

## Limitations

- This model is a **seed-only baseline**. It has no access to:
  - Race-by-race speed figures (horse_starts empty)
  - Real workout records (workouts empty)
  - Conditioned trainer/jockey stats (v_connections_180 empty)
  - Track bias (track_bias empty)
  - Trip flags (trip_flags empty)
- 12 of 46 features are PLACEHOLDER (null for all entries).
- 12 features are DEGRADED (proxy formulas from aggregate seed data).
- Calibration is a temperature-scaled softmax tuned to morning line spread,
  NOT isotonic regression calibrated against actual race outcomes.
- **Do not use these probabilities for real-money wagering without historical validation.**