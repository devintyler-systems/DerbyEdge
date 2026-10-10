# Play-day checklist

One page. Goal on a play day: every race on the card at the tracks you watch is scored **before post**, with a captured
price, so it can be graded and paper-traded. Scoring only the races you would bet throws away most of the sample.

## Before the first race (per race, as it nears post)

1. **Build the bundle.** Paste DK Advanced + TwinSpires summary + DK Basic into one `.md`, named
   `TRK_Full_Race_Data_R7_10-8-26.md` (track, race number, date). Format: `docs/race_bundle_format.md`.
2. **Upload it in the app** (`streamlit run src/app/app.py`). Read the import message: a conflict between the three
   sections blocks the import and names the values; a track the registry cannot resolve is refused, not guessed.
3. **Timing matters.** The upload time is the price time. Do it **2 to 20 minutes before post**.
   - Later than post: the race imports flagged `LATE_CAPTURE`, with no market snapshot. It cannot be graded as pre-race.
   - Earlier than 20 minutes before post: the price is too old for `place` to use (it refuses a price older than 20 min).
4. **Score it:** `Build + Score now` on the race. The score run's timestamp must be before post.
5. **Place the paper bet** while at least 2 minutes remain before post:
   ```
   python -m training.paper_trading place
   ```
   It prints each bet with the price source and time, and the reasons any race was skipped
   (`STALE_PRICE`, `NO_VALID_PRICE`, `TOO_CLOSE_TO_POST_OR_PAST`, ...). One decision per race and policy; re-running changes nothing.
   Add `--dry-run` to preview.

If you also bet real money, that is separate and your call. The paper record is what you use to judge the engine.

## After the last race (same evening or next morning)

6. **Get the Equibase full-card charts** (`eqb_<TRK>_<date>_fullcard.pdf`) for each track played and put them under
   `data/raw/historical_results/YYYY/MM/DD/`.
7. **Run the cycle:**
   ```
   python -m training.daily_cycle
   ```
   It stores new charts, joins each race to its card, feeds `race_results`, settles paper bets and writes a report per policy.
   Exit 1 lists the problem: an unreadable or invalid chart, a card and chart that disagree (usually a wrong program number
   or horse name in the bundle), or a held bet. Fix the cause; re-running is safe.
8. **Check where you stand:**
   ```
   python -m training.status
   ```
   Scored / graded races and settled bets against the thresholds (30 races, 100 bets, 50 for closing-line value), with a projected date.

## Weekly

- `python -m training.walk_forward`: the engine against the morning line, the live market and a uniform guess. The
  closing tote line is hindsight; it is the bar to beat, not a fair comparison.
- Read the paper report's closing-line value first. It is the earliest honest signal.

## Rules while evidence builds

- **Do not change the model or the paper policy after reading results.** Each change spends the sample. Batch changes, note
  the date, and judge a change only on races scored after it.
- Run `python scripts/stage_downloads.py` first for any raw DK / TwinSpires export that lands in Downloads
  (`docs/SOP_downloads_staging.md`). Upload from a fixed folder, never from Downloads.
- If a result looks too good in the first few weeks, assume it is noise until `status` says the thresholds are met.

## Not yet verified

The Streamlit upload and scoring buttons were not driven end to end in the build environment; steps 2 to 4 follow the
code, not a recorded session. Run one race through steps 1 to 5 and 6 to 8 on your first play day and report any message
that does not match this page.
