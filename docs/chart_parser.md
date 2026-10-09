# Equibase full-card chart parser (P1 of the results ingest)

`src/ingest/equibase_chart.py` turns a retained `eqb_<TRK>_<date>_fullcard.pdf` into one record per race. It reads
and validates only; it writes no database and makes nothing eligible for training. Charts are downloaded by hand
(Equibase's terms prohibit automated access) into `data/raw/historical_results/YYYY/MM/DD/`.

```python
from src.ingest.equibase_chart import parse_chart_pdf
card = parse_chart_pdf("data/raw/historical_results/2026/04/25/eqb_CD_2026-04-25_fullcard.pdf")
card.valid, [r.race_key for r in card.races]          # True, ['CD|2026-04-25|R1', ...]
```

Per race: conditions, distance (words -> furlongs), surface, purse, weather/track, off time, start comment, fractional
and final times, finish order with final odds, win/place/show payoffs, the exotic-pool rows, scratches with reasons
(including `Also-Eligible`), trainer and owner per program, and stewards' changes.

## Units and orders
* `odds_to_one` is Equibase's printed odds (4.79 = 4.79-1); `decimal_odds = odds_to_one + 1 = payoff / 2`.
* `finish_order` is the **official** order; `run_order` is the order they crossed the line. They differ only after a
  disqualification (`disqualified`, `placed_from`, flag `ORDER_CHANGED_BY_STEWARDS`). The payoff table follows the
  official order.
* Mid-race position calls are glued to lengths in the source ("131/4"), so they are kept raw in `calls_raw` and not
  interpreted. Do not model on them.

## Self-checks (a race with any problem is `valid == False` and must not be ingested)
winner row == the chart's `Winner:` line; winner odds == `win payoff / 2 - 1` to a cent; payoff table lists the first
three official finishers in order; unique programs and a trainer for each; distance, surface, final time, fractional
times and track condition understood; track resolves in the registry; no duplicate or missing race numbers on the card.

## Not handled yet (they are flagged or reported, never guessed)
Dead heats (flag `DEAD_HEAT`; the payoff identity is skipped), cancelled or moved-off-turf races, quarter-horse and
harness charts, exotic rows with unusual wording (kept as `raw`, `parsed=False`).

Gate: `python -m pytest tests/test_chart_parser.py -q` (28 races / 3 cards).

# Storing charts and joining them to cards (phase 2)

`src/services/chart_results_intake.py`, CLI `scripts/ingest_results_charts.py`.

```powershell
python scripts/ingest_results_charts.py --root data/raw/historical_results --check      # validate + compare, writes nothing
python scripts/ingest_results_charts.py --root data/raw/historical_results              # store valid races, record the card join
python scripts/ingest_results_charts.py --root data/raw/historical_results --populate   # also feed race_results for grading
```

Tables: `result_sources` (file SHA-256, parser version, raw bytes), `result_races` (one `is_current` per race key;
a corrected chart supersedes and keeps the old one), `result_starters`, `result_scratches`, `result_payoffs`,
`result_card_reconciliations`. Only races that pass every self-check are stored; a card with a structural fault
(duplicate or missing race number) is rejected whole. Re-ingesting a file is a no-op.

Join to the pre-race card is by track code, date, race number and **program number**:

| Finding | Meaning |
|---|---|
| error `CARD_RUNNER_NOT_IN_CHART`, `CHART_STARTER_NOT_ON_CARD`, `CARD_SCRATCHED_BUT_RAN`, `NAME_MISMATCH` | card and chart describe different fields; the join is not trusted, nothing is populated |
| warning `LATE_SCRATCH` | active on the pre-race card, scratched in the chart (the model scored a non-runner) |
| warning `JOCKEY_CHANGE`, `TRAINER_CHANGE`, `WEIGHT_CHANGE`, `POST_MISMATCH` | changed after the card was captured |
| warning `SURFACE_CHANGE`, `DISTANCE_CHANGE` | the race was moved; features built for the card's surface are for the wrong surface |

`--populate` writes `race_results` only for a MATCHED race and never overwrites differing rows without `--replace`.
`official_odds_decimal` is decimal including the stake (odds-to-one + 1); `finish_position` / `official_finish` are the
official place (a horse placed down by the stewards keeps its official place with `is_disqualified = 1`);
`earned_purse` is the purse for that official place. Late scratches get an `is_scratched = 1` row; the card's own
`scratch_flag` is left alone so the pre-race snapshot is not rewritten.
