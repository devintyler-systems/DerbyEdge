"""
migrate_schema.py  —  DerbyEdge V0 -> V1 schema migration.

What this does
--------------
1.  Backs up derbyedge.db -> derbyedge_v0_backup.db
2.  Applies the new V1 DDL (CREATE TABLE IF NOT EXISTS — safe to re-run)
3.  Migrates legacy data:
      derby_field       -> tracks / race_cards / horses / people / entries
      workouts_past_30  -> synthetic workouts (marked synthetic=1)
      derby_predictions -> score_runs + entry_scores
4.  Writes output/migration_report.txt
5.  (Optional) Drops legacy tables with --drop-legacy flag

Run
---
    python scripts/migrate_schema.py
    python scripts/migrate_schema.py --drop-legacy
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH     = ROOT / "db"    / "derbyedge.db"
BACKUP_PATH = ROOT / "db"    / "derbyedge_v0_backup.db"
SCHEMA_PATH = ROOT / "db"    / "schema.sql"
REPORT_PATH = ROOT / "output" / "migration_report.txt"

# 2026 Kentucky Derby constants
DERBY_DATE          = "2026-05-02"
DERBY_RACE_NUMBER   = 12
DERBY_DISTANCE_YD   = 2200       # 10 furlongs = 1.25 miles
DERBY_SURFACE       = "dirt"
DERBY_STAKES        = "Kentucky Derby"
DERBY_PURSE         = 3_000_000
DERBY_CLASS         = "G1"
DERBY_AGE           = "3YO"
DERBY_CONDITIONS    = "Grade 1 Stakes, 3-year-olds, 1 1/4 miles, Churchill Downs"

LEGACY_TABLES = [
    "derby_field", "horse_features", "derby_predictions",
    "races", "race_entries",
    # 'horses' is re-used with new schema; keep unless flag given
]


# ── helpers ──────────────────────────────────────────────────────────────────

def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(conn, name):
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def col_exists(conn, table, col):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return col in cols


# ── steps ────────────────────────────────────────────────────────────────────

def step_backup():
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print(f"  [backup]  {BACKUP_PATH.name}")
    else:
        print("  [backup]  No existing DB — nothing to back up.")


def step_apply_schema(conn):
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    print("  [schema]  V1 DDL applied.")


def _upsert_person(conn, full_name, role):
    conn.execute(
        "INSERT OR IGNORE INTO people (full_name, role) VALUES (?,?)",
        (full_name, role),
    )
    return conn.execute(
        "SELECT person_id FROM people WHERE full_name=? AND role=?",
        (full_name, role),
    ).fetchone()["person_id"]


def step_migrate_derby_field(conn):
    if not table_exists(conn, "derby_field"):
        print("  [derby_field]  Table not found — skipping.")
        return 0, None, None

    # ── track ────────────────────────────────────────────────────────────────
    conn.execute(
        "INSERT OR IGNORE INTO tracks (name, abbrev, city, state) "
        "VALUES ('Churchill Downs','CD','Louisville','KY')"
    )
    track_id = conn.execute(
        "SELECT track_id FROM tracks WHERE abbrev='CD'"
    ).fetchone()["track_id"]

    # ── race card ────────────────────────────────────────────────────────────
    conn.execute(
        """
        INSERT OR IGNORE INTO race_cards
            (track_id, card_date, race_number, stakes_name, purse,
             distance_yards, surface, race_class, age_restriction,
             conditions, field_size)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            track_id, DERBY_DATE, DERBY_RACE_NUMBER, DERBY_STAKES,
            DERBY_PURSE, DERBY_DISTANCE_YD, DERBY_SURFACE,
            DERBY_CLASS, DERBY_AGE, DERBY_CONDITIONS, 20,
        ),
    )
    card_id = conn.execute(
        "SELECT card_id FROM race_cards "
        "WHERE track_id=? AND card_date=? AND race_number=?",
        (track_id, DERBY_DATE, DERBY_RACE_NUMBER),
    ).fetchone()["card_id"]

    # ── horses / people / entries ─────────────────────────────────────────────
    rows = conn.execute(
        "SELECT * FROM derby_field ORDER BY post_position"
    ).fetchall()

    # Column map — old column names might differ
    def g(row, *keys, default=None):
        d = dict(row)
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return default

    migrated = 0
    for row in rows:
        name = g(row, "horse_name")

        # horses
        conn.execute(
            "INSERT OR IGNORE INTO horses (name, sire, dam) VALUES (?,?,?)",
            (name, g(row, "sire"), g(row, "dam")),
        )
        horse_id = conn.execute(
            "SELECT horse_id FROM horses WHERE name=? COLLATE NOCASE", (name,)
        ).fetchone()["horse_id"]

        trainer_id = _upsert_person(conn, g(row, "trainer", default="Unknown"), "trainer") \
                     if g(row, "trainer") else None
        jockey_id  = _upsert_person(conn, g(row, "jockey",  default="Unknown"), "jockey") \
                     if g(row, "jockey")  else None
        owner_id   = _upsert_person(conn, g(row, "owner",   default="Unknown"), "owner") \
                     if g(row, "owner")   else None

        conn.execute(
            """
            INSERT OR IGNORE INTO entries (
                card_id, horse_id, trainer_id, jockey_id, owner_id,
                post_position, weight, morning_line_odds,
                career_starts, career_wins, career_places, career_shows,
                career_earnings, dirt_starts, dirt_wins,
                dist_starts, dist_wins, wet_starts, wet_wins,
                last_race_days, last_race_finish,
                last_speed_fig, best_speed_fig, avg_speed_fig, beyer_fig,
                workouts_30, gate_class, stamina_index, pace_style
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                card_id, horse_id, trainer_id, jockey_id, owner_id,
                g(row, "post_position"), g(row, "weight", default=126),
                g(row, "morning_line_odds"),
                g(row, "career_starts"),    g(row, "career_wins"),
                g(row, "career_places"),    g(row, "career_shows"),
                g(row, "career_earnings"),  g(row, "dirt_starts"),
                g(row, "dirt_wins"),        g(row, "dist_starts"),
                g(row, "dist_wins"),        g(row, "wet_starts"),
                g(row, "wet_wins"),         g(row, "last_race_days_ago"),
                g(row, "last_race_finish"), g(row, "last_race_speed_figure"),
                g(row, "best_speed_figure"), g(row, "avg_speed_figure"),
                g(row, "beyer_speed_figure"), g(row, "workouts_past_30"),
                g(row, "gate_class"),        g(row, "stamina_index"),
                g(row, "pace_style"),
            ),
        )

        # synthetic workouts
        n_wk = int(g(row, "workouts_past_30", default=0) or 0)
        if n_wk > 0:
            grade = "G" if int(g(row, "gate_class", default=3) or 3) >= 4 else "N"
            base  = date.fromisoformat(DERBY_DATE)
            for i in range(n_wk):
                wdate = (base - timedelta(days=7 * (i + 1))).isoformat()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO workouts
                        (horse_id, workout_date, track_id, distance_furlongs,
                         time_seconds, work_grade, surface, synthetic)
                    VALUES (?,?,?,?,?,?,?,1)
                    """,
                    (horse_id, wdate, track_id, 5.0, 60.8, grade, "dirt"),
                )
        migrated += 1

    conn.commit()
    print(f"  [derby_field]  {migrated} horses -> horses/people/entries/workouts.")
    return migrated, card_id, track_id


def step_migrate_predictions(conn, card_id):
    if card_id is None or not table_exists(conn, "derby_predictions"):
        print("  [predictions]  Nothing to migrate.")
        return 0

    rows = conn.execute("SELECT * FROM derby_predictions").fetchall()
    if not rows:
        return 0

    migrated = 0
    seen_runs = {}

    for r in rows:
        r = dict(r)
        run_id = r.get("run_id", "legacy_run_01")

        if run_id not in seen_runs:
            conn.execute(
                "INSERT OR IGNORE INTO score_runs "
                "(run_id, card_id, model_type) VALUES (?,?,?)",
                (run_id, card_id, r.get("model_type", "fallback")),
            )
            seen_runs[run_id] = True

        horse_row = conn.execute(
            "SELECT horse_id FROM horses WHERE name=? COLLATE NOCASE",
            (r["horse_name"],),
        ).fetchone()
        if not horse_row:
            continue
        entry_row = conn.execute(
            "SELECT entry_id FROM entries WHERE card_id=? AND horse_id=?",
            (card_id, horse_row["horse_id"]),
        ).fetchone()
        if not entry_row:
            continue

        # old storage was percentage; convert to fraction
        win_p   = r.get("win_probability", 0) or 0
        place_p = r.get("place_probability", 0) or 0
        show_p  = r.get("show_probability", 0) or 0
        if win_p > 1:
            win_p, place_p, show_p = win_p / 100, place_p / 100, show_p / 100

        ml = float(r.get("morning_line_odds") or 0)
        mkt_prob = round(1.0 / (ml + 1.0), 6) if ml > 0 else None

        edge = round(win_p - mkt_prob, 4) if (win_p and mkt_prob) else None
        bet_tag = (
            "bet"      if edge and edge >  0.02 else
            "underlay" if edge and edge < -0.02 else
            "neutral"
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO entry_scores (
                run_id, entry_id, horse_name, post_position,
                morning_line_odds, win_probability, place_probability,
                show_probability, market_implied_prob, value_score,
                bet_tag, rank
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, entry_row["entry_id"], r["horse_name"],
                r.get("post_position"), ml,
                win_p, place_p, show_p, mkt_prob,
                r.get("composite_score"), bet_tag, r.get("rank"),
            ),
        )
        migrated += 1

    conn.commit()
    print(f"  [predictions]  {migrated} rows -> score_runs + entry_scores.")
    return migrated


def step_drop_legacy(conn):
    dropped = []
    for t in LEGACY_TABLES:
        if table_exists(conn, t):
            conn.execute(f"DROP TABLE {t}")
            dropped.append(t)
    conn.commit()
    print(f"  [drop-legacy]  Dropped: {', '.join(dropped) or 'none'}")


def step_report(conn, horses_n, preds_n):
    REPORT_PATH.parent.mkdir(exist_ok=True)
    lines = [
        "DerbyEdge V0 -> V1 Migration Report",
        "=" * 44,
        f"Horses migrated    : {horses_n}",
        f"Prediction rows    : {preds_n}",
        "",
        "V1 Table Row Counts",
        "-" * 44,
    ]
    for t in [
        "tracks","race_cards","horses","people","entries",
        "horse_starts","workouts","odds_snapshots","track_bias",
        "trip_flags","model_registry","score_runs","entry_scores",
    ]:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            n = "ERR"
        lines.append(f"  {t:<22} {n:>5}")

    lines += [
        "",
        "Breaking Changes",
        "-" * 44,
        "  derby_field       -> entries (join horses, race_cards, people)",
        "  horse_features    -> rebuilt in Stage 3",
        "  derby_predictions -> score_runs + entry_scores",
        "  races             -> race_cards",
        "  race_entries      -> horse_starts",
        "  win_probability   -> fraction 0-1 (was pct 0-100)",
        "",
        "Scripts broken until Stage 2",
        "  scripts/ingest.py         (writes to derby_field)",
        "  scripts/build_features.py (reads from derby_field)",
        "  scripts/score.py          (reads horse_features, derby_field)",
    ]

    text = "\n".join(lines)
    REPORT_PATH.write_text(text, encoding="utf-8")
    print(f"\n{text}\n")
    print(f"  [report]  Written to {REPORT_PATH}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="DerbyEdge V0 -> V1 migration")
    ap.add_argument("--drop-legacy", action="store_true",
                    help="Drop V0 tables after migration")
    args = ap.parse_args()

    print("\nDerbyEdge migrate_schema.py")
    print("=" * 44)

    step_backup()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()

    step_apply_schema(conn)
    horses_n, card_id, _ = step_migrate_derby_field(conn)
    preds_n = step_migrate_predictions(conn, card_id)

    if args.drop_legacy:
        step_drop_legacy(conn)

    step_report(conn, horses_n, preds_n)
    conn.close()
    print("Done.\n")


if __name__ == "__main__":
    main()
