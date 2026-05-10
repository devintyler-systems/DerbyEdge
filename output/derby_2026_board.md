# DerbyEdge Engine — 2026 Kentucky Derby Board

## Board Summary

| Field | Value |
|-------|-------|
| Model type | `seed_only_baseline` |
| Version | `1.0.0-seed-only` |
| Score timestamp | 2026-05-10T01:10:21Z |
| Model ID | 30 |
| Race | 2026 Kentucky Derby (G1) · Churchill Downs · 2026-05-02 |
| Total horses | 10 |
| Bet-tagged | 0 (none) |
| Underlay-tagged | 4 (General Issue, Spurs Up, Head Lad, Sisyphus) |
| Low-conf BET blocked | 2 (Iceteca, Blameitonthefun) |
| Top win probability | General Issue 16.8% (fair 4.9-1) |
| Top value score | Blameitonthefun +0.034 (neutral) |
| Kendall tau vs market | 1.0000 |
| Mean abs edge | 0.0189 |
| Low-confidence entries | 10 of 10 (dist_starts <= 1; distance_fit unreliable) |

---

## Race Card

**Bet thresholds:** BET >= +0.025  |  UNDERLAY < -0.015  |  NEUTRAL otherwise

| Rank | Horse | Post | Trainer | Jockey | ML | Win% | Fair | PaceFit | Form | SuDist | Edge | Tag | Conf |
|------|-------|------|---------|--------|----|------|------|---------|------|--------|------|-----|------|
| 1 | **General Issue** | 3 | Ronney W. Brown | Warren Ebow Iii | 4-1 | 16.8% | 4.9-1 | 0.650 | 0.500 | 0.500 | -0.029 | ~~UL~~ | LOW! |
| 2 | **Spurs Up** | 5 | Michael E. Jones, Jr. | Jeiron Barbosa | 4-1 | 13.6% | 6.3-1 | 0.650 | 0.500 | 0.500 | -0.025 | ~~UL~~ | LOW! |
| 3 | **Head Lad** | 7 | Ronney W. Brown | Moises Santaella | 5-1 | 12.6% | 6.9-1 | 0.650 | 0.500 | 0.500 | -0.022 | ~~UL~~ | LOW! |
| 4 | **Sisyphus** | 4 | Adam King | Christian Hiraldo | 6-1 | 11.8% | 7.5-1 | 0.650 | 0.500 | 0.500 | -0.019 | ~~UL~~ | LOW! |
| 5 | **English Painter** | 9 | None | Gerald Almodovar | 9-1 | 8.9% | 10.3-1 | 0.650 | 0.500 | 0.500 | +0.000 | -- | LOW! |
| 6 | **Ascendance** | 2 | Jesus Rodriguez | Jose Mauricio | 11-1 | 8.1% | 11.3-1 | 0.650 | 0.500 | 0.500 | +0.007 | -- | LOW! |
| 7 | **Gran Andrews** | 1 | Tyler S. Shanley | Reshawn Latchman | 13-1 | 7.7% | 12.1-1 | 0.650 | 0.500 | 0.500 | +0.013 | -- | LOW! |
| 8 | **Rhumjar** | 10 | Sherry L. Jackson | Walter Cullum | 13-1 | 7.7% | 12.1-1 | 0.650 | 0.500 | 0.500 | +0.013 | -- | LOW! |
| 9 | **Iceteca** | 8 | Timothy Shanley | Denis Vicente Araujo | 21-1 | 6.7% | 14.0-1 | 0.650 | 0.500 | 0.500 | +0.026 | --[B] | LOW! |
| 10 | **Blameitonthefun** | 6 | Timothy Shanley | Joe Stokes | 31-1 | 6.2% | 15.1-1 | 0.650 | 0.500 | 0.500 | +0.034 | --[B] | LOW! |

### Low-Confidence Entries

These horses have `dist_starts <= 1`; their distance_fit score is based on `stamina_index` alone (no race history at 1.25 miles).

| Horse | Post | Dist Starts | Additional Missing Flags |
|-------|------|-------------|--------------------------|
| General Issue | 3 | 0 | dist_fit_single_start |
| Spurs Up | 5 | 0 | dist_fit_single_start |
| Head Lad | 7 | 0 | dist_fit_single_start |
| Sisyphus | 4 | 0 | dist_fit_single_start |
| English Painter | 9 | 0 | dist_fit_single_start |
| Ascendance | 2 | 0 | dist_fit_single_start |
| Gran Andrews | 1 | 0 | dist_fit_single_start |
| Rhumjar | 10 | 0 | dist_fit_single_start |
| Iceteca | 8 | 0 | dist_fit_single_start |
| Blameitonthefun | 6 | 0 | dist_fit_single_start |

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
| Temperature | 20.0 |
| Calibration target | overround-adjusted morning line |
| Sum of win probabilities | 1.000000 |
| KL divergence vs market | 0.0330 |

### Model Limitations

> This baseline uses seed-aggregate features and has not been validated on historical Derby preps.
> Fair odds and value scores are **directional only**.
> The following features are unavailable until real historical data is loaded:
> race-by-race speed splits, bullet workout counts, trainer/jockey conditioned stats,
> Churchill Downs track form, post-position win bias, trip trouble flags.
>
> **Do not wager without manual audit of speed figures, trip notes, and trainer intent.**

### Low-Confidence BET Guardrail

> Low-confidence entries (`conf == LOW`) with a raw edge ≥ +0.025 are **NOT** auto-tagged BET.
> Their apparent edge comes from the odds-floor vs market probability gap, not from model signal.
> These entries are downgraded to `neutral` and flagged with `low_conf_bet_block = 1`.
> Tag column shows `--[B]` for blocked entries.
> To elevate after manual review, override the bet_tag in the database directly.