-- ============================================================
-- DerbyEdge Engine  V1  —  Normalized SQLite Schema
-- Requires SQLite 3.31+ (generated columns)
-- ============================================================
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- 1. TRACKS
--    One row per physical racing plant.
-- ============================================================
CREATE TABLE IF NOT EXISTS tracks (
    track_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    abbrev     TEXT    NOT NULL UNIQUE,
    city       TEXT,
    state      TEXT,
    country    TEXT    NOT NULL DEFAULT 'USA',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================
-- 2. RACE_CARDS
--    One row per individual race (not per day).
--    distance_furlongs is a stored generated column so that
--    queries can filter on furlongs without extra arithmetic.
-- ============================================================
CREATE TABLE IF NOT EXISTS race_cards (
    card_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id          INTEGER NOT NULL REFERENCES tracks(track_id),
    card_date         TEXT    NOT NULL,            -- ISO-8601  YYYY-MM-DD
    race_number       INTEGER NOT NULL DEFAULT 1,
    stakes_name       TEXT,                        -- NULL for overnight races
    purse             INTEGER,
    distance_yards    INTEGER NOT NULL,
    distance_furlongs REAL    NOT NULL
                      GENERATED ALWAYS AS (ROUND(distance_yards / 220.0, 2)) STORED,
    surface           TEXT    NOT NULL DEFAULT 'dirt'
                      CHECK(surface IN ('dirt','turf','synthetic','all_weather')),
    race_class        TEXT,                        -- e.g. G1, MAIDEN, CLM25k
    age_restriction   TEXT,                        -- e.g. 3YO, 3UP
    conditions        TEXT,
    field_size        INTEGER,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(track_id, card_date, race_number)
);

-- ============================================================
-- 3. HORSES
--    Canonical horse profile.  name is the key used for
--    de-duplication; COLLATE NOCASE prevents case-split dupes.
-- ============================================================
CREATE TABLE IF NOT EXISTS horses (
    horse_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    sire         TEXT,
    dam          TEXT,
    year_foaled  INTEGER,
    sex          TEXT    CHECK(sex IN ('C','F','H','G','R','M') OR sex IS NULL),
    color        TEXT,
    country_bred TEXT    NOT NULL DEFAULT 'USA',
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================
-- 4. PEOPLE
--    Trainers, jockeys, owners.  One person can hold multiple
--    roles; the UNIQUE constraint is (name, role) not just name.
-- ============================================================
CREATE TABLE IF NOT EXISTS people (
    person_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT    NOT NULL,
    role          TEXT    NOT NULL
                  CHECK(role IN ('trainer','jockey','owner','breeder')),
    license_state TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(full_name, role)
);

-- ============================================================
-- 5. ENTRIES
--    A horse's registration for a specific race.
--    morning_line_prob is the raw implied probability from the
--    morning line before any overround removal.
-- ============================================================
CREATE TABLE IF NOT EXISTS entries (
    entry_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id           INTEGER NOT NULL REFERENCES race_cards(card_id),
    horse_id          INTEGER NOT NULL REFERENCES horses(horse_id),
    trainer_id        INTEGER          REFERENCES people(person_id),
    jockey_id         INTEGER          REFERENCES people(person_id),
    owner_id          INTEGER          REFERENCES people(person_id),
    post_position     INTEGER NOT NULL,
    weight            INTEGER NOT NULL DEFAULT 126,
    morning_line_odds REAL    NOT NULL CHECK(morning_line_odds > 0),
    morning_line_prob REAL    NOT NULL
                      GENERATED ALWAYS AS (ROUND(1.0 / (morning_line_odds + 1.0), 6)) STORED,
    scratch_flag      INTEGER NOT NULL DEFAULT 0 CHECK(scratch_flag IN (0,1)),
    -- Seed-only aggregate columns; populated from CSV, null when real data present
    career_starts     INTEGER,
    career_wins       INTEGER,
    career_places     INTEGER,
    career_shows      INTEGER,
    career_earnings   INTEGER,
    dirt_starts       INTEGER,
    dirt_wins         INTEGER,
    dist_starts       INTEGER,  -- starts at today's race distance ±0.5f
    dist_wins         INTEGER,
    wet_starts        INTEGER,
    wet_wins          INTEGER,
    last_race_days    INTEGER,
    last_race_finish  INTEGER,
    last_speed_fig    INTEGER,
    best_speed_fig    INTEGER,
    avg_speed_fig     REAL,
    beyer_fig         INTEGER,
    workouts_30       INTEGER,
    gate_class        INTEGER,
    stamina_index     REAL,
    pace_style        TEXT    CHECK(pace_style IN ('front','presser','stalker','closer') OR pace_style IS NULL),
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(card_id, post_position),
    UNIQUE(card_id, horse_id)
);

-- ============================================================
-- 6. HORSE_STARTS
--    Official result for one horse in one race.
--    This is the source of truth for historical form.
-- ============================================================
CREATE TABLE IF NOT EXISTS horse_starts (
    start_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id           INTEGER NOT NULL REFERENCES entries(entry_id),
    horse_id           INTEGER NOT NULL REFERENCES horses(horse_id),
    card_id            INTEGER NOT NULL REFERENCES race_cards(card_id),
    finish_position    INTEGER,
    lengths_behind     REAL    NOT NULL DEFAULT 0.0,
    official_time_secs REAL,
    speed_figure       INTEGER,
    beyer_figure       INTEGER,
    earned_purse       INTEGER,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================
-- 7. WORKOUTS
--    Individual workout records.  work_grade: B=bullet, F=fast,
--    G=good, N=normal.  hand_timed=1 flags unofficial clocking.
-- ============================================================
CREATE TABLE IF NOT EXISTS workouts (
    workout_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id          INTEGER NOT NULL REFERENCES horses(horse_id),
    workout_date      TEXT    NOT NULL,
    track_id          INTEGER          REFERENCES tracks(track_id),
    distance_furlongs REAL    NOT NULL,
    time_seconds      REAL    NOT NULL,
    work_grade        TEXT    NOT NULL DEFAULT 'N'
                      CHECK(work_grade IN ('B','F','G','N')),
    surface           TEXT    NOT NULL DEFAULT 'dirt',
    hand_timed        INTEGER NOT NULL DEFAULT 0 CHECK(hand_timed IN (0,1)),
    synthetic         INTEGER NOT NULL DEFAULT 0 CHECK(synthetic IN (0,1)),
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================
-- 8. ODDS_SNAPSHOTS
--    Time-series of odds for a given entry.  implied_prob is
--    the raw fraction before overround normalization.
--    source: morning_line | tote | book | model
-- ============================================================
CREATE TABLE IF NOT EXISTS odds_snapshots (
    snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id         INTEGER NOT NULL REFERENCES entries(entry_id),
    snapshot_time    TEXT    NOT NULL,
    odds_numerator   REAL    NOT NULL CHECK(odds_numerator >= 0),
    odds_denominator REAL    NOT NULL DEFAULT 1.0 CHECK(odds_denominator > 0),
    implied_prob     REAL    NOT NULL
                     GENERATED ALWAYS AS (
                         ROUND(odds_denominator / (odds_numerator + odds_denominator), 6)
                     ) STORED,
    source           TEXT    NOT NULL DEFAULT 'morning_line'
                     CHECK(source IN ('morning_line','tote','book','model')),
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================
-- 9. TRACK_BIAS
--    Observed bias at a track on a given day.
--    early_speed_bias > 0  →  front speed favored
--    early_speed_bias < 0  →  closers favored
--    post_skew_json: {"1":0.12,"2":-0.05, ...}
-- ============================================================
CREATE TABLE IF NOT EXISTS track_bias (
    bias_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id         INTEGER NOT NULL REFERENCES tracks(track_id),
    bias_date        TEXT    NOT NULL,
    surface          TEXT    NOT NULL DEFAULT 'dirt',
    dist_category    TEXT    NOT NULL DEFAULT 'route'
                     CHECK(dist_category IN ('sprint','route')),
    rail_position    TEXT,
    early_speed_bias REAL             DEFAULT 0.0,
    post_skew_json   TEXT,
    notes            TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(track_id, bias_date, surface, dist_category)
);

-- ============================================================
-- 10. TRIP_FLAGS
--     Post-race annotations on trouble in running.
--     severity: 1=minor  2=moderate  3=severe
-- ============================================================
CREATE TABLE IF NOT EXISTS trip_flags (
    flag_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    start_id   INTEGER NOT NULL REFERENCES horse_starts(start_id),
    flag_type  TEXT    NOT NULL
               CHECK(flag_type IN (
                   'wide','traffic','stumble','pocket','saved_ground',
                   'checked','steadied','bumped','fell','disqualified','other'
               )),
    severity   INTEGER NOT NULL DEFAULT 1 CHECK(severity BETWEEN 1 AND 3),
    notes      TEXT,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================
-- 11. MODEL_REGISTRY
--     Metadata and evaluation metrics for every trained artifact.
--     model_family constrains to the four race-type families plus
--     the Derby override and the legacy fallback.
-- ============================================================
CREATE TABLE IF NOT EXISTS model_registry (
    model_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name        TEXT    NOT NULL UNIQUE,
    model_family      TEXT    NOT NULL
                      CHECK(model_family IN (
                          'dirt_sprint','dirt_route',
                          'turf_sprint','turf_route',
                          'derby_override','fallback'
                      )),
    version           TEXT    NOT NULL DEFAULT '0.1.0',
    artifact_path     TEXT,
    training_rows     INTEGER,
    train_date_min    TEXT,
    train_date_max    TEXT,
    log_loss          REAL,
    brier_score       REAL,
    calibration_error REAL,
    top1_hit_rate     REAL,
    edge_roi          REAL,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================
-- 12. SCORE_RUNS
--     One row per execution of the scorer.  Links a race card
--     to the model used and the timestamp of the run.
-- ============================================================
CREATE TABLE IF NOT EXISTS score_runs (
    run_id                TEXT    PRIMARY KEY,
    card_id               INTEGER NOT NULL REFERENCES race_cards(card_id),
    model_id              INTEGER          REFERENCES model_registry(model_id),
    run_timestamp         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    model_type            TEXT    NOT NULL DEFAULT 'fallback'
                          CHECK(model_type IN ('xgboost','fallback','derby_override','seed_only_baseline')),
    derby_override_active INTEGER NOT NULL DEFAULT 0 CHECK(derby_override_active IN (0,1)),
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ============================================================
-- 13. ENTRY_SCORES
--     Per-entry output of a score run.  Probabilities are stored
--     as fractions in [0,1].  fair_odds and model_edge are stored
--     generated columns so they are always consistent with the
--     stored probabilities.
-- ============================================================
CREATE TABLE IF NOT EXISTS entry_scores (
    score_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT    NOT NULL REFERENCES score_runs(run_id),
    entry_id            INTEGER NOT NULL REFERENCES entries(entry_id),
    horse_name          TEXT    NOT NULL,
    post_position       INTEGER NOT NULL,
    morning_line_odds   REAL    NOT NULL,
    -- win/place/show stored as fractions 0-1
    win_probability     REAL    CHECK(win_probability  IS NULL OR win_probability  BETWEEN 0 AND 1),
    place_probability   REAL    CHECK(place_probability IS NULL OR place_probability BETWEEN 0 AND 1),
    show_probability    REAL    CHECK(show_probability IS NULL OR show_probability  BETWEEN 0 AND 1),
    -- fair_odds: decimal odds implied by model win probability
    fair_odds           REAL
                        GENERATED ALWAYS AS (
                            CASE WHEN win_probability > 0
                            THEN ROUND((1.0 / win_probability) - 1.0, 2)
                            ELSE NULL END
                        ) STORED,
    pace_fit_score      REAL,
    form_score          REAL,
    surface_dist_fit    REAL,
    value_score         REAL,
    market_implied_prob REAL,
    -- model_edge: positive = model likes horse more than market
    model_edge          REAL
                        GENERATED ALWAYS AS (
                            CASE WHEN win_probability IS NOT NULL
                                  AND market_implied_prob IS NOT NULL
                            THEN ROUND(win_probability - market_implied_prob, 4)
                            ELSE NULL END
                        ) STORED,
    bet_tag             TEXT
                        CHECK(bet_tag IN ('bet','neutral','underlay','no_data') OR bet_tag IS NULL),
    confidence_flag     INTEGER NOT NULL DEFAULT 0 CHECK(confidence_flag IN (0,1)),
    missing_data_flag   INTEGER NOT NULL DEFAULT 0 CHECK(missing_data_flag IN (0,1)),
    low_conf_bet_block  INTEGER NOT NULL DEFAULT 0 CHECK(low_conf_bet_block IN (0,1)),
    rank                INTEGER,
    trainer_name        TEXT,
    jockey_name         TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(run_id, entry_id)
);

-- ============================================================
-- 14. FEATURE_STORE
--     One row per entry per build run.  All feature columns are
--     nullable REAL; NULL means the feature could not be computed
--     from available data (see feature_catalog.csv for null_reason).
--     Tier labels: IMPLEMENTED | DEGRADED | PLACEHOLDER
-- ============================================================
CREATE TABLE IF NOT EXISTS feature_store (
    feature_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id         INTEGER NOT NULL REFERENCES race_cards(card_id),
    entry_id        INTEGER NOT NULL REFERENCES entries(entry_id),
    horse_id        INTEGER NOT NULL REFERENCES horses(horse_id),
    horse_name      TEXT    NOT NULL,
    post_position   INTEGER NOT NULL,
    build_ts        TEXT    NOT NULL,
    -- Speed / pace / form
    speed_last                  REAL,   -- IMPLEMENTED
    speed_best                  REAL,   -- IMPLEMENTED
    speed_avg                   REAL,   -- IMPLEMENTED
    beyer_last                  REAL,   -- IMPLEMENTED
    speed_best_3                REAL,   -- DEGRADED: avg(best,last,avg); true best-3 needs horse_starts
    pace_early_mean_3           REAL,   -- PLACEHOLDER: needs call-fraction times from horse_starts
    pace_mid_mean_3             REAL,   -- PLACEHOLDER: needs call-fraction times from horse_starts
    finish_energy_proxy         REAL,   -- DEGRADED: pace_style reserve + last_finish
    form_cycle_idx              REAL,   -- DEGRADED: career_itm_pct weighted by last_finish
    layoff_days                 INTEGER,-- IMPLEMENTED
    career_win_pct              REAL,   -- IMPLEMENTED
    career_itm_pct              REAL,   -- IMPLEMENTED
    -- Class / field strength
    class_delta                 REAL,   -- DEGRADED: z-score of career_earnings within field
    field_strength_last         REAL,   -- PLACEHOLDER: needs competitors speed_figs from horse_starts
    horses_beaten_pct_last      REAL,   -- DEGRADED: (typical_field - last_finish) / (field-1)
    field_size_exp              REAL,   -- DEGRADED: career_starts normalized
    -- Workouts / readiness
    works_30d                   INTEGER,-- IMPLEMENTED (aggregate count from seed)
    bullet_30d                  INTEGER,-- PLACEHOLDER: needs workouts table grade='B'
    days_since_last_work        INTEGER,-- PLACEHOLDER: needs workouts table
    work_readiness_score        REAL,   -- DEGRADED: works_30d count + gate_class
    -- Connections
    trainer_intent_proxy        REAL,   -- DEGRADED: work_load + freshness
    trainer_jockey_itm_cond     REAL,   -- PLACEHOLDER: needs horse_starts conditioned stats
    jockey_route_cond           REAL,   -- PLACEHOLDER: needs horse_starts conditioned stats
    trainer_derby_cond          REAL,   -- PLACEHOLDER: needs horse_starts at Churchill/10f+
    -- Fit
    surface_fit                 REAL,   -- DEGRADED: dirt_win_pct with sample weighting
    distance_fit                REAL,   -- DEGRADED: stamina_index + dist_win_pct
    route_progression           REAL,   -- DEGRADED: distance_fit (route context)
    pedigree_route_proxy        REAL,   -- DEGRADED: sire-line route aptitude lookup
    -- Post / trip / bias
    post_win_bias               REAL,   -- PLACEHOLDER: needs track_bias or post history
    gate_reliability            REAL,   -- DEGRADED: gate_class normalized
    trouble_recovery_proxy      REAL,   -- PLACEHOLDER: needs trip_flags
    traffic_resilience_proxy    REAL,   -- DEGRADED: pace_style + field_size_exp
    -- Race shape (computed across full field)
    early_intent                REAL,   -- IMPLEMENTED: pace_style -> 0-1 scale
    run_style_bucket            TEXT,   -- IMPLEMENTED: pace_style pass-through
    pace_pressure               REAL,   -- IMPLEMENTED: (front+presser)/field_size
    lone_speed_edge             INTEGER,-- IMPLEMENTED: 1 if only front-runner
    collapse_risk               REAL,   -- IMPLEMENTED: pace_pressure alias
    pace_fit_score              REAL,   -- IMPLEMENTED: style x field shape matrix
    -- Market / publicness
    market_implied_prob         REAL,   -- IMPLEMENTED: 1/(odds+1)
    morning_line_rank           INTEGER,-- IMPLEMENTED: rank by implied_prob within field
    publicness_score            REAL,   -- DEGRADED: market_prob / career_win_pct
    public_underlay_penalty     REAL,   -- DEGRADED: z-score of publicness within field
    -- Derby override
    classic_distance_projection REAL,   -- DEGRADED: stamina_index + dist_win_pct
    churchill_readiness         REAL,   -- PLACEHOLDER: needs Churchill historical data
    jan_apr_improvement_curve   REAL,   -- PLACEHOLDER: needs sequential speed figs
    derby_override_score        REAL,   -- DEGRADED: weighted composite of available proxies
    UNIQUE(card_id, entry_id)
);

-- ============================================================
-- INDEXES
-- ============================================================
-- race_cards
CREATE INDEX IF NOT EXISTS idx_rc_date         ON race_cards(card_date);
CREATE INDEX IF NOT EXISTS idx_rc_track_date   ON race_cards(track_id, card_date);

-- entries
CREATE INDEX IF NOT EXISTS idx_ent_card        ON entries(card_id);
CREATE INDEX IF NOT EXISTS idx_ent_horse       ON entries(horse_id);
CREATE INDEX IF NOT EXISTS idx_ent_trainer     ON entries(trainer_id);
CREATE INDEX IF NOT EXISTS idx_ent_jockey      ON entries(jockey_id);

-- horse_starts
CREATE INDEX IF NOT EXISTS idx_hs_horse        ON horse_starts(horse_id);
CREATE INDEX IF NOT EXISTS idx_hs_card         ON horse_starts(card_id);
CREATE INDEX IF NOT EXISTS idx_hs_entry        ON horse_starts(entry_id);

-- workouts
CREATE INDEX IF NOT EXISTS idx_wk_horse_date   ON workouts(horse_id, workout_date);

-- odds_snapshots
CREATE INDEX IF NOT EXISTS idx_odds_entry_time ON odds_snapshots(entry_id, snapshot_time);

-- entry_scores
CREATE INDEX IF NOT EXISTS idx_es_run          ON entry_scores(run_id);
CREATE INDEX IF NOT EXISTS idx_es_entry        ON entry_scores(entry_id);
CREATE INDEX IF NOT EXISTS idx_es_run_rank     ON entry_scores(run_id, rank);

-- score_runs
CREATE INDEX IF NOT EXISTS idx_sr_card         ON score_runs(card_id);

-- feature_store
CREATE INDEX IF NOT EXISTS idx_fs_card         ON feature_store(card_id);
CREATE INDEX IF NOT EXISTS idx_fs_entry        ON feature_store(entry_id);

-- ============================================================
-- VIEWS
-- All views are pre-race safe (no post-result fields are used
-- to filter or derive values in v_entries_live or v_race_type).
-- Views over horse_starts / workouts return 0 rows for seed-only
-- installs — this is expected and documented in the validation report.
-- ============================================================

-- v_race_type: race categorization by surface + distance class
CREATE VIEW IF NOT EXISTS v_race_type AS
SELECT
    rc.card_id,
    rc.track_id,
    t.name        AS track_name,
    t.abbrev      AS track_abbrev,
    rc.card_date,
    rc.race_number,
    rc.stakes_name,
    rc.purse,
    rc.distance_furlongs,
    rc.surface,
    rc.race_class,
    rc.age_restriction,
    rc.field_size,
    CASE WHEN rc.distance_furlongs < 8.5 THEN 'sprint' ELSE 'route' END
        AS dist_category,
    rc.surface || '_' ||
        CASE WHEN rc.distance_furlongs < 8.5 THEN 'sprint' ELSE 'route' END
        AS race_type_key
FROM race_cards rc
JOIN tracks t ON rc.track_id = t.track_id;


-- v_entries_live: non-scratched entries with full connection info.
-- Seed-compat aggregate columns (career_starts, etc.) come from
-- entries; they are NULL when this entry has no seed row.
CREATE VIEW IF NOT EXISTS v_entries_live AS
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
    -- seed-compat aggregate columns (NULL when real horse_starts used)
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
    e.pace_style
FROM  entries    e
JOIN  race_cards rc  ON e.card_id    = rc.card_id
JOIN  horses     h   ON e.horse_id   = h.horse_id
LEFT JOIN people ptr ON e.trainer_id = ptr.person_id
LEFT JOIN people pjk ON e.jockey_id  = pjk.person_id
LEFT JOIN people pow ON e.owner_id   = pow.person_id
WHERE e.scratch_flag = 0;


-- v_horse_last_5: last 5 official starts per horse from horse_starts.
-- NOTE: returns 0 rows for seed-only installs (horse_starts is empty).
CREATE VIEW IF NOT EXISTS v_horse_last_5 AS
SELECT
    inner_q.horse_id,
    inner_q.horse_name,
    inner_q.card_date,
    inner_q.race_number,
    inner_q.stakes_name,
    inner_q.distance_furlongs,
    inner_q.surface,
    inner_q.finish_position,
    inner_q.speed_figure,
    inner_q.beyer_figure,
    inner_q.lengths_behind,
    inner_q.earned_purse,
    inner_q.recency_rank
FROM (
    SELECT
        hs.horse_id,
        h.name                 AS horse_name,
        rc.card_date,
        rc.race_number,
        rc.stakes_name,
        rc.distance_furlongs,
        rc.surface,
        hs.finish_position,
        hs.speed_figure,
        hs.beyer_figure,
        hs.lengths_behind,
        hs.earned_purse,
        ROW_NUMBER() OVER (
            PARTITION BY hs.horse_id
            ORDER BY rc.card_date DESC, rc.race_number DESC
        ) AS recency_rank
    FROM horse_starts hs
    JOIN race_cards rc ON hs.card_id  = rc.card_id
    JOIN horses     h  ON hs.horse_id = h.horse_id
) inner_q
WHERE inner_q.recency_rank <= 5;


-- v_workout_30: real (non-synthetic) workouts in a rolling 30-day window.
-- synthetic=0 filter excludes records seeded by migrate_schema.py.
-- NOTE: returns 0 rows for seed-only installs (no real workout records).
CREATE VIEW IF NOT EXISTS v_workout_30 AS
SELECT
    w.workout_id,
    w.horse_id,
    h.name     AS horse_name,
    w.workout_date,
    w.distance_furlongs,
    w.time_seconds,
    ROUND(w.time_seconds / w.distance_furlongs, 2) AS secs_per_furlong,
    w.work_grade,
    w.surface,
    w.hand_timed,
    t.abbrev   AS track_abbrev
FROM  workouts w
JOIN  horses   h  ON w.horse_id  = h.horse_id
LEFT JOIN tracks t ON w.track_id = t.track_id
WHERE w.synthetic = 0
  AND julianday('now') - julianday(w.workout_date) <= 30;


-- v_connections_180: trainer-jockey pair stats over rolling 180 days.
-- Derived purely from horse_starts and entries.
-- NOTE: returns 0 rows for seed-only installs (horse_starts is empty).
CREATE VIEW IF NOT EXISTS v_connections_180 AS
SELECT
    e.trainer_id,
    ptr.full_name   AS trainer,
    e.jockey_id,
    pjk.full_name   AS jockey,
    COUNT(*)        AS starts_180,
    SUM(CASE WHEN hs.finish_position = 1 THEN 1 ELSE 0 END)  AS wins_180,
    SUM(CASE WHEN hs.finish_position <= 3 THEN 1 ELSE 0 END) AS itm_180,
    ROUND(
        CAST(SUM(CASE WHEN hs.finish_position = 1 THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0), 3
    ) AS win_pct_180,
    ROUND(
        CAST(SUM(CASE WHEN hs.finish_position <= 3 THEN 1 ELSE 0 END) AS REAL)
        / NULLIF(COUNT(*), 0), 3
    ) AS itm_pct_180
FROM  horse_starts hs
JOIN  entries     e   ON hs.entry_id  = e.entry_id
JOIN  race_cards  rc  ON hs.card_id   = rc.card_id
JOIN  people      ptr ON e.trainer_id = ptr.person_id
JOIN  people      pjk ON e.jockey_id  = pjk.person_id
WHERE julianday('now') - julianday(rc.card_date) <= 180
GROUP BY e.trainer_id, e.jockey_id;
