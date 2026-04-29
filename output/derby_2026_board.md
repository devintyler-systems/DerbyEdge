# DerbyEdge Engine — 2026 Kentucky Derby Board

## Board Summary

| Field | Value |
|-------|-------|
| Model type | `seed_only_baseline` |
| Version | `1.0.0-seed-only` |
| Score timestamp | 2026-04-29T08:32:24Z |
| Model ID | 1 |
| Race | 2026 Kentucky Derby (G1) · Churchill Downs · 2026-05-02 |
| Total horses | 20 |
| Bet-tagged | 2 (Intrepido, Six Speed) |
| Underlay-tagged | 5 (Renegade, Commandment, Chief Wallabee, The Puma, Emerging Market) |
| Top win probability | Renegade 13.4% (fair 6.4-1) |
| Top value score | Intrepido +0.028 (bet) |
| Kendall tau vs market | 0.4908 |
| Mean abs edge | 0.0217 |
| Low-confidence entries | 16 of 20 (dist_starts <= 1; distance_fit unreliable) |

---

## Race Card

**Bet thresholds:** BET >= +0.025  |  UNDERLAY < -0.015  |  NEUTRAL otherwise

| Rank | Horse | Post | Trainer | Jockey | ML | Win% | Fair | PaceFit | Form | SuDist | Edge | Tag | Conf |
|------|-------|------|---------|--------|----|------|------|---------|------|--------|------|-----|------|
| 1 | **Renegade** | 1 | Todd Pletcher | Irad Ortiz Jr. | 4-1 | 13.4% | 6.4-1 | 0.750 | 1.000 | 0.928 | -0.021 | ~~UL~~ | MED |
| 2 | **Further Ado** | 18 | Brad Cox | John Velazquez | 6-1 | 13.2% | 6.5-1 | 0.750 | 0.626 | 0.867 | +0.021 | -- | MED |
| 3 | **Commandment** | 6 | Brad Cox | Luis Saez | 6-1 | 6.7% | 13.9-1 | 0.550 | 0.775 | 0.811 | -0.044 | ~~UL~~ | MED |
| 4 | **So Happy** | 8 | Mark Glatt | Mike Smith | 15-1 | 4.4% | 21.9-1 | 0.650 | 0.526 | 0.491 | -0.005 | -- | LOW! |
| 5 | **Danon Bourbon** | 7 | Manabu Ikezoe | Atsuya Nishimura | 20-1 | 4.3% | 22.0-1 | 0.650 | 0.526 | 0.491 | +0.006 | -- | LOW! |
| 6 | **Incredibolt** | 11 | Riley Mott | Jaime Torres | 20-1 | 4.3% | 22.0-1 | 0.650 | 0.526 | 0.491 | +0.006 | -- | LOW! |
| 7 | **Silent Tactic** | 13 | Mark Casse | Cristian Torres | 20-1 | 4.3% | 22.0-1 | 0.650 | 0.526 | 0.491 | +0.006 | -- | LOW! |
| 8 | **Potente** | 14 | Bob Baffert | Juan Hernandez | 20-1 | 4.3% | 22.0-1 | 0.650 | 0.526 | 0.491 | +0.006 | -- | LOW! |
| 9 | **Fulleffort** | 20 | Brad Cox | Tyler Gaffalione | 20-1 | 4.3% | 22.0-1 | 0.650 | 0.526 | 0.491 | +0.006 | -- | LOW! |
| 10 | **Albus** | 2 | Riley Mott | Manny Franco | 30-1 | 4.3% | 22.2-1 | 0.650 | 0.526 | 0.491 | +0.018 | -- | LOW! |
| 11 | **Litmus Test** | 4 | Bob Baffert | Martin Garcia | 30-1 | 4.3% | 22.2-1 | 0.650 | 0.526 | 0.491 | +0.018 | -- | LOW! |
| 12 | **Right To Party** | 5 | Kenny McPeek | Chris Elliott | 30-1 | 4.3% | 22.2-1 | 0.650 | 0.526 | 0.491 | +0.018 | -- | LOW! |
| 13 | **Wonder Dean** | 10 | Daisuke Takayanagi | Ryusei Sakai | 30-1 | 4.3% | 22.2-1 | 0.650 | 0.526 | 0.491 | +0.018 | -- | LOW! |
| 14 | **Pavlovian** | 16 | Doug O'Neill | Edward Maldonado | 30-1 | 4.3% | 22.2-1 | 0.650 | 0.526 | 0.491 | +0.018 | -- | LOW! |
| 15 | **Golden Tempo** | 19 | Cherie deVaux | Jose Ortiz | 30-1 | 4.3% | 22.2-1 | 0.650 | 0.526 | 0.491 | +0.018 | -- | LOW! |
| 16 | **Intrepido** | 3 | Jeff Mullins | Hector Berrios | 50-1 | 4.3% | 22.3-1 | 0.650 | 0.526 | 0.491 | +0.028 | **BET** | LOW! |
| 17 | **Six Speed** | 17 | Bhupat Seemar | Brian Hernandez Jr. | 50-1 | 4.3% | 22.3-1 | 0.650 | 0.526 | 0.491 | +0.028 | **BET** | LOW! |
| 18 | **Chief Wallabee** | 12 | Bill Mott | Junior Alvarado | 8-1 | 3.2% | 30.1-1 | 0.900 | 0.299 | 0.226 | -0.054 | ~~UL~~ | LOW! |
| 19 | **The Puma** | 9 | Gustavo Delgado | Javier Castellano | 10-1 | 2.1% | 46.6-1 | 0.750 | 0.290 | 0.136 | -0.050 | ~~UL~~ | LOW! |
| 20 | **Emerging Market** | 15 | Chad Brown | Flavien Prat | 15-1 | 0.7% | 148.0-1 | 0.600 | 0.278 | 0.000 | -0.042 | ~~UL~~ | MED |

### Low-Confidence Entries

These horses have `dist_starts <= 1`; their distance_fit score is based on `stamina_index` alone (no race history at 1.25 miles).

| Horse | Post | Dist Starts | Additional Missing Flags |
|-------|------|-------------|--------------------------|
| So Happy | 8 | 0 | dist_fit_single_start |
| Danon Bourbon | 7 | 0 | dist_fit_single_start |
| Incredibolt | 11 | 0 | dist_fit_single_start |
| Silent Tactic | 13 | 0 | dist_fit_single_start |
| Potente | 14 | 0 | dist_fit_single_start |
| Fulleffort | 20 | 0 | dist_fit_single_start |
| Albus | 2 | 0 | dist_fit_single_start |
| Litmus Test | 4 | 0 | dist_fit_single_start |
| Right To Party | 5 | 0 | dist_fit_single_start |
| Wonder Dean | 10 | 0 | dist_fit_single_start |
| Pavlovian | 16 | 0 | dist_fit_single_start |
| Golden Tempo | 19 | 0 | dist_fit_single_start |
| Intrepido | 3 | 0 | dist_fit_single_start |
| Six Speed | 17 | 0 | dist_fit_single_start |
| Chief Wallabee | 12 | 2 | dist_fit_single_start |
| The Puma | 9 | 2 | dist_fit_single_start |

---

## Diagnostics

### Feature Tier Summary

| Tier | Count | Meaning |
|------|-------|---------|
| IMPLEMENTED | 22 | Computed directly from seed columns |
| DEGRADED | 12 | Proxy formula from aggregate seed data; less precise than row-level history |
| PLACEHOLDER | 12 | Null; requires horse_starts / workouts / track_bias / trip_flags |

### Top 5 Feature Importances

| Feature | Weight | Tier |
|---------|--------|------|
| `distance_fit` | 0.1320 | DEGRADED |
| `pace_fit_score` | 0.1080 | IMPLEMENTED |
| `derby_override_score` | 0.0900 | DEGRADED |
| `speed_best_3` | 0.0880 | DEGRADED |
| `surface_fit` | 0.0880 | DEGRADED |

### Calibration

| Parameter | Value |
|-----------|-------|
| Method | temperature-scaled softmax |
| Temperature | 4.15 |
| Calibration target | overround-adjusted morning line |
| Sum of win probabilities | 1.000000 |
| KL divergence vs market | 0.1573 |

### Model Limitations

> This baseline uses seed-aggregate features and has not been validated on historical Derby preps.
> Fair odds and value scores are **directional only**.
> The following features are unavailable until real historical data is loaded:
> race-by-race speed splits, bullet workout counts, trainer/jockey conditioned stats,
> Churchill Downs track form, post-position win bias, trip trouble flags.
>
> **Do not wager without manual audit of speed figures, trip notes, and trainer intent.**