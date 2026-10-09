# Paper trading

Records what the engine would have bet, settles each bet on the exact payoff in the Equibase chart, and reports
return and closing-line value. No money moves; the point is an honest answer to "does this make money after takeout?".

```
python -m training.paper_trading place --backfill     # replay history: each race decided at its last pre-cutoff run + price
python -m training.paper_trading place                # live: only races whose cut-off (post - 2 min) is still ahead
python -m training.paper_trading place --dry-run      # print the bets, write nothing
python -m training.paper_trading settle               # settle open bets from stored charts (ingest charts first)
python -m training.paper_trading report               # output/paper_trading/<stamp>_<policy>.md + .json
```

## The rules that keep it honest

| Rule | Why |
|---|---|
| Decide uses only the last stored score run and the last complete book capture, both at or before `post - 2 min` | what you could actually have known and acted on |
| Decide never reads a chart, a result, or `entries.scratch_flag` | those can be written after the race; a test deletes the result tables and checks identical bets |
| Replay and live give the same bet when live acts at the same moment | a test proves it |
| A capture must quote every runner on the card at decision time; two books disagreeing at one instant means no price | no partial or ambiguous market |
| Live mode refuses prices older than 20 minutes | a stale price is not a price you could take |
| First decision per (policy, race) stands; re-running changes nothing | no re-deciding after the model "changes its mind" |
| Settlement uses the chart's exact $2 win payoff: return = stake x payoff / 2 | pari-mutuel: the decision price is **not** the price paid |
| A bet settles only when the chart's reconciliation to the card is `MATCHED` | otherwise it is held with the reason in `note` |
| Late scratch: stake refunded. Dead heat: voided (payoff split not modelled) | never guessed |

## Default policy `edge_ev_v1`

Back at most one runner per race when model p x captured decimal - 1 >= 5%, model p - devigged market p >= 2.5 points,
decimal odds between 2.0 and 21.0, and the engine's low-confidence block is not set. Flat $2 stake (Kelly available:
quarter-Kelly capped at 2% of a $1,000 bankroll). A policy is identified by a hash of its parameters, so changing any
number creates a separate book instead of rewriting history.

## Report

* ROI with a bootstrap interval, hit rate against the model's own expected hit rate, and a wins z-score.
* **CLV**: captured decimal / final decimal - 1 on each bet. Positive on average means you beat the closing price; it
  converges much faster than ROI.
* Baselines settled on the same races and payoffs: back every runner, back the favourite. They show what takeout costs.
* Verdicts: `INSUFFICIENT_DATA` below 100 settled bets (50 for CLV). Below that the report says so at the top.

## Limits

* `implied_prob` is stored rounded to 6 decimals, so captured decimals carry ~1e-4 relative error.
* Price is the book's, not the pool's; the tote final is the chart's. CLV compares the two, which is the point.
* Only win bets. Dead heats are voided. Takeout and breakage are in the chart payoff, so they are already paid.

## Daily cycle

```
python -m training.daily_cycle                    # charts under data/raw/historical_results
python -m training.daily_cycle --root <folder> --no-populate
```
Run it after the last race of the day, with that day's `eqb_<TRK>_<date>_fullcard.pdf` files in the chart folder:
ingest new charts (stored ones are skipped) -> join each race to its pre-race card -> feed `race_results` for MATCHED
races (never overwrites) -> settle open paper bets -> write a report per policy to `output/paper_trading/` plus a
`<stamp>_cycle.json` log. Every step runs even if an earlier one found a problem. Exit 1 on an unreadable or invalid
chart, a card/chart disagreement, or a held bet; open bets still waiting for a chart are normal and exit 0.
Placing bets (`place`) is a separate, pre-race step.
