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
    run_id        TEXT    PRIMARY KEY,
    card_id       INTEGER NOT NULL REFERENCES race_cards(card_id),
    model_id      INTEGER          REFERENCES model_registry(model_id),
    run_timestamp TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    model_type    TEXT    NOT NULL DEFAULT 'fallback'
                  CHECK(model_type IN ('xgboost','fallback','derby_override')),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
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
    rank                INTEGER,
    trainer_name        TEXT,
    jockey_name         TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(run_id, entry_id)
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
