import sqlite3
from pathlib import Path

ROOT        = Path(__file__).resolve().parents[2]
DB_PATH     = ROOT / "db" / "derbyedge.db"
SCHEMA_PATH = ROOT / "db" / "schema.sql"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create all V1 tables from schema.sql.  Safe to re-run (IF NOT EXISTS)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)   # plain connect; executescript handles pragmas
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    _migrate_db()
    print(f"[init_db] V1 schema applied at {DB_PATH}")


# ---------------------------------------------------------------------------
# Column-presence helpers (PRAGMA-based, never raises on existing columns)
# ---------------------------------------------------------------------------

def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return set of column names for *table* using PRAGMA table_info."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_col_if_missing(
    conn: sqlite3.Connection,
    table: str,
    col: str,
    col_type: str,
    existing: set[str],
) -> bool:
    """ALTER TABLE … ADD COLUMN when col is absent. Returns True if column was added."""
    if col in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
    existing.add(col)
    return True


# ---------------------------------------------------------------------------
# Canonical schema-ensure functions — public, reused by app + scorer
# ---------------------------------------------------------------------------

def ensure_score_runs_columns(conn: sqlite3.Connection) -> None:
    """Idempotent: ensure score_runs has all columns for the current schema."""
    cols = _table_cols(conn, "score_runs")
    changed = any([
        _add_col_if_missing(conn, "score_runs", "derby_override_active",
                            "INTEGER NOT NULL DEFAULT 0", cols),
        _add_col_if_missing(conn, "score_runs", "chaos_active",
                            "INTEGER NOT NULL DEFAULT 0", cols),
        _add_col_if_missing(conn, "score_runs", "chaos_intensity",    "REAL", cols),
        _add_col_if_missing(conn, "score_runs", "field_entropy_score", "REAL", cols),
        _add_col_if_missing(conn, "score_runs", "quality_tier",        "TEXT", cols),
        _add_col_if_missing(conn, "score_runs", "effective_run_mode",  "TEXT", cols),
        _add_col_if_missing(conn, "score_runs", "model_collapse_status", "TEXT", cols),
        _add_col_if_missing(conn, "score_runs", "max_abs_model_ml_delta", "REAL", cols),
        _add_col_if_missing(conn, "score_runs", "mean_abs_model_ml_delta", "REAL", cols),
        _add_col_if_missing(conn, "score_runs", "displayed_model_assigned_from_market",
                            "INTEGER NOT NULL DEFAULT 0", cols),
        _add_col_if_missing(conn, "score_runs", "uncalibrated_entropy", "REAL", cols),
        _add_col_if_missing(conn, "score_runs", "calibrated_entropy", "REAL", cols),
        _add_col_if_missing(conn, "score_runs", "selected_temperature", "REAL", cols),
        _add_col_if_missing(conn, "score_runs", "morning_line_available", "INTEGER", cols),
        _add_col_if_missing(conn, "score_runs", "market_prior_source", "TEXT", cols),
        _add_col_if_missing(conn, "score_runs", "divergence_from_morning_line", "REAL", cols),
        _add_col_if_missing(conn, "score_runs", "calibration_status", "TEXT", cols),
        _add_col_if_missing(conn, "score_runs", "dispatcher_mode", "TEXT", cols),
        _add_col_if_missing(conn, "score_runs", "dispatcher_reason_codes", "TEXT", cols),
    ])
    if changed:
        conn.commit()


def ensure_entry_scores_columns(conn: sqlite3.Connection) -> None:
    """Idempotent: ensure entry_scores has all columns for the current schema.

    Uses PRAGMA table_info so it never raises on already-existing columns.
    Safe to call at every app startup and before every scoring write.

    Also backfills confidence_bucket from the legacy confidence_flag for
    rows that pre-date the scored confidence system.
    """
    cols = _table_cols(conn, "entry_scores")

    # All additive entry_scores columns in chronological rollout order
    additions: list[tuple[str, str]] = [
        # ── original columns (should always exist, but guard anyway) ──
        ("confidence_flag",     "INTEGER NOT NULL DEFAULT 0"),
        ("missing_data_flag",   "INTEGER NOT NULL DEFAULT 0"),
        # ── low_conf_bet_block rollout ──
        ("low_conf_bet_block",  "INTEGER NOT NULL DEFAULT 0"),
        # ── chaos rollout ──
        ("chaos_score",         "REAL"),
        ("chaos_boost",         "REAL"),
        ("chaos_tier",          "TEXT"),
        ("chaos_eligible",      "INTEGER NOT NULL DEFAULT 0"),
        # ── confidence v2 rollout ──
        ("confidence_score",    "REAL"),
        ("confidence_bucket",   "TEXT"),
        ("confidence_reasons",  "TEXT"),
        # ── probability provenance / market-independence rollout ──
        ("p_ml_implied",        "REAL"),
        ("p_signal_pre_market", "REAL"),
        ("p_model_pre_market",  "REAL"),
        ("p_market_live",       "REAL"),
        ("p_model_blended",     "REAL"),
        ("edge_vs_live_market", "REAL"),
    ]

    changed = False
    for col_name, col_type in additions:
        if _add_col_if_missing(conn, "entry_scores", col_name, col_type, cols):
            changed = True

    if changed:
        conn.commit()

    # Backfill confidence_bucket for existing rows that pre-date the v2 rollout.
    # Prior semantics: confidence_flag=0 → LOW, confidence_flag=1 → MEDIUM.
    conn.execute(
        """
        UPDATE entry_scores
        SET    confidence_bucket = CASE WHEN confidence_flag = 0 THEN 'LOW' ELSE 'MEDIUM' END
        WHERE  confidence_bucket IS NULL
        """
    )
    conn.commit()


def entry_scores_cols(conn: sqlite3.Connection) -> set[str]:
    """Return the current set of column names in entry_scores.

    Callers (e.g. load_board) use this to build safe, version-aware SELECT lists.
    """
    return _table_cols(conn, "entry_scores")


# ---------------------------------------------------------------------------
# Internal migration (called by init_db; also consolidated into ensure_* above)
# ---------------------------------------------------------------------------

def ensure_starter_observations(conn: sqlite3.Connection) -> None:
    """Idempotent: ensure starter_observations table and its indexes exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS starter_observations (
            obs_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id                INTEGER NOT NULL,
            race_date              TEXT    NOT NULL,
            track                  TEXT    NOT NULL,
            race_no                INTEGER NOT NULL,
            surface                TEXT,
            distance_furlongs      REAL,
            distance_bucket        TEXT,
            field_size             INTEGER,
            horse                  TEXT    NOT NULL,
            post                   INTEGER,
            trainer                TEXT,
            jockey                 TEXT,
            ml_odds                REAL,
            pred_win_prob          REAL,
            pred_fair_odds         REAL,
            pred_rank              INTEGER,
            edge                   REAL,
            tag                    TEXT,
            pace_fit               REAL,
            form_score             REAL,
            sudist_fit             REAL,
            chaos_pct              REAL,
            tier                   TEXT,
            scratched              INTEGER NOT NULL DEFAULT 0,
            finish_pos             INTEGER,
            win_flag               INTEGER,
            off_odds               REAL,
            model_version          TEXT,
            source_prediction_file TEXT,
            source_result_file     TEXT,
            created_at             TEXT NOT NULL
                       DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(race_id, post)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_race_date
            ON starter_observations(race_date);
        CREATE INDEX IF NOT EXISTS idx_obs_track_date
            ON starter_observations(track, race_date);
    """)
    conn.commit()


def ensure_horse_starts_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add canonical pre-race history/provenance columns."""
    cols = _table_cols(conn, "horse_starts")
    additions = [
        ("field_size_last", "INTEGER"),
        ("start_date", "TEXT"),
        ("track_code", "TEXT"),
        ("race_class_raw", "TEXT"),
        ("distance_furlongs", "REAL"),
        ("surface", "TEXT"),
        ("historical_odds_raw", "TEXT"),
        ("historical_odds_type", "TEXT"),
        ("is_scratch", "INTEGER NOT NULL DEFAULT 0"),
        ("source_provider", "TEXT"),
        ("source_document_id", "TEXT"),
        ("source_row_id", "TEXT"),
    ]
    changed = False
    for name, kind in additions:
        changed = _add_col_if_missing(conn, "horse_starts", name, kind, cols) or changed
    if changed:
        conn.commit()


def ensure_workouts_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add workout provenance and source-rank columns."""
    cols = _table_cols(conn, "workouts")
    additions = [
        ("location_label", "TEXT"),
        ("source_rank", "INTEGER"),
        ("source_provider", "TEXT"),
        ("source_document_id", "TEXT"),
        ("source_row_id", "TEXT"),
    ]
    changed = False
    for name, kind in additions:
        changed = _add_col_if_missing(conn, "workouts", name, kind, cols) or changed
    if changed:
        conn.commit()


def ensure_race_cards_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add an optional UTC post time for market validation."""
    cols = _table_cols(conn, "race_cards")
    if _add_col_if_missing(
        conn, "race_cards", "scheduled_post_time_utc", "TEXT", cols
    ):
        conn.commit()


def ensure_model_registry_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add dispatcher/promotion audit metadata."""
    cols = _table_cols(conn, "model_registry")
    additions = [
        ("dispatcher_mode", "TEXT"),
        ("dispatcher_reason_codes", "TEXT"),
        ("completed_races", "INTEGER"),
        ("labeled_starters", "INTEGER"),
        ("rolling_validation_folds", "INTEGER"),
        ("core_feature_coverage", "REAL"),
        ("feature_schema_version", "TEXT"),
        ("calibration_artifact_path", "TEXT"),
        ("baseline_log_loss", "REAL"),
        ("baseline_brier_score", "REAL"),
        ("calibration_acceptable", "INTEGER"),
        ("field_size_regression_acceptable", "INTEGER"),
        ("target_race_type_key", "TEXT"),
        ("training_window_start", "TEXT"),
        ("training_window_end", "TEXT"),
    ]
    changed = False
    for name, kind in additions:
        changed = _add_col_if_missing(conn, "model_registry", name, kind, cols) or changed
    if changed:
        conn.commit()


def ensure_feature_store_columns(conn: sqlite3.Connection) -> None:
    """Idempotent: add post-schema feature observability columns if absent."""
    cols = _table_cols(conn, "feature_store")
    t1_cols: list[tuple[str, str]] = [
        ("speed_fig_adj",            "REAL"),
        ("layoff_bucket_encoded",    "REAL"),
        ("class_level",              "REAL"),
        ("class_delta_v2",           "REAL"),
        ("horses_beaten_pct_actual", "REAL"),
        ("pace_pressure_tier",       "INTEGER"),
        ("collapse_risk_v2",         "REAL"),
        ("morning_line_delta",       "REAL"),
        ("run_style_evidence_count", "INTEGER"),
        ("run_style_source",         "TEXT"),
        ("pace_band",                "TEXT"),
        ("classified_runner_count",  "INTEGER"),
        ("active_runner_count",      "INTEGER"),
        ("pace_state",               "TEXT"),
        ("recent_finish_percentile_w", "REAL"),
        ("recent_finish_evidence_count", "INTEGER"),
        ("starts_last_90d", "INTEGER"),
        ("form_class_coverage", "REAL"),
        ("class_delta_last_to_today", "REAL"),
        ("class_delta_confidence", "TEXT"),
        ("last_class_label_raw", "TEXT"),
        ("today_class_label_raw", "TEXT"),
        ("distance_fit_eb", "REAL"),
        ("surface_fit_eb", "REAL"),
        ("surface_distance_finish_percentile_w", "REAL"),
        ("distance_fit_n", "INTEGER"),
        ("surface_fit_n", "INTEGER"),
        ("surface_distance_start_count", "INTEGER"),
        ("distance_surface_coverage", "REAL"),
        ("days_since_last_workout", "INTEGER"),
        ("workout_cadence_30d", "INTEGER"),
        ("workout_count_30d", "INTEGER"),
        ("workout_readiness_score_v2", "REAL"),
        ("readiness_coverage", "REAL"),
        ("workout_time_normalization_available", "INTEGER"),
        ("workout_data_source", "TEXT"),
        ("historical_scratch_rate", "REAL"),
        ("historical_scratch_n", "INTEGER"),
        ("historical_scratch_confidence", "TEXT"),
        ("prior_publicness", "REAL"),
        ("prior_publicness_n", "INTEGER"),
        ("dk_history_start_count", "INTEGER"),
        ("dk_workout_count", "INTEGER"),
        ("feature_source_mix", "TEXT"),
        ("market_implied_prob_source", "TEXT"),
    ]
    changed = False
    for col_name, col_type in t1_cols:
        if _add_col_if_missing(conn, "feature_store", col_name, col_type, cols):
            changed = True
    if changed:
        conn.commit()


def ensure_v_entries_live(conn: sqlite3.Connection) -> None:
    """Idempotent: recreate v_entries_live with field_size_last if the column is absent."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(v_entries_live)").fetchall()}
    if "field_size_last" in cols:
        return
    conn.execute("DROP VIEW IF EXISTS v_entries_live")
    conn.executescript("""
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
    """)
    conn.commit()


def ensure_race_eval_log(conn: sqlite3.Connection) -> None:
    """Idempotent: ensure race_eval_log table, indexes, and reporting views exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS race_eval_log (
            eval_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file       TEXT    NOT NULL,
            source_row_num    INTEGER NOT NULL,
            import_batch_ts   TEXT    NOT NULL,
            race_id           INTEGER,
            race_date         TEXT    NOT NULL,
            track_code        TEXT    NOT NULL,
            race_number       INTEGER NOT NULL,
            surface           TEXT,
            distance_text     TEXT,
            distance_f        REAL,
            field_size        INTEGER,
            orig_tp_raw       TEXT,
            orig_tp_name      TEXT,
            orig_tp_norm      TEXT,
            orig_tp_scratched INTEGER NOT NULL DEFAULT 0,
            eff_tp_raw        TEXT,
            eff_tp_name       TEXT,
            eff_tp_norm       TEXT,
            eff_tp_finish_text TEXT,
            eff_tp_finish_pos INTEGER,
            eff_tp_won        INTEGER NOT NULL DEFAULT 0,
            winner_raw        TEXT,
            winner_name       TEXT,
            winner_norm       TEXT,
            chaos_raw         TEXT,
            chaos_active      INTEGER NOT NULL DEFAULT 0,
            tier_name         TEXT,
            match_status      TEXT    NOT NULL DEFAULT 'UNMATCHED',
            match_notes       TEXT,
            created_ts        TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(source_file, source_row_num)
        );

        CREATE INDEX IF NOT EXISTS idx_race_eval_log_race_key
            ON race_eval_log(race_date, track_code, race_number);
        CREATE INDEX IF NOT EXISTS idx_race_eval_log_race_id
            ON race_eval_log(race_id);
        CREATE INDEX IF NOT EXISTS idx_race_eval_log_tier
            ON race_eval_log(tier_name);
        CREATE INDEX IF NOT EXISTS idx_race_eval_log_surface
            ON race_eval_log(surface);

        CREATE VIEW IF NOT EXISTS v_race_eval_tool AS
        SELECT
            rel.eval_id, rel.source_file, rel.import_batch_ts,
            rel.race_id, rel.race_date, rel.track_code, rel.race_number,
            rel.surface, rel.distance_text, rel.distance_f, rel.field_size,
            rel.orig_tp_name, rel.orig_tp_scratched,
            rel.eff_tp_name, rel.eff_tp_finish_text, rel.eff_tp_finish_pos,
            rel.eff_tp_won, rel.winner_name, rel.chaos_active,
            rel.tier_name, rel.match_status, rel.match_notes
        FROM race_eval_log rel;

        CREATE VIEW IF NOT EXISTS v_race_eval_tool_enriched AS
        SELECT
            rel.eval_id, rel.source_file, rel.import_batch_ts,
            rel.race_id, rel.race_date, rel.track_code, rel.race_number,
            rel.surface, rel.distance_text, rel.distance_f, rel.field_size,
            rel.orig_tp_name, rel.orig_tp_scratched,
            rel.eff_tp_name, rel.eff_tp_finish_text, rel.eff_tp_finish_pos,
            rel.eff_tp_won, rel.winner_name, rel.chaos_active,
            rel.tier_name, rel.match_status, rel.match_notes,
            t.name  AS track_name_canonical,
            rc.stakes_name, rc.purse, rc.race_class,
            rc.distance_furlongs AS rc_distance_furlongs,
            rc.surface           AS rc_surface,
            CASE WHEN rc.distance_furlongs IS NOT NULL
                 THEN CASE WHEN rc.distance_furlongs < 8.5 THEN 'sprint' ELSE 'route' END
                 ELSE NULL END   AS dist_category
        FROM race_eval_log rel
        LEFT JOIN race_cards rc ON rc.card_id = rel.race_id
        LEFT JOIN tracks     t  ON t.track_id = rc.track_id;
    """)
    conn.commit()


def _migrate_db() -> None:
    """Apply all additive column migrations.  Safe to re-run (idempotent)."""
    conn = sqlite3.connect(DB_PATH)
    ensure_score_runs_columns(conn)
    ensure_entry_scores_columns(conn)
    ensure_starter_observations(conn)
    ensure_horse_starts_columns(conn)
    ensure_workouts_columns(conn)
    ensure_race_cards_columns(conn)
    ensure_feature_store_columns(conn)
    ensure_model_registry_columns(conn)
    ensure_v_entries_live(conn)
    ensure_race_eval_log(conn)
    conn.close()


def get_derby_card_id(stakes_name: str = "Kentucky Derby") -> int | None:
    """Return the card_id for the first matching stakes race, or None."""
    conn = get_connection()
    row  = conn.execute(
        "SELECT card_id FROM race_cards WHERE stakes_name=? LIMIT 1",
        (stakes_name,),
    ).fetchone()
    conn.close()
    return row["card_id"] if row else None
