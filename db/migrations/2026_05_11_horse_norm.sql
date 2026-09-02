-- Migration: 2026-05-11 — add horse_norm to starter_observations
--
-- Adds a normalised horse-name column used as the primary join key in the
-- shadow-evaluation pipeline.  horse_norm is computed by Python on insert;
-- this ALTER only adds the column — backfill existing rows with:
--
--   python -m training.migrate_horse_norm
--
-- Safe to apply to any existing DerbyEdge database.
-- SQLite has no "ADD COLUMN IF NOT EXISTS"; use the Python wrapper below or
-- wrap the ALTER in a try/except (as done by migrate_horse_norm.py).
--
-- Recommended apply method (idempotent):
--   python -m training.migrate_horse_norm

ALTER TABLE starter_observations ADD COLUMN horse_norm TEXT;
