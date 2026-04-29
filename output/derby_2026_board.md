# DerbyEdge Engine — 2026 Kentucky Derby Board

## Board Summary

| Field | Value |
|-------|-------|
| Model type | `seed_only_baseline` |
| Version | `1.0.0-seed-only` |
| Score timestamp | 2026-04-29T00:57:15Z |
| Model ID | 1 |
| Race | 2026 Kentucky Derby (G1) · Churchill Downs · 2026-05-02 |
| Total horses | 20 |
| Bet-tagged | 0 (none) |
| Underlay-tagged | 2 (Renegade, Chief Wallabee) |
| Top win probability | Commandment 12.2% (fair 7.2-1) |
| Top value score | Commandment +0.019 (neutral) |
| Kendall tau vs market | 0.8724 |
| Mean abs edge | 0.0074 |
| Low-confidence entries | 7 of 20 (dist_starts <= 1; distance_fit unreliable) |

---

## Race Card

**Bet thresholds:** BET >= +0.025  |  UNDERLAY < -0.015  |  NEUTRAL otherwise

| Rank | Horse | Post | Trainer | Jockey | ML | Win% | Fair | PaceFit | Form | SuDist | Edge | Tag | Conf |
|------|-------|------|---------|--------|----|------|------|---------|------|--------|------|-----|------|
| 1 | **Commandment** | 6 | Brad Cox | Javier Castellano | 6-1 | 12.2% | 7.2-1 | 0.800 | 0.893 | 0.915 | +0.019 | -- | MED |
| 2 | **Renegade** | 1 | Todd Pletcher | John Velazquez | 4-1 | 11.5% | 7.7-1 | 0.650 | 1.000 | 0.981 | -0.029 | ~~UL~~ | MED |
| 3 | **Further Ado** | 18 | Bill Mott | Irad Ortiz Jr. | 6-1 | 11.4% | 7.8-1 | 0.650 | 0.884 | 0.938 | +0.011 | -- | MED |
| 4 | **Valiant Knight** | 11 | Todd Pletcher | Luis Saez | 8-1 | 8.4% | 10.8-1 | 0.650 | 0.864 | 0.881 | +0.004 | -- | MED |
| 5 | **The Puma** | 9 | Doug O'Neill | Victor Espinoza | 10-1 | 6.6% | 14.1-1 | 0.650 | 0.718 | 0.707 | +0.001 | -- | MED |
| 6 | **Chief Wallabee** | 12 | Wayne Lukas | Joseph Talamo | 8-1 | 6.1% | 15.3-1 | 0.550 | 0.734 | 0.732 | -0.019 | ~~UL~~ | MED |
| 7 | **Emerging Market** | 15 | Chad Brown | Joel Rosario | 15-1 | 5.3% | 17.9-1 | 0.700 | 0.688 | 0.651 | +0.008 | -- | MED |
| 8 | **Desert Storm** | 2 | Bob Baffert | Mike Smith | 12-1 | 5.1% | 18.4-1 | 0.800 | 0.492 | 0.592 | -0.004 | -- | MED |
| 9 | **Royal Flush** | 14 | Michael McCarthy | Florent Geroux | 18-1 | 4.4% | 21.7-1 | 0.650 | 0.597 | 0.588 | +0.006 | -- | MED |
| 10 | **Silver Bullet** | 5 | Chad Brown | Flavien Prat | 25-1 | 4.3% | 22.0-1 | 0.650 | 0.488 | 0.711 | +0.016 | -- | MED |
| 11 | **Blue Thunder** | 7 | Mark Casse | Patrick Husbands | 15-1 | 3.6% | 26.4-1 | 0.700 | 0.509 | 0.237 | -0.009 | -- | MED |
| 12 | **Golden Gate** | 10 | John Shirreffs | Kent Desormeaux | 20-1 | 3.6% | 26.6-1 | 0.700 | 0.489 | 0.528 | +0.002 | -- | MED |
| 13 | **Lucky Strike** | 17 | Art Sherman | Manuel Franco | 22-1 | 3.3% | 29.6-1 | 0.550 | 0.481 | 0.523 | +0.001 | -- | MED |
| 14 | **Iron Will** | 3 | Steve Asmussen | Ricardo Albarado | 20-1 | 2.8% | 35.2-1 | 0.700 | 0.400 | 0.191 | -0.007 | -- | LOW! |
| 15 | **Storm Dancer** | 13 | Bill Mott | Dylan Davis | 35-1 | 2.3% | 43.0-1 | 0.800 | 0.232 | 0.167 | +0.003 | -- | LOW! |
| 16 | **Lone Star** | 19 | Steve Asmussen | Ricardo Santana Jr. | 28-1 | 2.2% | 44.1-1 | 0.700 | 0.299 | 0.207 | -0.003 | -- | LOW! |
| 17 | **Midnight Serenade** | 4 | Todd Pletcher | Tyler Gaffalione | 30-1 | 2.0% | 49.9-1 | 0.800 | 0.173 | 0.094 | -0.004 | -- | LOW! |
| 18 | **Phantom Rider** | 16 | Tom Amoss | Brian Hernandez Jr. | 40-1 | 1.9% | 53.1-1 | 0.800 | 0.109 | 0.015 | +0.001 | -- | LOW! |
| 19 | **Mountain King** | 20 | Ken McPeek | Joe Rocco Jr. | 45-1 | 1.7% | 56.7-1 | 0.800 | 0.077 | 0.013 | +0.002 | -- | LOW! |
| 20 | **Crimson Tide** | 8 | Larry Jones | Chris Landeros | 50-1 | 1.2% | 78.8-1 | 0.550 | 0.000 | 0.005 | -0.002 | -- | LOW! |

### Low-Confidence Entries

These horses have `dist_starts <= 1`; their distance_fit score is based on `stamina_index` alone (no race history at 1.25 miles).

| Horse | Post | Dist Starts | Additional Missing Flags |
|-------|------|-------------|--------------------------|
| Iron Will | 3 | 1 | dist_fit_single_start |
| Storm Dancer | 13 | 1 | dist_fit_single_start |
| Lone Star | 19 | 1 | dist_fit_single_start |
| Midnight Serenade | 4 | 1 | dist_fit_single_start |
| Phantom Rider | 16 | 1 | dist_fit_single_start |
| Mountain King | 20 | 1 | dist_fit_single_start |
| Crimson Tide | 8 | 1 | dist_fit_single_start |

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
| `speed_best_3` | 0.1000 | DEGRADED |
| `pace_fit_score` | 0.0975 | IMPLEMENTED |
| `distance_fit` | 0.0935 | DEGRADED |
| `speed_last` | 0.0875 | IMPLEMENTED |
| `surface_fit` | 0.0765 | DEGRADED |

### Calibration

| Parameter | Value |
|-----------|-------|
| Method | temperature-scaled softmax |
| Temperature | 2.81 |
| Calibration target | overround-adjusted morning line |
| Sum of win probabilities | 1.000000 |
| KL divergence vs market | 0.0155 |

### Model Limitations

> This baseline uses seed-aggregate features and has not been validated on historical Derby preps.
> Fair odds and value scores are **directional only**.
> The following features are unavailable until real historical data is loaded:
> race-by-race speed splits, bullet workout counts, trainer/jockey conditioned stats,
> Churchill Downs track form, post-position win bias, trip trouble flags.
>
> **Do not wager without manual audit of speed figures, trip notes, and trainer intent.**