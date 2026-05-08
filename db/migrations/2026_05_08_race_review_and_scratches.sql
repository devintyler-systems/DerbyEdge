-- Migration: 2026-05-08 — race_review view + chaos columns + race_results table
--
-- Safe to apply to any existing DerbyEdge database.
-- Idempotency notes:
--   • CREATE TABLE IF NOT EXISTS is safe to re-run.
--   • ALTER TABLE ADD COLUMN will fail if the column already exists.
--     SQLite has no "ADD COLUMN IF NOT EXISTS", so run these once or use
--     the Python helpers (_ensure_chaos_columns / ensure_race_review_view)
--     which wrap each ALTER in a try/except.
--   • DROP VIEW IF EXISTS / CREATE VIEW is always idempotent.
--
-- Recommended apply method (idempotent Python wrapper):
--   python -c "
--   import sys; sys.path.insert(0,'.')
--   from src.utils.db import get_connection
--   from src.services.results_intake import ensure_race_review_view
--   conn = get_connection()
--   ensure_race_review_view(conn)
--   conn.close()
--   print('done')
--   "

-- ============================================================
-- 1. Canonical results table (no-op if already present)
-- ============================================================
CREATE TABLE IF NOT EXISTS race_results (
    result_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id                 INTEGER NOT NULL REFERENCES race_cards(card_id),
    entry_id                INTEGER          REFERENCES entries(entry_id),
    horse_id                INTEGER NOT NULL REFERENCES horses(horse_id),
    post_position           INTEGER,
    finish_position         INTEGER,
    official_finish         INTEGER,
    is_scratched            INTEGER NOT NULL DEFAULT 0 CHECK(is_scratched IN (0,1)),
    is_disqualified         INTEGER NOT NULL DEFAULT 0 CHECK(is_disqualified IN (0,1)),
    official_odds_decimal   REAL,
    official_odds_american  INTEGER,
    beaten_lengths          REAL,
    speed_figure            INTEGER,
    beyer_figure            INTEGER,
    final_time              TEXT,
    earned_purse            INTEGER,
    comment                 TEXT,
    ingested_at             TEXT NOT NULL,
    UNIQUE(card_id, entry_id)
);

CREATE INDEX IF NOT EXISTS idx_rr_card        ON race_results(card_id);
CREATE INDEX IF NOT EXISTS idx_rr_entry       ON race_results(entry_id);
CREATE INDEX IF NOT EXISTS idx_rr_card_finish ON race_results(card_id, finish_position);

-- ============================================================
-- 2. Chaos columns on score_runs
--    chaos_active        : 1 when the chaos patch was applied this run
--    chaos_intensity     : realloc_target output (0.05–0.10 for Derby, 0 otherwise)
--    field_entropy_score : Shannon entropy of base win_probs
-- ============================================================
ALTER TABLE score_runs ADD COLUMN chaos_active        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE score_runs ADD COLUMN chaos_intensity     REAL;
ALTER TABLE score_runs ADD COLUMN field_entropy_score REAL;

-- ============================================================
-- 3. Chaos columns on entry_scores
--    chaos_score    : WinProb_final from chaos patch (NULL when chaos inactive)
--    chaos_boost    : WinProb_final − WinProb_base (+ = beneficiary, − = donor)
--    chaos_tier     : 'none' | 'light' | 'strong'  (DarkHorseTier)
--    chaos_eligible : 1 if entry meets DarkHorseFlag criteria
-- ============================================================
ALTER TABLE entry_scores ADD COLUMN chaos_score    REAL;
ALTER TABLE entry_scores ADD COLUMN chaos_boost    REAL;
ALTER TABLE entry_scores ADD COLUMN chaos_tier     TEXT;
ALTER TABLE entry_scores ADD COLUMN chaos_eligible INTEGER NOT NULL DEFAULT 0;

-- ============================================================
-- 4. race_review view — drop and recreate
--    One row per (score_run × race_card).
--    Scratch-aware: effective_tp skips the original TP if scratched.
--    The Python helper ensure_race_review_view() manages this view;
--    running it here gives an equivalent standalone path.
-- ============================================================
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
        MAX(CASE WHEN rl.model_rank = 1 THEN rl.entry_id    END)
            AS original_tp_entry_id,
        MAX(CASE WHEN rl.model_rank = 1
                 THEN COALESCE(rr.is_scratched, 0) END)
            AS original_tp_scratched,
        MAX(CASE WHEN rl.effective_live_rank = 1 THEN rl.horse_name  END)
            AS effective_tp,
        MAX(CASE WHEN rl.effective_live_rank = 1 THEN rl.entry_id    END)
            AS effective_tp_entry_id,
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
    COALESCE(sr.chaos_active, sr.derby_override_active, 0)         AS chaos_active,
    sr.chaos_intensity,
    sr.quality_tier,
    sr.run_timestamp,
    tops.original_tp,
    tops.original_tp_entry_id,
    tops.original_tp_scratched,
    tops.effective_tp,
    tops.effective_tp_entry_id,
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
