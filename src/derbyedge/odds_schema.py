"""Odds-layer schema additions for DerbyEdge.

Separate file from schema.py so v0 DBs can be migrated forward without rebuild.
Tables:
    markets          - Sportsbook / pool registry
    odds_snapshots   - Time-series of odds per (race, entry, book)
    odds_features    - Per-entry derived features (current best, drift, devig prob)

Design rules:
- One snapshot row per (book, race_id, program_number, timestamp).
- Decimal odds are canonical; American odds derived on read.
- Devig prob computed at the SNAPSHOT level for a (book, race) pair, not stored
  per-row (it's a function of all runners in that book/race at that time).
- We never overwrite a snapshot row. Odds drift = comparing snapshots over time.
"""

ODDS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS markets (
    book_id        TEXT PRIMARY KEY,    -- 'fanduel','draftkings','twinspires','churchill','morningline'
    book_name      TEXT,
    book_type      TEXT,                -- 'fixed','pari-mutuel','morning-line'
    region         TEXT,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at     TEXT NOT NULL,       -- ISO-8601 UTC
    book_id         TEXT NOT NULL,
    race_id         TEXT NOT NULL,
    program_number  TEXT NOT NULL,
    entry_id        TEXT,                -- nullable until we resolve it
    decimal_odds    REAL,                -- 1.0 = even money breakeven; null = not offered
    american_odds   INTEGER,             -- mirror, optional
    is_scratched    INTEGER DEFAULT 0,
    is_morning_line INTEGER DEFAULT 0,
    raw_payload     TEXT,                -- optional JSON for debugging
    FOREIGN KEY (book_id) REFERENCES markets(book_id),
    FOREIGN KEY (race_id) REFERENCES races(race_id)
);

CREATE INDEX IF NOT EXISTS ix_odds_race_book ON odds_snapshots(race_id, book_id);
CREATE INDEX IF NOT EXISTS ix_odds_entry ON odds_snapshots(entry_id);
CREATE INDEX IF NOT EXISTS ix_odds_time ON odds_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS odds_features (
    entry_id            TEXT PRIMARY KEY,
    race_id             TEXT,
    morning_line_dec    REAL,
    morning_line_prob   REAL,
    best_dec_now        REAL,            -- best (highest) decimal currently offered across books
    best_book_now       TEXT,
    median_dec_now      REAL,
    market_prob_devig   REAL,            -- average devigged win prob across books
    publicness_score    REAL,            -- 0-10, derived from market_prob_devig rank vs equal-mass
    odds_drift_pct      REAL,            -- (current_best - opening_best) / opening_best
    drift_direction     TEXT,            -- 'shortening','drifting','flat'
    n_books             INTEGER,
    last_updated_at     TEXT,
    FOREIGN KEY (entry_id) REFERENCES entries(entry_id)
);

-- Seed canonical books (idempotent)
INSERT OR IGNORE INTO markets (book_id, book_name, book_type, region) VALUES
    ('morningline',  'Morning Line',         'morning-line',  'USA'),
    ('fanduel',      'FanDuel Sportsbook',   'fixed',         'USA'),
    ('draftkings',   'DraftKings Sportsbook','fixed',         'USA'),
    ('twinspires',   'TwinSpires (CDI)',     'pari-mutuel',   'USA'),
    ('churchill',    'Churchill Downs Pool', 'pari-mutuel',   'USA'),
    ('betmgm',       'BetMGM',               'fixed',         'USA');
"""


def init_odds_schema(conn):
    conn.executescript(ODDS_SCHEMA_SQL)
    conn.commit()
