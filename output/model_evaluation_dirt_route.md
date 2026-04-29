# DerbyEdge Model Evaluation — dirt_route

**Generated** : 2026-04-29T00:57:15Z  
**Model name** : `dirt_route_v1` (ID=1)  
**Version**    : `1.0.0-seed-only`  
**Model type** : seed_only_baseline  

## Model Quality Assessment

> **SEED-ONLY BASELINE — principled weighted composite from 46-feature catalog; no historical training data; probabilities are model-informed estimates, not calibrated predictions**

| Criterion | Status |
|-----------|--------|
| Training rows | 0 (need >= 50 for XGBoost) |
| Calibration | temperature-scaled softmax (T=2.81) |
| Calibration target | overround-adjusted morning line |
| Bet threshold | edge >= +0.025 |
| Underlay threshold | edge < -0.015 |
| Outcome validation | NOT POSSIBLE — race not yet run (2026-05-02) |

## Pre-Race Diagnostics

| Metric | Value | Interpretation |
|--------|-------|----------------|
| `sum_win_prob` | 1.000000 | Should be 1.000000 |
| `kendall_tau_vs_ml` | 0.8724 | Rank correlation with market |
| `kl_div_vs_ml` | 0.0155 | KL(model \|\| market) |
| `mean_edge_abs` | 0.0074 | Mean abs model-market divergence |
| `max_positive_edge` | 0.0191 | Best value candidate |
| `max_negative_edge` | -0.0291 | Worst underlay |
| `bet_count` | 0 | Horses with edge >= +0.025 |
| `underlay_count` | 2 | Horses with edge < -0.015 |

## Post-Race Metrics (N/A — Race Not Run)

| Metric | Value |
|--------|-------|
| log_loss | N/A |
| brier_score | N/A |
| calibration_error | N/A |
| top1_hit_rate | N/A |
| edge_bucket_roi | N/A |

## Top Feature Importances

| Rank | Feature | Weight | Tier |
|------|---------|--------|------|
| 1 | `speed_best_3` | 0.1000 | DEGRADED |
| 2 | `pace_fit_score` | 0.0975 | IMPLEMENTED |
| 3 | `distance_fit` | 0.0935 | DEGRADED |
| 4 | `speed_last` | 0.0875 | IMPLEMENTED |
| 5 | `surface_fit` | 0.0765 | DEGRADED |
| 6 | `derby_override_score` | 0.0700 | DEGRADED |
| 7 | `work_readiness_score` | 0.0650 | DEGRADED |
| 8 | `form_cycle_idx` | 0.0630 | DEGRADED |
| 9 | `beyer_last` | 0.0625 | IMPLEMENTED |
| 10 | `class_delta` | 0.0540 | DEGRADED |
| 11 | `traffic_resilience_proxy` | 0.0525 | DEGRADED |
| 12 | `market_implied_prob` | 0.0500 | IMPLEMENTED |
| 13 | `trainer_intent_proxy` | 0.0390 | DEGRADED |
| 14 | `horses_beaten_pct_last` | 0.0360 | DEGRADED |
| 15 | `career_win_pct` | 0.0270 | IMPLEMENTED |

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

## Top 3 by Value Score

| Horse | ML Odds | Win% | Edge | Tag |
|-------|---------|------|------|-----|
| Commandment | 6-1 | 12.2% | +0.019 | neutral |
| Silver Bullet | 25-1 | 4.3% | +0.016 | neutral |
| Further Ado | 6-1 | 11.4% | +0.011 | neutral |

## Limitations

- **Seed-only**: no access to race-by-race speed splits, real workout records,
  conditioned trainer/jockey stats, track bias, or trip flags.
- 12/46 features are PLACEHOLDER (null for all entries).
- 12/46 features are DEGRADED (proxy formulas from aggregate seed data).
- Calibration is temperature-scaled softmax tuned to morning line spread;
  NOT isotonic regression against actual race outcomes.
- **Do not use for real-money wagering without historical validation.**