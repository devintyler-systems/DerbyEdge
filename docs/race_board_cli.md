# Headless Race Board (away from the user's PC)

`python scripts/race_board_cli.py <bundle.md> [--captured-at ISO]` runs one race bundle through the app's own code and prints the
Race Board as text: import validation, scoring through the eligibility gate, the diagnostic table, and the pre-threshold picks.
A Claude session uses it when the user sends bundles from a phone.

* Same code as the app: `dispatch_primary_race_card_import`, `persist_validated_draftkings_markdown`, `persist_race_bundle_extras`,
  `scripts/build_features.py`, `scripts/score.py`, `contain_ineligible_board`, `bet_picks`. Only the board SQL is a copy of
  `load_board` in `src/app/app.py`: change both together.
* Throwaway database (`DERBYEDGE_DB`, a temp file). It refuses `db/derbyedge.db`. So **nothing is saved to the user's database**:
  the run is not evidence (not in `training.status`, walk-forward or stored picks), has no track-record line, and a later upload of the same
  file is a late capture. Say so every time.
* Output rules: Win %, fair odds, edge and bet tags stay withheld (labelled DIAGNOSTIC, not valid for betting); picks carry the
  PRE-THRESHOLD label, break-even payout per $2 and longshot warnings; no stake is computed. Do not quote the raw `score.py` printout
  (it shows "Bet candidates" before the gate).
* Capture time defaults to now (the as-of). A bundle received at/after post prints `LATE CAPTURE` and is for reading only.
* Exit codes: 0 board, 2 import blocked (named reasons printed) or refused path, 3 not score-eligible, 4 build/score failed.
* The user can paste raw text instead of attaching a file: write it to `TRK_Full_Race_Data_R#_M-D-YY.md` (the filename carries the date)
  with a newline between tabs.

Tests: `tests/test_race_board_cli.py`.
