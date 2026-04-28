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
    print(f"[init_db] V1 schema applied at {DB_PATH}")


def get_derby_card_id(stakes_name: str = "Kentucky Derby") -> int | None:
    """Return the card_id for the first matching stakes race, or None."""
    conn = get_connection()
    row  = conn.execute(
        "SELECT card_id FROM race_cards WHERE stakes_name=? LIMIT 1",
        (stakes_name,),
    ).fetchone()
    conn.close()
    return row["card_id"] if row else None
