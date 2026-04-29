# DerbyEdge Model Evaluation — dirt_route

**Generated** : 2026-04-29T08:42:27Z  
**Model name** : `derby_override_v1` (ID=1)  
**Version**    : `1.0.0-seed-only`  
**Model type** : seed_only_baseline  

## Model Quality Assessment

> **SEED-ONLY BASELINE — principled weighted composite from 46-feature catalog; no historical training data; probabilities are model-informed estimates, not calibrated predictions**

| Criterion | Status |
|-----------|--------|
| Training rows | 0 (need >= 50 for XGBoost) |
| Calibration | temperature-scaled softmax (T=4.15) |
| Calibration target | overround-adjusted morning line |
| Bet threshold | edge >= +0.025 |
| Underlay threshold | edge < -0.015 |
| Outcome validation | NOT POSSIBLE — race not yet run (2026-05-02) |

## Pre-Race Diagnostics

| Metric | Value | Interpretation |
|--------|-------|----------------|
| `sum_win_prob` | 1.000000 | Should be 1.000000 |
| `kendall_tau_vs_ml` | 0.4908 | Rank correlation with market |
| `kl_div_vs_ml` | 0.1573 | KL(model \|\| market) |
| `mean_edge_abs` | 0.0217 | Mean abs model-market divergence |
| `max_positive_edge` | 0.0276 | Best value candidate |
| `max_negative_edge` | -0.0544 | Worst underlay |
| `bet_count` | 0 | Horses with edge >= +0.025 |
| `underlay_count` | 5 | Horses with edge < -0.015 |

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
| 1 | `distance_fit` | 0.1320 | DEGRADED |
| 2 | `pace_fit_score` | 0.1080 | IMPLEMENTED |
| 3 | `derby_override_score` | 0.0900 | DEGRADED |
| 4 | `speed_best_3` | 0.0880 | DEGRADED |
| 5 | `surface_fit` | 0.0880 | DEGRADED |
| 6 | `speed_last` | 0.0770 | IMPLEMENTED |
| 7 | `traffic_resilience_proxy` | 0.0720 | DEGRADED |
| 8 | `work_readiness_score` | 0.0600 | DEGRADED |
| 9 | `beyer_last` | 0.0550 | IMPLEMENTED |
| 10 | `form_cycle_idx` | 0.0525 | DEGRADED |
| 11 | `class_delta` | 0.0450 | DEGRADED |
| 12 | `trainer_intent_proxy` | 0.0360 | DEGRADED |
| 13 | `horses_beaten_pct_last` | 0.0300 | DEGRADED |
| 14 | `finish_energy_proxy` | 0.0240 | DEGRADED |
| 15 | `career_win_pct` | 0.0225 | IMPLEMENTED |

## Group Weights

| Group | Weight |
|-------|--------|
| speed_quality | 0.22 |
| form_class | 0.15 |
| distance_surface | 0.22 |
| race_shape | 0.18 |
| readiness | 0.12 |
| derby_override | 0.09 |
| market_prior | 0.02 |

## Top 5 by Win Probability

| Rank | Horse | Win% | Fair Odds | Edge | Tag |
|------|-------|------|-----------|------|-----|
| 1 | Renegade | 13.4% | 6.4-1 | -0.021 | underlay |
| 2 | Further Ado | 13.2% | 6.5-1 | +0.021 | neutral |
| 3 | Commandment | 6.7% | 13.9-1 | -0.044 | underlay |
| 4 | So Happy | 4.4% | 21.9-1 | -0.005 | neutral |
| 5 | Danon Bourbon | 4.3% | 22.0-1 | +0.006 | neutral |

## Top 3 by Value Score

| Horse | ML Odds | Win% | Edge | Tag |
|-------|---------|------|------|-----|
| Intrepido | 50-1 | 4.3% | +0.028 | neutral |
| Six Speed | 50-1 | 4.3% | +0.028 | neutral |
| Further Ado | 6-1 | 13.2% | +0.021 | neutral |

## Limitations

- **Seed-only**: no access to race-by-race speed splits, real workout records,
  conditioned trainer/jockey stats, track bias, or trip flags.
- 12/46 features are PLACEHOLDER (null for all entries).
- 12/46 features are DEGRADED (proxy formulas from aggregate seed data).
- Calibration is temperature-scaled softmax tuned to morning line spread;
  NOT isotonic regression against actual race outcomes.
- **Do not use for real-money wagering without historical validation.**