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


def _migrate_db() -> None:
    """Apply additive column migrations that CREATE TABLE IF NOT EXISTS cannot cover."""
    conn = sqlite3.connect(DB_PATH)

    cols_sr = {row[1] for row in conn.execute("PRAGMA table_info(score_runs)").fetchall()}
    if "derby_override_active" not in cols_sr:
        conn.execute(
            "ALTER TABLE score_runs ADD COLUMN "
            "derby_override_active INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        print("[migrate_db] Added score_runs.derby_override_active")

    cols_es = {row[1] for row in conn.execute("PRAGMA table_info(entry_scores)").fetchall()}
    if "low_conf_bet_block" not in cols_es:
        conn.execute(
            "ALTER TABLE entry_scores ADD COLUMN "
            "low_conf_bet_block INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        print("[migrate_db] Added entry_scores.low_conf_bet_block")

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
