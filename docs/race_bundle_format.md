# One-file race bundle

Paste the three tabs for one race into a single `.md` and upload it as the primary race card.
Name it as today, with the race date: `CT_Full_Race_Data_R7_10-8-26.md`.

| Tab | Recognised by header cell | Gives |
|---|---|---|
| DK Advanced | `Sire / Dam` | track, race #, post time, runners, PPs, workouts |
| TwinSpires summary | `STYLE` | run style, speed/class/power, current odds, ML |
| DK Basic | `MED/WT/EQP` | assigned weight (`L122` = Lasix, 122 lb), jockey, trainer |

Order does not matter; LF or CRLF both work. Nothing needs to be typed into the file.

* Track, race number, post time: DK Advanced header. Date: filename. Capture time: when the app received the file.
* Post time is converted to UTC with the track's registered timezone (`src/derbyedge/tracks.py`).
  A countdown post ("9 MTP") is an estimate (capture time + minutes) and is labelled `MTP_ESTIMATE`.
* The sections are cross-checked per program number (horse, morning line, scratch status, jockey, trainer, weight).
  Any disagreement, missing, or duplicated section blocks the import and names the conflicting values.
* Received at/after post: the card imports but is flagged `LATE_CAPTURE` and gets no market snapshot or pace features.
* Scratches, also-eligibles (`AE…`) and main-track-only (`MTO…`) entries are kept but are not starters;
  `field_size` and runner counts are starters only. The `AE`/`MTO` label spelling is UNVERIFIED against a real DK export.

Code: `src/ingest/race_bundle.py`, `src/ingest/draftkings_basic_grid.py`, `src/services/race_bundle_intake.py`.
Tests: `tests/test_race_bundle.py`, `tests/test_entry_model.py`.
