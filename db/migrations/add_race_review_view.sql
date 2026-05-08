-- Migration: add race_review view
-- Safe to run on any existing DerbyEdge database.
-- The view is a pure read layer — no table changes, no data movement.
-- Requires SQLite 3.25+ (window functions).

DROP VIEW IF EXISTS race_review;

CREATE VIEW race_review AS
WITH ranked_live AS (
    SELECT
        es.run_id,
        sr.card_id,
        es.entry_id,
        es.horse_name,
        es.rank          AS model_rank,
        es.win_probability,
        es.value_score,
        es.bet_tag,
        ROW_NUMBER() OVER (
            PARTITION BY es.run_id
            ORDER BY
                CASE WHEN COALESCE(rr.is_scratched, 0) = 1 THEN 1 ELSE 0 END,
                es.rank ASC
        ) AS effective_live_rank
    FROM entry_scores es
    JOIN  score_runs    sr  ON sr.run_id   = es.run_id
    LEFT JOIN race_results rr
           ON rr.entry_id = es.entry_id
          AND rr.card_id  = sr.card_id
),
tops AS (
    SELECT
        rl.run_id,
        rl.card_id,
        MAX(CASE WHEN rl.model_rank = 1 THEN rl.horse_name  END)
            AS original_tp,
        MAX(CASE WHEN rl.model_rank = 1
                 THEN COALESCE(rr.is_scratched, 0) END)
            AS original_tp_scratched,
        MAX(CASE WHEN rl.effective_live_rank = 1 THEN rl.horse_name  END)
            AS effective_tp,
        MAX(CASE WHEN rl.effective_live_rank = 1 THEN rl.model_rank  END)
            AS effective_tp_rank,
        MAX(CASE WHEN rl.effective_live_rank = 1 THEN rr.finish_position END)
            AS effective_tp_finish,
        MAX(CASE WHEN rl.effective_live_rank = 1
                  AND rr.official_finish = 1
                  AND COALESCE(rr.is_disqualified, 0) = 0
                 THEN 1 ELSE 0 END)
            AS effective_tp_won
    FROM ranked_live rl
    LEFT JOIN race_results rr
           ON rr.entry_id = rl.entry_id
          AND rr.card_id  = rl.card_id
    GROUP BY rl.run_id, rl.card_id
)
SELECT
    rc.card_id,
    rc.card_date                                                    AS race_date,
    t.abbrev                                                        AS track,
    rc.race_number,
    rc.surface,
    rc.distance_furlongs,
    CASE WHEN rc.distance_furlongs < 8.5 THEN 'sprint' ELSE 'route' END
                                                                    AS dist_category,
    COALESCE(rc.field_size,
        (SELECT COUNT(*) FROM entries e
         WHERE e.card_id = rc.card_id AND e.scratch_flag = 0))     AS field_size,
    rc.race_class,
    sr.run_id,
    sr.model_id,
    sr.model_type,
    sr.derby_override_active                                        AS chaos_active,
    sr.quality_tier,
    sr.run_timestamp,
    tops.original_tp,
    tops.original_tp_scratched,
    tops.effective_tp,
    tops.effective_tp_rank,
    tops.effective_tp_finish,
    tops.effective_tp_won,
    winner_h.name                                                   AS actual_winner,
    winner_rr.entry_id                                              AS actual_winner_entry_id
FROM score_runs sr
JOIN  race_cards rc        ON rc.card_id         = sr.card_id
JOIN  tracks     t         ON t.track_id         = rc.track_id
LEFT JOIN tops             ON tops.run_id        = sr.run_id
LEFT JOIN race_results winner_rr
       ON winner_rr.card_id                      = rc.card_id
      AND winner_rr.official_finish              = 1
      AND COALESCE(winner_rr.is_scratched,    0) = 0
      AND COALESCE(winner_rr.is_disqualified, 0) = 0
LEFT JOIN horses winner_h  ON winner_h.horse_id  = winner_rr.horse_id;
