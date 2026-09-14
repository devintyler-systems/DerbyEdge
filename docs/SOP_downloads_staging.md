# SOP: Downloads Staging Discipline

Root cause this fixes: a stale/incomplete export file sitting at the
Downloads path got silently overwritten by the real file mid-workflow,
after ingest had already read the stale version (card_id=74 incident,
2026-09 session). Fix is procedural, not code: never let ingest read from
Downloads at all.

## Checklist — run every time, before touching any ingest command

1. Export DK Advanced markdown, DK Basic-tab XLSX, and TwinSpires summary
   from their respective sources.
2. Immediately run:
   ```
   python scripts/stage_downloads.py
   ```
   Do this before opening any file, before running any ingest script, before
   anything else. This moves the exports out of Downloads into
   `draftkings_racedata_pdfs/fixtures/` and appends a row to
   `fixtures/_staging_manifest.csv` (original name, staged name, SHA-256,
   source last-modified time, staged-at time).
3. Confirm the console output shows the expected number of files staged for
   this card (e.g. 3: DK Advanced .md, DK Basic .xlsx, TwinSpires .md). If
   the count is wrong, stop — do not proceed to ingest.
4. Point every ingest command at the staged path under `fixtures/`, never at
   Downloads. If a script still accepts a `--source-path` pointing outside
   `fixtures/`, that is now flagged automatically by
   `src/utils/source_file_guard.warn_if_outside_fixtures` at ingest time —
   check the log for `INGEST_SOURCE_OUTSIDE_FIXTURES` before trusting the run.
5. Before scoring, open `fixtures/_staging_manifest.csv` and confirm the
   SHA-256 of the file you're about to score against matches the SHA-256
   logged by the ingest step's `INGEST_SOURCE_STAMP` log line. Mismatch means
   two different files were involved somewhere in the chain — stop and
   re-stage.
6. Never re-run `stage_downloads.py` mid-workflow expecting it to "catch" a
   file that finished downloading late. If the real export lands after
   staging already ran, re-run staging, then restart ingest from the
   beginning for that source file. Partial re-ingest of one source without
   the others is how ML/ODDS-style misclassification incidents happen.

## What this does not fix

This does not validate file content (e.g. missing ODDS column). It only
guarantees that the file physically read by ingest is the file you think it
is, at the timestamp you think it was captured. Source-contract validation
(missing ODDS, missing weight, etc.) remains a separate, existing gate and
must still fail closed per the no-ML-as-ODDS rule.
