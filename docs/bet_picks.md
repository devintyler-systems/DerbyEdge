# Pre-threshold bet-type picks

The Race Board lists up to 3 WIN / PLACE / SHOW picks (combinations such as `WIN+SHOW`) for the scored race, labelled
**PRE-THRESHOLD: not validated evidence**. They are derived from the stored win probabilities, valid or diagnostic.

- Place and show probabilities: Harville from the win probabilities (place sums to 2, show to 3). Harville overrates
  favourites and underrates longshots at place/show; the picks lean toward longshots, so the bias is conservative but unverified.
- Market side: the same transform of the morning-line probabilities (no live place/show pool is captured).
- A bet type is picked when the model probability clears a floor AND beats the morning-line-implied one by a margin
  (WIN 8% / +4 pts, PLACE 30% / +5, SHOW 35% / +5). Untuned defaults; changing them changes `PICKS_VERSION`.
- Each pick shows a break-even payout per $2 (`2 / probability`): the lowest price worth taking on the model's numbers.
  The board shows model probability, break-even payout and the morning-line price (with the ML-implied %) side by side for the
  owner's own judgment, each labelled unvalidated. The app computes no stake and calls no pick a recommended wager.
- Picks are stored in `bet_picks` only when the run was before the scheduled post. The results page grades stored picks
  hit/miss (WIN 1st, PLACE top 2, SHOW top 3; scratched = VOID). Races with no stored picks show a RETROSPECTIVE re-derivation
  that is never counted.
- Track record under the picks (per bet type, newest engine version only): picks graded, hit rate vs the model's own mean
  probability (Wilson CI), return per $2 at chart payoffs with a 95% CI over legs whose race has a stored chart; INSUFFICIENT DATA below 30.
- Longshot warning when a pick's model probability is more than 2x the morning-line-implied one.
- No verdict below 30 graded picks per bet type. Return is computed at the stored Equibase chart payoffs (Results Import itself does not store them).
- Results page: BET tags the gate withheld on the Race Board are no longer graded (`evaluate_score_run(bet_tags_valid=False)`).
- Source: `ENGINE_FORECAST` only. When the engine collapsed to the morning line (no independent probabilities) no pick is made
  and the board says "MORNING-LINE BASELINE". Stored rows carry `engine_version` and `source`; `bet_picks` is a separate table, so
  `training.status` and walk-forward never count it.
- Scope: only the uncalibrated seed baseline the gate blocks (`picks_allowed`).
