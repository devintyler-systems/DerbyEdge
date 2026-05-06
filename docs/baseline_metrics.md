# Baseline Model — v0.2

Trained: 2,119 rows, 315 wins (14.9%). Date range 2019-04-18 → 2023-12-03.

## Walk-forward backtest (quarterly)

| fold_id | test_start | test_end | n_train | n_test | log_loss | brier | auc | ece_10bin | base_rate |
|---|---|---|---|---|---|---|---|---|---|
| 8 | 2022-07-01 | 2022-09-30 | 357 | 318 | 0.4192 | 0.1265 | 0.6512 | 0.0501 | 0.1509 |
| 9 | 2022-10-01 | 2022-12-31 | 675 | 494 | 0.4523 | 0.1309 | 0.5793 | 0.0728 | 0.1498 |
| 10 | 2023-01-01 | 2023-03-31 | 1169 | 187 | 0.2986 | 0.0845 | 0.7173 | 0.0530 | 0.0963 |
| 11 | 2023-04-01 | 2023-06-29 | 1356 | 221 | 0.3937 | 0.1126 | 0.5909 | 0.0531 | 0.1267 |
| 12 | 2023-07-01 | 2023-09-30 | 1577 | 338 | 0.4482 | 0.1317 | 0.6281 | 0.0656 | 0.1568 |
| 13 | 2023-10-01 | 2023-12-03 | 1915 | 204 | 0.3912 | 0.1160 | 0.6910 | 0.0646 | 0.1422 |

## Fold averages

- **n_folds**: 6
- **n_folds_valid_auc**: 6
- **mean_log_loss**: 0.4005240402355105
- **mean_brier**: 0.11702449936481123
- **mean_auc**: 0.6429613973954281
- **mean_ece**: 0.05988567226693651

## What the metrics mean

- **log_loss** lower is better. Baseline (predict base rate): 0.4204
- **brier** lower is better. Baseline (predict base rate): 0.1266
- **auc** discrimination. 0.50 = random; 0.70+ = useful for racing.
- **ece_10bin** expected calibration error across 10 quantile bins. <0.05 is well-calibrated.

## Honest caveats

- Training corpus is **per-runner PP rows**, not full race shells. Within-race calibration only kicks in at scoring time via softmax over today's field.
- 2,459 rows is small for racing ML. Treat metrics as plumbing validation, not bankable signal.
- Surface-conditioned, but no class-distance interaction yet, no jockey/trainer effects.
- ROI columns are exploratory because the per-runner odds in PPs are post-race — this is the one place we deliberately use closing odds for cost-of-betting estimation.