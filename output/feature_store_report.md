# DerbyEdge Feature Store Report

**Generated**: 2026-04-29T08:32:18Z  
**Race**     : 2026 Kentucky Derby (G1) — Churchill Downs  
**Entries**  : 20

## Feature Count

| Tier | Count |
|------|-------|
| Total features | 46 |
| IMPLEMENTED    | 16 |
| DEGRADED       | 18 |
| PLACEHOLDER    | 12 |

## Tier Definitions

| Tier | Meaning |
|------|---------|
| IMPLEMENTED | Computed directly from seed columns; formula is exact |
| DEGRADED | Proxy computation from aggregate seed fields; honest but less precise than row-level history |
| PLACEHOLDER | In catalog but null; source table (horse_starts / workouts / track_bias / trip_flags) is empty |

## Populated vs Null by Feature

| Feature | Tier | Null count | Null % |
|---------|------|-----------|--------|
| `pace_early_mean_3` | PLACEHOLDER | 20 | 100% |
| `pace_mid_mean_3` | PLACEHOLDER | 20 | 100% |
| `field_strength_last` | PLACEHOLDER | 20 | 100% |
| `bullet_30d` | PLACEHOLDER | 20 | 100% |
| `days_since_last_work` | PLACEHOLDER | 20 | 100% |
| `trainer_jockey_itm_cond` | PLACEHOLDER | 20 | 100% |
| `jockey_route_cond` | PLACEHOLDER | 20 | 100% |
| `trainer_derby_cond` | PLACEHOLDER | 20 | 100% |
| `post_win_bias` | PLACEHOLDER | 20 | 100% |
| `trouble_recovery_proxy` | PLACEHOLDER | 20 | 100% |
| `churchill_readiness` | PLACEHOLDER | 20 | 100% |
| `jan_apr_improvement_curve` | PLACEHOLDER | 20 | 100% |
| `speed_last` | IMPLEMENTED | 14 | 70% |
| `speed_best` | IMPLEMENTED | 14 | 70% |
| `speed_avg` | IMPLEMENTED | 14 | 70% |
| `beyer_last` | IMPLEMENTED | 14 | 70% |
| `speed_best_3` | DEGRADED | 14 | 70% |
| `finish_energy_proxy` | DEGRADED | 14 | 70% |
| `form_cycle_idx` | DEGRADED | 14 | 70% |
| `layoff_days` | IMPLEMENTED | 14 | 70% |
| `career_win_pct` | IMPLEMENTED | 14 | 70% |
| `career_itm_pct` | IMPLEMENTED | 14 | 70% |
| `class_delta` | DEGRADED | 14 | 70% |
| `horses_beaten_pct_last` | DEGRADED | 14 | 70% |
| `field_size_exp` | DEGRADED | 14 | 70% |
| `works_30d` | IMPLEMENTED | 14 | 70% |
| `work_readiness_score` | DEGRADED | 14 | 70% |
| `trainer_intent_proxy` | DEGRADED | 14 | 70% |
| `surface_fit` | DEGRADED | 14 | 70% |
| `distance_fit` | DEGRADED | 14 | 70% |
| `route_progression` | DEGRADED | 14 | 70% |
| `gate_reliability` | DEGRADED | 14 | 70% |
| `traffic_resilience_proxy` | DEGRADED | 14 | 70% |
| `early_intent` | IMPLEMENTED | 14 | 70% |
| `run_style_bucket` | IMPLEMENTED | 14 | 70% |
| `publicness_score` | DEGRADED | 14 | 70% |
| `public_underlay_penalty` | DEGRADED | 14 | 70% |
| `classic_distance_projection` | DEGRADED | 14 | 70% |
| `derby_override_score` | DEGRADED | 14 | 70% |
| `pedigree_route_proxy` | DEGRADED | 0 | 0% |
| `pace_pressure` | IMPLEMENTED | 0 | 0% |
| `lone_speed_edge` | IMPLEMENTED | 0 | 0% |
| `collapse_risk` | IMPLEMENTED | 0 | 0% |
| `pace_fit_score` | IMPLEMENTED | 0 | 0% |
| `market_implied_prob` | IMPLEMENTED | 0 | 0% |
| `morning_line_rank` | IMPLEMENTED | 0 | 0% |

## Features Degraded Due to Missing History

These features return a value but use aggregate proxies instead of row-level history:

- **`speed_best_3`** — True best-of-3 needs race-by-race horse_starts figs; using mean of seed aggregates
- **`finish_energy_proxy`** — Race-by-race finish splits not available from seed
- **`form_cycle_idx`** — Recency weight uses last_finish as proxy; true cycle needs sequential figs
- **`class_delta`** — Career earnings are cumulative; does not reflect per-race class level
- **`horses_beaten_pct_last`** — Typical field size assumed 10; actual field size not in seed
- **`field_size_exp`** — Career_starts as proxy for large-field experience; not Derby-specific
- **`work_readiness_score`** — Bullet count not available; uses aggregate count + gate_class as proxy
- **`trainer_intent_proxy`** — Cannot distinguish trainer sharpening vs holding back without real workouts
- **`surface_fit`** — Aggregate win% not split by class or distance
- **`distance_fit`** — Dist starts covers +-0.5f; stamina_index is seed heuristic
- **`route_progression`** — Cannot track race-by-race progression without horse_starts
- **`pedigree_route_proxy`** — Static lookup table; does not account for dam-line or individual variation
- **`gate_reliability`** — gate_class is a seed heuristic (1-5); not a timed gate-break measurement
- **`traffic_resilience_proxy`** — Proxy only; does not account for actual trip trouble incidents
- **`publicness_score`** — career_win_pct is career-total; does not condition on recency
- **`public_underlay_penalty`** — Depends on publicness_score; inherits its degradation
- **`classic_distance_projection`** — stamina_index is a seed heuristic; true projection needs pace figs at 1m+
- **`derby_override_score`** — 3 of 5 intended components (churchill_readiness, jan_apr_improvement_curve) are PLACEHOLDER; score degrades gracefully on remaining components

## Features Currently Unavailable (PLACEHOLDER)

These features are in the catalog but return NULL for every entry until
real historical data is imported into `horse_starts`, `workouts`,
`track_bias`, or `trip_flags`:

- **`pace_early_mean_3`** — missing horse_starts call-fraction splits
- **`pace_mid_mean_3`** — missing horse_starts call-fraction splits
- **`field_strength_last`** — missing horse_starts for last race field
- **`bullet_30d`** — missing real workout records; workouts table empty
- **`days_since_last_work`** — missing real workout records; workouts table empty
- **`trainer_jockey_itm_cond`** — missing horse_starts; v_connections_180 empty
- **`jockey_route_cond`** — missing horse_starts
- **`trainer_derby_cond`** — missing horse_starts; no Churchill stakes history
- **`post_win_bias`** — track_bias table empty; no Churchill 2026 post history
- **`trouble_recovery_proxy`** — trip_flags table empty
- **`churchill_readiness`** — no Churchill Downs historical starts in DB
- **`jan_apr_improvement_curve`** — missing horse_starts; cannot compute improvement curve

## Top Null-Rate Features

| Rank | Feature | Null % | Tier |
|------|---------|--------|------|
| 1 | `pace_early_mean_3` | 100% | PLACEHOLDER |
| 2 | `pace_mid_mean_3` | 100% | PLACEHOLDER |
| 3 | `field_strength_last` | 100% | PLACEHOLDER |
| 4 | `bullet_30d` | 100% | PLACEHOLDER |
| 5 | `days_since_last_work` | 100% | PLACEHOLDER |
| 6 | `trainer_jockey_itm_cond` | 100% | PLACEHOLDER |
| 7 | `jockey_route_cond` | 100% | PLACEHOLDER |
| 8 | `trainer_derby_cond` | 100% | PLACEHOLDER |
| 9 | `post_win_bias` | 100% | PLACEHOLDER |
| 10 | `trouble_recovery_proxy` | 100% | PLACEHOLDER |

## Sample Output (first 5 entries by post position)

```
    horse_name  post_position  speed_last  speed_best_3  distance_fit  classic_distance_projection  derby_override_score  market_implied_prob  morning_line_rank
      Renegade              1       105.0        106.33        0.7357                       0.7587                0.7685             0.200000                  1
         Albus              2         NaN           NaN           NaN                          NaN                   NaN             0.032258                 13
     Intrepido              3         NaN           NaN           NaN                          NaN                   NaN             0.019608                 19
   Litmus Test              4         NaN           NaN           NaN                          NaN                   NaN             0.032258                 13
Right To Party              5         NaN           NaN           NaN                          NaN                   NaN             0.032258                 13
```