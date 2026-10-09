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
  A countdown post ("9 MTP") is an estimate (capture time + minutes) and is labelled `MTP_ESTIMATE`.
* The sections are cross-checked per program number (horse, morning line, scratch status, jockey, trainer, weight).
  Any disagreement, missing, or duplicated section blocks the import and names the conflicting values.
* Received at/after post: the card imports but is flagged `LATE_CAPTURE` and gets no market snapshot or pace features.
* Scratches, also-eligibles (`AE…`) and main-track-only (`MTO…`) entries are kept but are not starters;
  `field_size` and runner counts are starters only. The `AE`/`MTO` label spelling is UNVERIFIED against a real DK export.

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
  or the offending text, never a silent partial read. Two blocks in one file is also an error. To import without the
  block, delete it from the file.
* Stored in `twinspires_race_stats` (created on first use): `card_id`, `captured_at` (when the app received the bundle),
  the block text as pasted (`raw_text`), `parsed_json`, `parser_version`, `bundle_sha256`. Re-importing an identical block
  keeps the first row and its capture time; a changed block is kept as a new row.
* UNVERIFIED beyond the one BEL sample: other tracks, turf, stakes races, or a window with no races (placeholder cells such
  as `-` or `N/A`) may print differently and would fail the strict parse until the new layout is seen.
* Track-bias samples are small (the sample has 6 and 3 races): do not weight them until pooled.

Code: `src/ingest/race_bundle.py`, `src/ingest/draftkings_basic_grid.py`, `src/services/race_bundle_intake.py`.
Tests: `tests/test_race_bundle.py`, `tests/test_entry_model.py`, `tests/test_twinspires_race_stats.py`.
