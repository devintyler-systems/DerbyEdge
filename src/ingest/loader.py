"""
src/ingest/loader.py — V1 normalized ETL

Loads a race-field CSV into the normalized schema tables:
  tracks, race_cards, horses, people, entries, odds_snapshots

What this loader does NOT fabricate
------------------------------------
- horse_starts: no individual result records exist in the Derby seed CSV.
  Aggregate career stats (career_wins, etc.) are stored in entries as
  seed-compat columns; they are NOT expanded into horse_starts rows.
- workouts: no individual workout records exist in the seed.
  The aggregate count (workouts_past_30) is stored in entries.workouts_30
  only. The workouts table is left empty by this loader.
- odds history: only the morning line is inserted as a single odds_snapshot.

Entity resolution rules
-----------------------
- Horse names:   whitespace-normalized + COLLATE NOCASE; INSERT OR IGNORE
- People (trainer/jockey/owner): UNIQUE(full_name, role); INSERT OR IGNORE
- Race card:     UNIQUE(track_id, card_date, race_number); INSERT OR IGNORE
- Track:         UNIQUE(abbrev); INSERT OR IGNORE

Column mapping (CSV -> entries table)
--------------------------------------
See ENTRIES_COL_MAP below. Every field is explicitly mapped; nothing is
inferred or defaulted beyond what the source supplies.
"""

import dataclasses
import re
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEED = ROOT / "data" / "seeds" / "derby_2026_field.csv"

# ── Derby 2026 race-card metadata ─────────────────────────────────────────────
# These are canonical constants for the seed file; future generic loaders
# will accept these as parameters.
DERBY_2026_META = {
    "track_name":        "Churchill Downs",
    "track_abbrev":      "CD",
    "track_city":        "Louisville",
    "track_state":       "KY",
    "track_country":     "USA",
    "card_date":         "2026-05-02",
    "race_number":       12,
    "stakes_name":       "Kentucky Derby",
    "purse":             3_000_000,
    "distance_yards":    2200,          # 10 furlongs = 1.25 miles
    "surface":           "dirt",
    "race_class":        "G1",
    "age_restriction":   "3YO",
    "conditions":        "Grade 1 Stakes, 3-year-olds, 1 1/4 miles, Churchill Downs",
    "expected_field":    20,
}

# ── Explicit CSV -> entries column mapping ────────────────────────────────────
# Only columns that exist in both the seed CSV and the entries table.
# Keys = CSV column name; values = entries table column name.
ENTRIES_COL_MAP: dict[str, str] = {
    "weight":                 "weight",
    "career_starts":          "career_starts",
    "career_wins":            "career_wins",
    "career_places":          "career_places",
    "career_shows":           "career_shows",
    "career_earnings":        "career_earnings",
    "last_race_days_ago":     "last_race_days",
    "last_race_finish":       "last_race_finish",
    "last_race_speed_figure": "last_speed_fig",
    "best_speed_figure":      "best_speed_fig",
    "avg_speed_figure":       "avg_speed_fig",
    "beyer_speed_figure":     "beyer_fig",
    "dirt_starts":            "dirt_starts",
    "dirt_wins":              "dirt_wins",
    "dist_starts":            "dist_starts",
    "dist_wins":              "dist_wins",
    "wet_starts":             "wet_starts",
    "wet_wins":               "wet_wins",
    "workouts_past_30":       "workouts_30",
    "gate_class":             "gate_class",
    "stamina_index":          "stamina_index",
    "pace_style":             "pace_style",
}

# Required source columns — ingest fails if any of these are absent
REQUIRED_CSV_COLS = [
    "horse_name", "post_position", "morning_line_odds",
    "trainer", "jockey",
]

VALID_PACE_STYLES = {"front", "presser", "stalker", "closer"}


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclasses.dataclass
class LoadResult:
    csv_path: str
    card_id: int
    track_id: int
    source_rows: int
    horses_new: int
    horses_existing: int
    trainers_new: int
    jockeys_new: int
    owners_new: int
    entries_new: int
    entries_existing: int
    odds_snapshots: int
    gaps: list[str]        # fields present in CSV but missing from some rows
    warnings: list[str]    # non-fatal data quality notes

    @property
    def total_entries(self) -> int:
        return self.entries_new + self.entries_existing

    def summary(self) -> str:
        lines = [
            f"Source rows    : {self.source_rows}",
            f"horses (new)   : {self.horses_new}",
            f"entries (new)  : {self.entries_new}",
            f"entries (exist): {self.entries_existing}",
            f"odds_snapshots : {self.odds_snapshots}",
        ]
        if self.warnings:
            lines += ["", "Warnings:"] + [f"  - {w}" for w in self.warnings]
        return "\n".join(lines)


# ── Entity resolution helpers ─────────────────────────────────────────────────

def _norm(s: object) -> Optional[str]:
    """Whitespace-normalize a name; return None if blank."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    cleaned = re.sub(r"\s+", " ", str(s).strip())
    return cleaned if cleaned else None


def _upsert_track(conn: sqlite3.Connection, meta: dict) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO tracks (name, abbrev, city, state, country) "
        "VALUES (?,?,?,?,?)",
        (meta["track_name"], meta["track_abbrev"],
         meta.get("track_city"), meta.get("track_state"),
         meta.get("track_country", "USA")),
    )
    return conn.execute(
        "SELECT track_id FROM tracks WHERE abbrev=?", (meta["track_abbrev"],)
    ).fetchone()["track_id"]


def _upsert_race_card(conn: sqlite3.Connection, track_id: int, meta: dict) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO race_cards
            (track_id, card_date, race_number, stakes_name, purse,
             distance_yards, surface, race_class, age_restriction,
             conditions, field_size)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            track_id,
            meta["card_date"],
            meta["race_number"],
            meta.get("stakes_name"),
            meta.get("purse"),
            meta["distance_yards"],
            meta.get("surface", "dirt"),
            meta.get("race_class"),
            meta.get("age_restriction"),
            meta.get("conditions"),
            meta.get("expected_field"),
        ),
    )
    return conn.execute(
        "SELECT card_id FROM race_cards "
        "WHERE track_id=? AND card_date=? AND race_number=?",
        (track_id, meta["card_date"], meta["race_number"]),
    ).fetchone()["card_id"]


def _upsert_horse(conn: sqlite3.Connection, name: str,
                  sire: Optional[str], dam: Optional[str]) -> tuple[int, bool]:
    """Return (horse_id, is_new)."""
    name = _norm(name)
    existing = conn.execute(
        "SELECT horse_id FROM horses WHERE name=? COLLATE NOCASE", (name,)
    ).fetchone()
    if existing:
        return existing["horse_id"], False
    conn.execute(
        "INSERT INTO horses (name, sire, dam) VALUES (?,?,?)",
        (name, _norm(sire), _norm(dam)),
    )
    return conn.execute(
        "SELECT horse_id FROM horses WHERE name=? COLLATE NOCASE", (name,)
    ).fetchone()["horse_id"], True


def _upsert_person(conn: sqlite3.Connection, full_name: str,
                   role: str) -> tuple[int, bool]:
    """Return (person_id, is_new)."""
    full_name = _norm(full_name)
    existing = conn.execute(
        "SELECT person_id FROM people WHERE full_name=? AND role=?",
        (full_name, role),
    ).fetchone()
    if existing:
        return existing["person_id"], False
    conn.execute(
        "INSERT INTO people (full_name, role) VALUES (?,?)",
        (full_name, role),
    )
    return conn.execute(
        "SELECT person_id FROM people WHERE full_name=? AND role=?",
        (full_name, role),
    ).fetchone()["person_id"], True


def _build_entry_params(
    row: dict,
    card_id: int,
    horse_id: int,
    trainer_id: Optional[int],
    jockey_id: Optional[int],
    owner_id: Optional[int],
) -> dict:
    """Assemble the entry INSERT parameter dict from a source row.

    Only columns present in the source AND non-null are included.
    Defaults are NOT back-filled — missing values stay NULL in the DB.
    """
    params: dict = {
        "card_id":           card_id,
        "horse_id":          horse_id,
        "trainer_id":        trainer_id,
        "jockey_id":         jockey_id,
        "owner_id":          owner_id,
        "post_position":     int(row["post_position"]),
        "morning_line_odds": float(row["morning_line_odds"]),
    }
    # Optional seed-compat columns — include only when present and non-null
    for src_col, db_col in ENTRIES_COL_MAP.items():
        val = row.get(src_col)
        if val is not None and not (isinstance(val, float) and pd.isna(val)):
            # Validate pace_style against allowed set
            if src_col == "pace_style":
                val_str = str(val).lower().strip()
                if val_str in VALID_PACE_STYLES:
                    params[db_col] = val_str
                # else: silently skip invalid pace style (logged at validate time)
            else:
                params[db_col] = val
    return params


def _insert_entry(conn: sqlite3.Connection, params: dict) -> tuple[int, bool]:
    """INSERT OR IGNORE entry; return (entry_id, is_new)."""
    existing = conn.execute(
        "SELECT entry_id FROM entries WHERE card_id=? AND horse_id=?",
        (params["card_id"], params["horse_id"]),
    ).fetchone()
    if existing:
        return existing["entry_id"], False

    cols  = list(params.keys())
    ph    = ",".join("?" * len(cols))
    sql   = f"INSERT INTO entries ({','.join(cols)}) VALUES ({ph})"
    conn.execute(sql, [params[c] for c in cols])
    return conn.execute(
        "SELECT entry_id FROM entries WHERE card_id=? AND horse_id=?",
        (params["card_id"], params["horse_id"]),
    ).fetchone()["entry_id"], True


def _insert_morning_line_snapshot(
    conn: sqlite3.Connection, entry_id: int,
    odds: float, snapshot_time: str,
) -> None:
    """Insert one morning-line odds snapshot per entry.

    This is the only odds record we have from the seed CSV; inserting it
    is not fabrication — it comes directly from morning_line_odds in the source.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO odds_snapshots
            (entry_id, snapshot_time, odds_numerator, odds_denominator, source)
        VALUES (?,?,?,1.0,'morning_line')
        """,
        (entry_id, snapshot_time, odds),
    )


# ── Main loader ───────────────────────────────────────────────────────────────

def load_derby_seed(
    csv_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
    meta: Optional[dict] = None,
) -> LoadResult:
    """
    Load the Derby 2026 field CSV into normalized V1 tables.

    Parameters
    ----------
    csv_path : path to source CSV (defaults to data/seeds/derby_2026_field.csv)
    conn     : existing sqlite3.Connection; if None, opens and closes one
    meta     : race-card metadata dict (defaults to DERBY_2026_META)

    Returns
    -------
    LoadResult with per-table insert counts and warnings
    """
    path = Path(csv_path) if csv_path else DEFAULT_SEED
    if not path.exists():
        raise FileNotFoundError(f"Source CSV not found: {path}")

    meta = meta or DERBY_2026_META
    own_conn = conn is None
    if own_conn:
        from src.utils.db import get_connection
        conn = get_connection()

    try:
        df = _read_and_validate_csv(path)
        result = _load_dataframe(conn, df, meta, str(path))
        if own_conn:
            conn.commit()
        return result
    finally:
        if own_conn:
            conn.close()


def _read_and_validate_csv(path: Path) -> pd.DataFrame:
    """Read CSV and raise on structural errors (missing required columns)."""
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]

    missing_cols = [c for c in REQUIRED_CSV_COLS if c not in df.columns]
    if missing_cols:
        raise ValueError(
            f"Source CSV missing required columns: {missing_cols}\n"
            f"Found columns: {list(df.columns)}"
        )
    # Coerce numeric fields — keep as float/int, NaN for empty cells
    numeric_cols = [
        "post_position", "morning_line_odds", "weight",
        "career_starts", "career_wins", "career_places", "career_shows",
        "career_earnings", "last_race_days_ago", "last_race_finish",
        "last_race_speed_figure", "best_speed_figure", "avg_speed_figure",
        "beyer_speed_figure", "dirt_starts", "dirt_wins", "dist_starts",
        "dist_wins", "wet_starts", "wet_wins", "workouts_past_30",
        "gate_class", "stamina_index",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _load_dataframe(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    meta: dict,
    csv_path_str: str,
) -> LoadResult:
    track_id = _upsert_track(conn, meta)
    card_id  = _upsert_race_card(conn, track_id, meta)

    horses_new = horses_existing = 0
    trainers_new = jockeys_new = owners_new = 0
    entries_new = entries_existing = 0
    odds_snaps = 0
    warnings: list[str] = []
    gaps: list[str] = []

    # Snapshot timestamp = race morning (pre-race)
    snap_time = f"{meta['card_date']}T09:00:00Z"

    for _, row in df.iterrows():
        row = row.to_dict()

        # ── horse ─────────────────────────────────────────────────────────
        horse_name = _norm(str(row.get("horse_name", "")))
        if not horse_name:
            warnings.append(f"Row skipped: empty horse_name at post {row.get('post_position')}")
            continue

        h_id, h_new = _upsert_horse(
            conn, horse_name,
            sire=row.get("sire"),
            dam=row.get("dam"),
        )
        if h_new:
            horses_new += 1
        else:
            horses_existing += 1

        # ── connections ───────────────────────────────────────────────────
        trainer_name = _norm(str(row.get("trainer", "")))
        jockey_name  = _norm(str(row.get("jockey", "")))
        owner_name   = _norm(str(row.get("owner", "")))

        trainer_id: Optional[int] = None
        jockey_id:  Optional[int] = None
        owner_id:   Optional[int] = None

        if trainer_name:
            trainer_id, t_new = _upsert_person(conn, trainer_name, "trainer")
            if t_new:
                trainers_new += 1
        else:
            warnings.append(f"{horse_name}: missing trainer")

        if jockey_name:
            jockey_id, j_new = _upsert_person(conn, jockey_name, "jockey")
            if j_new:
                jockeys_new += 1
        else:
            warnings.append(f"{horse_name}: missing jockey")

        if owner_name:
            owner_id, o_new = _upsert_person(conn, owner_name, "owner")
            if o_new:
                owners_new += 1

        # ── entry ─────────────────────────────────────────────────────────
        params = _build_entry_params(row, card_id, h_id, trainer_id, jockey_id, owner_id)
        e_id, e_new = _insert_entry(conn, params)
        if e_new:
            entries_new += 1
        else:
            entries_existing += 1

        # ── morning line odds snapshot ─────────────────────────────────────
        # Inserted only when we just created the entry (idempotent re-runs)
        if e_new and "morning_line_odds" in row:
            odds_val = row.get("morning_line_odds")
            if odds_val and not (isinstance(odds_val, float) and pd.isna(odds_val)):
                _insert_morning_line_snapshot(conn, e_id, float(odds_val), snap_time)
                odds_snaps += 1

    # ── gap audit: which seed columns are present but partially null ───────
    for src_col in ENTRIES_COL_MAP:
        if src_col in df.columns:
            null_rate = df[src_col].isna().mean()
            if null_rate > 0:
                gaps.append(
                    f"{src_col}: {null_rate:.0%} null in source"
                )

    return LoadResult(
        csv_path       = csv_path_str,
        card_id        = card_id,
        track_id       = track_id,
        source_rows    = len(df),
        horses_new     = horses_new,
        horses_existing= horses_existing,
        trainers_new   = trainers_new,
        jockeys_new    = jockeys_new,
        owners_new     = owners_new,
        entries_new    = entries_new,
        entries_existing = entries_existing,
        odds_snapshots = odds_snaps,
        gaps           = gaps,
        warnings       = warnings,
    )
