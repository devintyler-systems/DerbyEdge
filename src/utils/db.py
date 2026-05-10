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

def _migrate_db() -> None:
    """Apply all additive column migrations.  Safe to re-run (idempotent)."""
    conn = sqlite3.connect(DB_PATH)
    ensure_score_runs_columns(conn)
    ensure_entry_scores_columns(conn)
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
