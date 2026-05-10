# DerbyEdge Feature Store Report

**Generated**: 2026-05-10T01:10:21Z  
**Race**     : 2026 Kentucky Derby (G1) — Churchill Downs  
**Entries**  : 10

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
| `speed_last` | IMPLEMENTED | 10 | 100% |
| `speed_best` | IMPLEMENTED | 10 | 100% |
| `speed_avg` | IMPLEMENTED | 10 | 100% |
| `beyer_last` | IMPLEMENTED | 10 | 100% |
| `speed_best_3` | DEGRADED | 10 | 100% |
| `pace_early_mean_3` | PLACEHOLDER | 10 | 100% |
| `pace_mid_mean_3` | PLACEHOLDER | 10 | 100% |
| `finish_energy_proxy` | DEGRADED | 10 | 100% |
| `form_cycle_idx` | DEGRADED | 10 | 100% |
| `layoff_days` | IMPLEMENTED | 10 | 100% |
| `career_win_pct` | IMPLEMENTED | 10 | 100% |
| `career_itm_pct` | IMPLEMENTED | 10 | 100% |
| `field_strength_last` | PLACEHOLDER | 10 | 100% |
| `horses_beaten_pct_last` | DEGRADED | 10 | 100% |
| `field_size_exp` | DEGRADED | 10 | 100% |
| `works_30d` | IMPLEMENTED | 10 | 100% |
| `bullet_30d` | PLACEHOLDER | 10 | 100% |
| `days_since_last_work` | PLACEHOLDER | 10 | 100% |
| `work_readiness_score` | DEGRADED | 10 | 100% |
| `trainer_intent_proxy` | DEGRADED | 10 | 100% |
| `trainer_jockey_itm_cond` | PLACEHOLDER | 10 | 100% |
| `jockey_route_cond` | PLACEHOLDER | 10 | 100% |
| `trainer_derby_cond` | PLACEHOLDER | 10 | 100% |
| `surface_fit` | DEGRADED | 10 | 100% |
| `distance_fit` | DEGRADED | 10 | 100% |
| `route_progression` | DEGRADED | 10 | 100% |
| `post_win_bias` | PLACEHOLDER | 10 | 100% |
| `gate_reliability` | DEGRADED | 10 | 100% |
| `trouble_recovery_proxy` | PLACEHOLDER | 10 | 100% |
| `traffic_resilience_proxy` | DEGRADED | 10 | 100% |
| `early_intent` | IMPLEMENTED | 10 | 100% |
| `run_style_bucket` | IMPLEMENTED | 10 | 100% |
| `publicness_score` | DEGRADED | 10 | 100% |
| `classic_distance_projection` | DEGRADED | 10 | 100% |
| `churchill_readiness` | PLACEHOLDER | 10 | 100% |
| `jan_apr_improvement_curve` | PLACEHOLDER | 10 | 100% |
| `class_delta` | DEGRADED | 0 | 0% |
| `pedigree_route_proxy` | DEGRADED | 0 | 0% |
| `pace_pressure` | IMPLEMENTED | 0 | 0% |
| `lone_speed_edge` | IMPLEMENTED | 0 | 0% |
| `collapse_risk` | IMPLEMENTED | 0 | 0% |
| `pace_fit_score` | IMPLEMENTED | 0 | 0% |
| `market_implied_prob` | IMPLEMENTED | 0 | 0% |
| `morning_line_rank` | IMPLEMENTED | 0 | 0% |
| `public_underlay_penalty` | DEGRADED | 0 | 0% |
| `derby_override_score` | DEGRADED | 0 | 0% |

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
| 1 | `speed_last` | 100% | IMPLEMENTED |
| 2 | `speed_best` | 100% | IMPLEMENTED |
| 3 | `speed_avg` | 100% | IMPLEMENTED |
| 4 | `beyer_last` | 100% | IMPLEMENTED |
| 5 | `speed_best_3` | 100% | DEGRADED |
| 6 | `pace_early_mean_3` | 100% | PLACEHOLDER |
| 7 | `pace_mid_mean_3` | 100% | PLACEHOLDER |
| 8 | `finish_energy_proxy` | 100% | DEGRADED |
| 9 | `form_cycle_idx` | 100% | DEGRADED |
| 10 | `layoff_days` | 100% | IMPLEMENTED |

## Sample Output (first 5 entries by post position)

```
   horse_name  post_position speed_last speed_best_3 distance_fit classic_distance_projection  derby_override_score  market_implied_prob  morning_line_rank
 Gran Andrews              1       None         None         None                        None                 0.685             0.071429                  7
   Ascendance              2       None         None         None                        None                 0.685             0.083333                  6
General Issue              3       None         None         None                        None                 0.685             0.222222                  1
     Sisyphus              4       None         None         None                        None                 0.685             0.153846                  4
     Spurs Up              5       None         None         None                        None                 0.685             0.181818                  2
```