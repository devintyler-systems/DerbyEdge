-- Backfill: set chaos_active = 1 on historical Derby score runs
-- that had derby_override_active = 1 but pre-date the chaos_active column.
--
-- The ALTER TABLE that added chaos_active (NOT NULL DEFAULT 0) assigned 0
-- to all pre-existing rows.  This one-time UPDATE promotes those rows to 1
-- so the race_review view and calibration breakdowns reflect them correctly.
--
-- Idempotent: rows already at chaos_active = 1 are excluded by the WHERE clause.

UPDATE score_runs
SET    chaos_active = 1
WHERE  derby_override_active = 1
  AND  COALESCE(chaos_active, 0) = 0;
