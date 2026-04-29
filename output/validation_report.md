# DerbyEdge ETL Validation Report

**Generated**: 2026-04-29 08:42 UTC  
**Source**   : `C:\Projects\derbyedge-engine\data\seeds\derby_2026_field.csv`  
**Race**     : 2026 Kentucky Derby (G1) — Churchill Downs, 2026-05-02  

## Load Summary

| Field | Count |
|-------|-------|
| Source rows | 20 |
| Horses (new) | 0 |
| Horses (pre-existing) | 20 |
| Entries (new) | 0 |
| Entries (pre-existing) | 20 |
| Odds snapshots | 0 |
| Trainers (new) | 0 |
| Jockeys (new) | 0 |
| Owners (new) | 0 |

## Validation Status

**0 failures, 0 warnings**

### Source checks

| | Check | Detail |
|--|-------|--------|
| ✓ **PASS** | No duplicate horse names in source |  |
| ✓ **PASS** | No duplicate post positions in source |  |
| ✓ **PASS** | No duplicate jockeys in same race |  |
| ✓ **PASS** | No null post_position |  |
| ✓ **PASS** | No null morning_line_odds |  |
| ✓ **PASS** | No missing trainer |  |
| ✓ **PASS** | No missing jockey |  |
| ✓ **PASS** | All morning line odds > 0 | range 4.0–50.0 |
| ✓ **PASS** | All pace_style values valid (front/presser/stalker/closer) |  |
| ✓ **PASS** | Source field size = 20 |  |

### Database checks

| | Check | Detail |
|--|-------|--------|
| ✓ **PASS** | Loaded entries = 20 |  |
| ✓ **PASS** | All entries have trainer_id |  |
| ✓ **PASS** | All entries have jockey_id |  |
| ✓ **PASS** | Post positions 1-20 complete, no gaps |  |
| ✓ **PASS** | Morning line overround in expected range (1.00-1.35) | Sum of implied probs = 1.2836 |
| ✓ **PASS** | Odds snapshots loaded (morning line) | 20 snapshots for 20 entries |
| i **INFO** | horse_starts populated | 0 rows — historical result data required for v_horse_last_5 and v_connections_180 |
| i **INFO** | workouts (real, synthetic=0) populated | 0 real rows, 0 synthetic rows — real workout records required for v_workout_30; aggregate counts are in entries.workouts_30 |
| i **INFO** | track_bias populated | 0 rows — Churchill Downs 2026 bias requires manual entry |
| ✓ **PASS** | Live entries view | 20 rows |
| i **INFO** | Last-5 starts view | 0 rows (0 expected until historical data loaded) |
| i **INFO** | Workout-30 view | 0 rows (0 expected until historical data loaded) |
| i **INFO** | Connections-180 view | 0 rows (0 expected until historical data loaded) |

## Missing Data — Expected Gaps for Seed-Only Install

The Derby 2026 seed CSV does not contain the following data.
These gaps are **expected** and explicitly documented here.
They will be filled when real historical data is imported.

| Table / View | Status | Notes |
|--------------|--------|-------|
| `horse_starts` | Empty | No individual race results in seed CSV |
| `workouts` (synthetic=0) | Empty | Aggregate count stored in `entries.workouts_30` |
| `track_bias` | Empty | Churchill Downs 2026 bias requires manual entry |
| `trip_flags` | Empty | Post-race data; not available pre-race |
| `v_horse_last_5` | 0 rows | Requires `horse_starts` |
| `v_workout_30` | 0 rows | Requires real workout records |
| `v_connections_180` | 0 rows | Requires `horse_starts` |

### What IS available from the seed

| Column | Source | Table |
|--------|--------|-------|
| `career_starts/wins/places/shows` | Derby seed CSV | `entries` |
| `best_speed_fig`, `last_speed_fig`, `avg_speed_fig`, `beyer_fig` | Derby seed CSV | `entries` |
| `dirt_starts/wins`, `dist_starts/wins` | Derby seed CSV | `entries` |
| `workouts_30` (aggregate count) | Derby seed CSV | `entries` |
| `stamina_index`, `gate_class`, `pace_style` | Derby seed CSV | `entries` |
| Morning line odds snapshot | Derived from `morning_line_odds` | `odds_snapshots` |

### Partial null columns in source

- career_starts: 70% null in source
- career_wins: 70% null in source
- career_places: 70% null in source
- career_shows: 70% null in source
- career_earnings: 70% null in source
- last_race_days_ago: 70% null in source
- last_race_finish: 70% null in source
- last_race_speed_figure: 70% null in source
- best_speed_figure: 70% null in source
- avg_speed_figure: 70% null in source
- beyer_speed_figure: 70% null in source
- dirt_starts: 70% null in source
- dirt_wins: 70% null in source
- dist_starts: 70% null in source
- dist_wins: 70% null in source
- wet_starts: 70% null in source
- wet_wins: 70% null in source
- workouts_past_30: 70% null in source
- gate_class: 70% null in source
- stamina_index: 70% null in source
- pace_style: 70% null in source