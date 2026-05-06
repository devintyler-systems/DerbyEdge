"""SQLite schema for DerbyEdge.

Normalized entities matching the system prompt:
races, tracks, horses, entries, horse_starts, workouts, people, pedigree.
Plus point_of_call and fractions for sectional pace work.

All times stored as integer hundredths of a second (Equibase convention).
All lengths stored as integer hundredths (e.g., 600 = 6.00 lengths).
"""

SCHEMA_SQL = """
-- Reference: tracks
CREATE TABLE IF NOT EXISTS tracks (
    track_id      TEXT PRIMARY KEY,
    track_name    TEXT,
    country       TEXT
);

-- Reference: people (jockeys, trainers, owners, breeders)
CREATE TABLE IF NOT EXISTS people (
    external_party_id  INTEGER PRIMARY KEY,
    type_source        TEXT,    -- JE=jockey, TE=trainer, O6=owner
    first_name         TEXT,
    middle_name        TEXT,
    last_name          TEXT
);

-- Horses (one row per registered animal)
CREATE TABLE IF NOT EXISTS horses (
    registration_number  TEXT PRIMARY KEY,
    horse_name           TEXT NOT NULL,
    foaling_date         TEXT,
    year_of_birth        INTEGER,
    foaling_area         TEXT,
    breed_type           TEXT,
    color                TEXT,
    sex                  TEXT,
    breeder_name         TEXT,
    sire_reg             TEXT,
    sire_name            TEXT,
    dam_reg              TEXT,
    dam_name             TEXT,
    dam_sire_reg         TEXT,
    dam_sire_name        TEXT
);

-- Race cards (entry-side info, one per race)
CREATE TABLE IF NOT EXISTS races (
    race_id              TEXT PRIMARY KEY,   -- track_id|date|race_no
    track_id             TEXT,
    race_date            TEXT,
    race_number          INTEGER,
    day_evening          TEXT,
    breed_type           TEXT,
    course_type          TEXT,               -- D=dirt, T=turf, etc
    surface              TEXT,
    distance_id          INTEGER,            -- Equibase: yards if Y, furlongs*100 etc
    distance_unit        TEXT,
    distance_published   TEXT,
    about_distance       TEXT,
    age_restriction      TEXT,
    sex_restriction      TEXT,
    race_type            TEXT,               -- MSW, ALW, STK, etc
    race_type_desc       TEXT,
    race_name            TEXT,
    grade                TEXT,
    purse_usa            REAL,
    min_claim_price      REAL,
    max_claim_price      REAL,
    post_time            TEXT,
    number_of_runners    INTEGER,
    conditions_text      TEXT,
    FOREIGN KEY (track_id) REFERENCES tracks(track_id)
);

-- Entries (one per starter in a race today)
CREATE TABLE IF NOT EXISTS entries (
    entry_id            TEXT PRIMARY KEY,    -- race_id|program_number
    race_id             TEXT NOT NULL,
    program_number      TEXT,
    post_position       INTEGER,
    horse_reg           TEXT,
    weight_carried      INTEGER,
    coupled_indicator   TEXT,
    couple_type         TEXT,
    equipment_code      TEXT,
    apprentice_type     TEXT,
    apprentice_wt_allow INTEGER,
    eligibility_text    TEXT,
    FOREIGN KEY (race_id) REFERENCES races(race_id),
    FOREIGN KEY (horse_reg) REFERENCES horses(registration_number)
);

-- Past performance race-level header (one per historical race the horse ran)
CREATE TABLE IF NOT EXISTS horse_starts (
    start_id              TEXT PRIMARY KEY,   -- horse_reg|race_date|track|race_no
    horse_reg             TEXT NOT NULL,
    entry_id              TEXT,               -- which today's entry produced this PP
    pp_track_id           TEXT,
    pp_country            TEXT,
    pp_race_date          TEXT,
    pp_race_number        INTEGER,
    pp_breed_type         TEXT,
    jump_flag             TEXT,
    official_indicator    TEXT,
    pp_race_type          TEXT,
    pp_course_type        TEXT,
    pp_surface            TEXT,
    pp_distance_id        INTEGER,
    pp_distance_unit      TEXT,
    pp_distance_published TEXT,
    pp_grade              TEXT,
    pp_stakes_indicator   TEXT,
    pp_age_restriction    TEXT,
    pp_max_claim_price    REAL,
    pp_purse_usa          REAL,
    pp_track_condition    TEXT,
    pp_off_turf           TEXT,
    pp_weather            TEXT,
    pp_temperature        INTEGER,
    pp_wind_speed         INTEGER,
    pp_wind_direction     TEXT,
    pp_n_starters         INTEGER,
    pp_temp_rail_distance INTEGER,
    pp_run_up_distance    INTEGER,
    pp_timer_type         TEXT,
    pp_race_name          TEXT,
    pp_race_comment       TEXT,
    pp_division           TEXT,
    -- Start (the horse's own line)
    weight_carried        INTEGER,
    medication_code       TEXT,
    equipment_code        TEXT,
    earnings_usa          REAL,
    jockey_id             INTEGER,
    trainer_id            INTEGER,
    owner_id              INTEGER,
    odds_int              INTEGER,            -- raw int; divide by 100 for $
    favorite              TEXT,
    nonbetting            TEXT,
    coupled_indicator     TEXT,
    coupled_finish        INTEGER,
    post_position         INTEGER,
    program_number        TEXT,
    official_finish       INTEGER,
    race_rating           INTEGER,
    class_rating          INTEGER,
    pace_figure_1         INTEGER,
    pace_figure_2         INTEGER,
    pace_figure_3         INTEGER,
    speed_figure          INTEGER,
    dead_heat_flag        TEXT,
    claim_price_usa       REAL,
    claimed_flag          TEXT,
    time_of_horse         INTEGER,
    dq_indicator          TEXT,
    placed_indicator      TEXT,
    short_comment         TEXT,
    long_comment          TEXT,
    FOREIGN KEY (horse_reg) REFERENCES horses(registration_number),
    FOREIGN KEY (entry_id) REFERENCES entries(entry_id)
);

-- Sectional fractions for each historical race
CREATE TABLE IF NOT EXISTS fractions (
    start_id        TEXT NOT NULL,
    fraction_label  TEXT,        -- 1, 2, 3, 4, 5, W (winner final)
    time_int        INTEGER,     -- hundredths of a second
    fraction_print  TEXT,
    PRIMARY KEY (start_id, fraction_label),
    FOREIGN KEY (start_id) REFERENCES horse_starts(start_id)
);

-- Point-of-call positions for the horse in each historical race
CREATE TABLE IF NOT EXISTS point_of_call (
    start_id          TEXT NOT NULL,
    point_of_call     TEXT,       -- S=start, 1,2,3,4,5, F=finish
    position_int      INTEGER,
    lengths_ahead     INTEGER,
    lengths_behind    INTEGER,
    print_flag        TEXT,
    PRIMARY KEY (start_id, point_of_call),
    FOREIGN KEY (start_id) REFERENCES horse_starts(start_id)
);

-- Company line (top-3 finishers in each PP race, useful for class lookups)
CREATE TABLE IF NOT EXISTS company_line (
    start_id           TEXT NOT NULL,
    horse_name         TEXT,
    weight_carried     INTEGER,
    lengths_ahead      INTEGER,
    position_at_finish INTEGER,
    official_position  INTEGER,
    PRIMARY KEY (start_id, official_position),
    FOREIGN KEY (start_id) REFERENCES horse_starts(start_id)
);

-- Workouts (one per logged morning workout for a today's-entry horse)
CREATE TABLE IF NOT EXISTS workouts (
    workout_id        TEXT PRIMARY KEY,   -- entry_id|date|seq
    entry_id          TEXT NOT NULL,
    horse_reg         TEXT,
    workout_date      TEXT,
    workout_track_id  TEXT,
    distance_id       INTEGER,
    distance_unit     TEXT,
    course_type       TEXT,
    surface           TEXT,
    track_condition   TEXT,
    workout_time      INTEGER,           -- hundredths
    type_of_workout   TEXT,
    rank_in_set       INTEGER,
    set_size          INTEGER,
    workout_note      TEXT,
    FOREIGN KEY (entry_id) REFERENCES entries(entry_id),
    FOREIGN KEY (horse_reg) REFERENCES horses(registration_number)
);

-- Indices for common access patterns
CREATE INDEX IF NOT EXISTS ix_entries_race ON entries(race_id);
CREATE INDEX IF NOT EXISTS ix_entries_horse ON entries(horse_reg);
CREATE INDEX IF NOT EXISTS ix_starts_horse ON horse_starts(horse_reg);
CREATE INDEX IF NOT EXISTS ix_starts_date ON horse_starts(pp_race_date);
CREATE INDEX IF NOT EXISTS ix_workouts_horse ON workouts(horse_reg);
CREATE INDEX IF NOT EXISTS ix_starts_surface_dist ON horse_starts(pp_surface, pp_distance_id);
"""


def init_db(conn):
    """Initialize schema on a sqlite3 connection."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()
