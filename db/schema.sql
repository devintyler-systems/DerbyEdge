-- DerbyEdge Engine — SQLite Schema

CREATE TABLE IF NOT EXISTS horses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sire TEXT,
    dam TEXT,
    owner TEXT,
    trainer TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_name TEXT NOT NULL,
    race_date TEXT NOT NULL,
    track TEXT NOT NULL,
    distance_furlongs REAL NOT NULL,
    surface TEXT DEFAULT 'dirt',
    race_class TEXT,
    purse INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS race_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER REFERENCES races(id),
    horse_id INTEGER REFERENCES horses(id),
    post_position INTEGER,
    morning_line_odds REAL,
    finish_position INTEGER,
    lengths_behind REAL DEFAULT 0,
    speed_figure INTEGER,
    beyer_figure INTEGER,
    time_seconds REAL,
    weight INTEGER DEFAULT 126,
    jockey TEXT,
    trainer TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS derby_field (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_name TEXT NOT NULL UNIQUE,
    post_position INTEGER NOT NULL,
    morning_line_odds REAL NOT NULL,
    trainer TEXT,
    jockey TEXT,
    sire TEXT,
    dam TEXT,
    owner TEXT,
    weight INTEGER DEFAULT 126,
    career_starts INTEGER DEFAULT 0,
    career_wins INTEGER DEFAULT 0,
    career_places INTEGER DEFAULT 0,
    career_shows INTEGER DEFAULT 0,
    career_earnings INTEGER DEFAULT 0,
    last_race_days_ago INTEGER DEFAULT 21,
    last_race_finish INTEGER DEFAULT 1,
    last_race_speed_figure INTEGER DEFAULT 95,
    best_speed_figure INTEGER DEFAULT 100,
    avg_speed_figure REAL DEFAULT 93,
    beyer_speed_figure INTEGER DEFAULT 97,
    dirt_starts INTEGER DEFAULT 0,
    dirt_wins INTEGER DEFAULT 0,
    dist_starts INTEGER DEFAULT 0,
    dist_wins INTEGER DEFAULT 0,
    wet_starts INTEGER DEFAULT 0,
    wet_wins INTEGER DEFAULT 0,
    workouts_past_30 INTEGER DEFAULT 4,
    gate_class INTEGER DEFAULT 3,
    stamina_index REAL DEFAULT 0.60,
    pace_style TEXT DEFAULT 'presser',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS horse_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_name TEXT NOT NULL UNIQUE,
    speed_score REAL,
    form_score REAL,
    distance_score REAL,
    class_score REAL,
    pace_score REAL,
    workout_score REAL,
    market_score REAL,
    composite_score REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS derby_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    horse_name TEXT NOT NULL,
    post_position INTEGER,
    morning_line_odds REAL,
    win_probability REAL,
    place_probability REAL,
    show_probability REAL,
    composite_score REAL,
    model_type TEXT,
    rank INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
