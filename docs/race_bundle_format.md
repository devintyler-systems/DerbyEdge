# One-file race bundle

Paste the three tabs for one race into a single `.md` and upload it as the primary race card.
Name it as today, with the race date: `CT_Full_Race_Data_R7_10-8-26.md`.

| Tab | Recognised by header cell | Gives |
|---|---|---|
| DK Advanced | `Sire / Dam` | track, race #, post time, runners, PPs, workouts |
| TwinSpires summary | `STYLE` | run style, speed/class/power, current odds, ML |
| DK Basic | `MED/WT/EQP` | assigned weight (`L122` = Lasix, 122 lb), jockey, trainer |

Order does not matter; LF or CRLF both work. Nothing needs to be typed into the file.

An optional fourth block, the TwinSpires **RACE STATS** panel, may be pasted anywhere in the file (see below).

* Track, race number, post time: DK Advanced header. Date: filename. Capture time: when the app received the file.
* Post time is converted to UTC with the track's registered timezone (`src/derbyedge/tracks.py`).
  A countdown post ("9 MTP") is an estimate (capture time + minutes) and is labelled `MTP_ESTIMATE`; a class line with no `$` ("ALLOWANCE")
  right after it is not part of the post display.
* The sections are cross-checked per program number (horse, morning line, scratch status, jockey, trainer, weight).
  Any disagreement, missing, or duplicated section blocks the import and names the conflicting values.
* Received at/after post: the card imports but is flagged `LATE_CAPTURE` and gets no market snapshot or pace features.
* Scratches, also-eligibles (`AE…`) and main-track-only (`MTO…`) entries are kept but are not starters;
  `field_size` and runner counts are starters only. The `AE`/`MTO` label spelling is UNVERIFIED against a real DK export.

## Optional: TwinSpires SPEED, CLASS and PACE tabs

Three more TwinSpires program tabs (BRIS figures with ranks) can be pasted into the same file. Sample:
`tests/fixtures/TS_Tabs_MNR_R5_10-12-26.md`. **They are stored only; scoring does not read them.** None is required.

**How to paste (all in one sitting, before post).** Order does not matter to the app, because each tab is recognised by its column
header row; use this order so none is forgotten: DK Advanced, DK Basic, TwinSpires summary (the existing tab), **Speed**, **Class**,
**Pace**, then RACE STATS if you have it. For each of the three new tabs, open it on TwinSpires, select the table from its `#` header
row down to the last runner (scratches included), copy, and paste it below the previous tab. Do **not** type labels such as `(SPEED)`
and do not trim the header row: it is how the app tells the tabs apart. TwinSpires auto-advances to the next race, so confirm each
tab still shows the same race before copying (a tab from another race blocks the import and names the horse that does not match).

| Tab | Recognised by header cells | Printed columns kept (value and rank each) |
|---|---|---|
| Speed | `Average Distance`, `best speed` | run style (+ points 0-8), `Average Speed`, `Average Distance`, `best speed` |
| Class | `prime power`, `last class` | `days off`, `prime power`, `last class`, `Average Class` |
| Pace | `early pace 1` | run style (+ points), `early pace 1`, `early pace 2`, `late pace` |

* Strict, like RACE STATS: the header row must match literally and each runner must have exactly the expected cells. A malformed tab
  **blocks the import** with `TwinSpires Speed tab is malformed (remove the tab to import without it): ...` naming the program and the
  cell. Each tab is also cross-checked per program against the DK card (horse, morning line, scratch status, program set); a mismatch
  blocks the import. Two copies of the same tab is an error. To import without a tab, delete it from the file.
* Values are stored under their exact printed column names with **no interpretation**: TwinSpires' help screens define "Average Speed"
  and "Average Class" inconsistently, so which reading predicts is for outcome data to decide. A printed `-` is stored as null, never 0.
  Run style is kept as printed (`NA 0`, `E/P 2`): `NA` runners (first-timers, no style) are stored, not dropped.
* The live ODDS cell is **not** parsed (the tabs are captured at different moments, so it disagrees between them); the morning line is kept.
* Stored in `twinspires_tab_stats` (created on first use): `card_id`, `tab_kind`, `captured_at`, `late_capture`, the race distance and its
  `sprint`/`route` category (E1/E2/LP are measured to different calls in each, so never pool across them), the tab text as pasted,
  `parsed_json`, `parser_version` (`twinspires_tabs_v1`). Re-importing an identical tab keeps the first row and its capture time.
* Caution when using them later: **Prime Power already combines speed, class, pace, form and weight**, so never put it in one model beside
  those components. BRIS speed is not the Beyer scale and must not be mapped onto `speed_last` / `beyer_last`.
* Code: `src/ingest/twinspires_tabs.py`; tests: `tests/test_twinspires_tabs.py`. UNVERIFIED beyond the MNR R5 sample: other tracks, turf,
  and rows with extra editorial tags.

**Profit Line.** The existing TwinSpires summary tab's `PL` cell is now parsed (`profit_line` as printed, `profit_line_decimal` through the
shared odds math: `7/2`, `Even`, `20`; `-` is null) and stored as the `twinspires_profit_line` source candidate. It is BRIS's
computer-modelled fair odds: an external benchmark for shadow comparison, not a market price and not scored.

## Optional: TwinSpires RACE STATS block

Paste the panel that starts with a line reading exactly `RACE STATS`. Sample: `tests/fixtures/TS_RaceStats_BEL_R5_10-9-26.md`.
It is captured now because it cannot be reconstructed as-of later. **It is stored only; scoring does not read it.**

* Not required: a bundle without it imports exactly as before.
* Parsed fields (`src/ingest/twinspires_race_stats.py`, `parser_version` `twinspires_race_stats_v1`):
  * header: track, the descriptive lines as printed, **pars** (`E1`, `E2`, `LP`, `SPD`);
  * **race-type stats** for the last N years: races, favourite win % / in-the-money win % / $2 ROI, average field size,
    median $2 win payoff, % winners under 5/1, 5/1 to 10/1, 10/1 and up;
  * **track bias** for each window printed (`Meet`, `Week`), with the surface, distance, MM/DD period as printed (the
    source gives no year) and the **sample size** (`races`): % wire, speed bias %, winner average beaten lengths at the 1st
    and 2nd call, run-style impact values and % won (`E`, `E/P`, `P`, `S`), **post bias** impact values and average win %
    (`RAIL`, `1-3`, `4-7`, `8+`).
* Strict on purpose: column labels must match the sample literally and every value must be a well-formed number.
  A present but malformed block **blocks the import** with `RACE STATS block is malformed: ...` naming the expected labels
  or the offending text, never a silent partial read. Two blocks in one file is also an error.
  The block's own header (track, distance, dirt/turf) is also checked against the DK card: a block from another race blocks the
  import with `RACE STATS block looks like a different race ... (distance '6 1/2 F' vs card '1 1/16 M')`. Fields that do not parse on
  both sides are skipped, never a false mismatch. To import without the
  block, delete it from the file.
* Stored in `twinspires_race_stats` (created on first use): `card_id`, `captured_at` (when the app received the bundle),
  the block text as pasted (`raw_text`), `parsed_json`, `parser_version`, `bundle_sha256`. Re-importing an identical block
  keeps the first row and its capture time; a changed block is kept as a new row.
* UNVERIFIED beyond the one BEL sample: other tracks, turf, stakes races, or a window with no races (placeholder cells such
  as `-` or `N/A`) may print differently and would fail the strict parse until the new layout is seen.
* Track-bias samples are small (the sample has 6 and 3 races): do not weight them until pooled.

After a valid bundle import the app builds features and scores the card automatically, once per uploaded file and card (`src/app/auto_score.py`);
the result is shown under the import message, and a failure never undoes the import.

Code: `src/ingest/race_bundle.py`, `src/ingest/draftkings_basic_grid.py`, `src/services/race_bundle_intake.py`.
DK Basic weight cell: `L122` = Lasix + 122 lb; a trailing equipment code is accepted and kept (`L122bOn` = blinkers on, informational,
not cross-checked). Anything else in that cell still fails the whole grid as misaligned.

Tests: `tests/test_race_bundle.py`, `tests/test_entry_model.py`, `tests/test_twinspires_race_stats.py`.
