# Walk-forward evaluation

`python -m training.walk_forward` (read-only) grades the win probabilities the engine **actually produced** against
official results, in date order, next to the benchmarks a bettor could have used. No model is retrained: every score
run is stored with its timestamp, so each race is graded with the forecast that existed before post.

```powershell
python -m training.walk_forward                                  # everything gradable
python -m training.walk_forward --since 2026-09-01 --track CD --min-races 30
```
Output (under `output/walk_forward/<UTC time>/`): `report.md`, `summary.json`, `per_race.csv`. Exit status 1 if fewer than
`--min-races` races could be graded.

## What counts as a gradable race
Official result with exactly one winner, at least two starters, a score run made **before the scheduled post**. Races with
no scheduled post time cannot prove "before post" and are excluded unless `--include-unproven` (they are then flagged).
Dead heats, races with no winner, never-scored races and runs made after post are excluded, and every exclusion is
counted with its reason in the report.

## Forecasts compared (all renormalised over the horses that started, the same way)
| name | source |
|---|---|
| `model_board` | `entry_scores.win_probability`, the probability the board showed |
| `model_pre_market` | `p_model_pre_market`, the model before any market input (when stored) |
| `morning_line` | morning-line odds, overround removed (the default reference) |
| `live_market` | last complete, valid pre-post book capture, overround removed |
| `closing_tote` | final odds, overround removed. **Hindsight**: not known at decision time; a yardstick, never a forecast |
| `uniform` | 1/n |

## Metrics
Per race first, then averaged over races: log loss `-ln p(winner)` (floored at 1e-6), Brier (sum over the field), top-1,
mean winner rank. Starter-level calibration table and ECE. Paired comparison against the reference on the races where
both exist: mean difference with a deterministic bootstrap 95% interval (negative = better than the reference), a verdict
(`BETTER_THAN_REFERENCE`, `WORSE_THAN_REFERENCE`, `NOT_DISTINGUISHABLE`, or `INSUFFICIENT_DATA` below 30 races), and the
number of races needed to detect a 1% log-loss gain at the observed noise. Segments by race family and field size flag
thin groups (<10 races). The cumulative log-loss curve shows how the comparison evolved over time.

## Retrainable models
`walk_forward_oof(graded, fit_predict, ...)` produces out-of-fold forecasts from an expanding window: training races are
strictly earlier than the test block minus an embargo, `fit_predict` sees results for training races only and receives
test races without a winner. There is no random splitting anywhere.

## Reading it honestly
`closing_tote` shows how good the market gets by the end; beating `morning_line` is a low bar, beating `live_market` is the
real one. With a few dozen races the interval is wide and the verdict will usually be `NOT_DISTINGUISHABLE`; the model's
own promotion gate asks for 500 completed races. `closing_tote` uses `race_results.official_odds_decimal`, which is only
decimal-including-stake for rows written by the chart ingest; rows from older CSV uploads may be in other units.
