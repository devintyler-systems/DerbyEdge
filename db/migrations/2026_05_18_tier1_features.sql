-- Migration: 2026-05-18 — Tier 1 features + field_size_last
--
-- 1. Adds field_size_last to horse_starts (number of starters in each race).
-- 2. Adds 8 Tier 1 feature columns to feature_store.
-- 3. Recreates v_entries_live to expose field_size_last.
--
-- Applied automatically by src/utils/db.py _migrate_db().
-- Safe to run manually on any existing DerbyEdge database.

-- horse_starts
ALTER TABLE horse_starts ADD COLUMN field_size_last INTEGER;

-- feature_store — Tier 1 columns
ALTER TABLE feature_store ADD COLUMN speed_fig_adj            REAL;
ALTER TABLE feature_store ADD COLUMN layoff_bucket_encoded    REAL;
ALTER TABLE feature_store ADD COLUMN class_level              REAL;
ALTER TABLE feature_store ADD COLUMN class_delta_v2           REAL;
ALTER TABLE feature_store ADD COLUMN horses_beaten_pct_actual REAL;
ALTER TABLE feature_store ADD COLUMN pace_pressure_tier       INTEGER;
ALTER TABLE feature_store ADD COLUMN collapse_risk_v2         REAL;
ALTER TABLE feature_store ADD COLUMN morning_line_delta       REAL;

-- v_entries_live — drop and recreate with field_size_last
DROP VIEW IF EXISTS v_entries_live;

CREATE VIEW v_entries_live AS
SELECT
    e.entry_id,
    e.card_id,
    rc.card_date,
    rc.stakes_name,
    rc.distance_furlongs,
    rc.surface,
    CASE WHEN rc.distance_furlongs < 8.5 THEN 'sprint' ELSE 'route' END
        AS dist_category,
    h.horse_id,
    h.name          AS horse_name,
    h.sire,
    h.dam,
    e.post_position,
    e.weight,
    e.morning_line_odds,
    e.morning_line_prob,
    ptr.person_id   AS trainer_id,
    ptr.full_name   AS trainer,
    pjk.person_id   AS jockey_id,
    pjk.full_name   AS jockey,
    pow.person_id   AS owner_id,
    pow.full_name   AS owner,
    e.career_starts,
    e.career_wins,
    e.career_places,
    e.career_shows,
    e.career_earnings,
    e.last_race_days,
    e.last_race_finish,
    e.best_speed_fig,
    e.last_speed_fig,
    e.avg_speed_fig,
    e.beyer_fig,
    e.dirt_starts,
    e.dirt_wins,
    e.dist_starts,
    e.dist_wins,
    e.wet_starts,
    e.wet_wins,
    e.workouts_30,
    e.gate_class,
    e.stamina_index,
    e.pace_style,
    hs_last.field_size_last
FROM  entries    e
JOIN  race_cards rc  ON e.card_id    = rc.card_id
JOIN  horses     h   ON e.horse_id   = h.horse_id
LEFT JOIN people ptr ON e.trainer_id = ptr.person_id
LEFT JOIN people pjk ON e.jockey_id  = pjk.person_id
LEFT JOIN people pow ON e.owner_id   = pow.person_id
LEFT JOIN (
    SELECT hs.horse_id, hs.field_size_last
    FROM   horse_starts hs
    WHERE  hs.start_id = (
        SELECT MAX(hs2.start_id)
        FROM   horse_starts hs2
        WHERE  hs2.horse_id = hs.horse_id
    )
) hs_last ON hs_last.horse_id = h.horse_id
WHERE e.scratch_flag = 0;
