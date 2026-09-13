-- Additive provenance and entry fields for validated DraftKings Markdown intake.
-- Runtime migration is performed idempotently by
-- src.services.draftkings_markdown_intake.ensure_draftkings_markdown_intake_tables
-- because SQLite lacks ADD COLUMN IF NOT EXISTS.
CREATE TABLE IF NOT EXISTS dk_markdown_imports (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_sha256 TEXT NOT NULL UNIQUE,
    source_filename TEXT NOT NULL,
    source_path TEXT,
    source_format TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    scoring_as_of TEXT NOT NULL,
    card_id INTEGER REFERENCES race_cards(card_id),
    parsed_runner_count INTEGER NOT NULL,
    past_performance_count INTEGER NOT NULL,
    workout_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS dk_markdown_import_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    source_path TEXT,
    source_format TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    scoring_as_of TEXT NOT NULL,
    card_id INTEGER REFERENCES race_cards(card_id),
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(file_sha256, parser_version)
);

CREATE TABLE IF NOT EXISTS dk_horse_profile_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id INTEGER NOT NULL REFERENCES horses(horse_id),
    card_id INTEGER NOT NULL REFERENCES race_cards(card_id),
    file_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    source_as_of TEXT NOT NULL,
    owner_name TEXT,
    breeder_name TEXT,
    age INTEGER,
    sex_raw TEXT,
    color TEXT,
    sire TEXT,
    dam TEXT,
    dam_sire TEXT,
    raw_profile TEXT,
    record_splits_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(horse_id, card_id, file_sha256, parser_version)
);
