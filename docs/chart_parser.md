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
