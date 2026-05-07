"""
Race admin service: metadata editing, dependency auditing, soft/hard delete.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def ensure_is_hidden_column(conn: sqlite3.Connection) -> None:
    """Add is_hidden to race_cards if not present. Idempotent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(race_cards)").fetchall()}
    if "is_hidden" not in cols:
        conn.execute(
            "ALTER TABLE race_cards ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        print("[race_admin] Added is_hidden column to race_cards.")


def get_race_info(conn: sqlite3.Connection, card_id: int) -> dict[str, Any]:
    """Full race metadata for the edit form."""
    row = conn.execute(
        """SELECT rc.card_id, rc.card_date, rc.race_number,
                  rc.stakes_name, rc.purse, rc.distance_yards, rc.distance_furlongs,
                  rc.surface, rc.race_class, rc.age_restriction, rc.conditions,
                  rc.field_size,
                  t.track_id, t.name AS track_name, t.abbrev AS track_abbrev,
                  t.city, t.state
           FROM race_cards rc
           JOIN tracks t ON rc.track_id = t.track_id
           WHERE rc.card_id = ?""",
        (card_id,),
    ).fetchone()
    return dict(row) if row else {}


def update_race_card(
    conn: sqlite3.Connection,
    card_id: int,
    *,
    card_date: str | None = None,
    race_number: int | None = None,
    stakes_name: str | None = None,
    purse: int | None = None,
    distance_yards: int | None = None,
    surface: str | None = None,
    race_class: str | None = None,
    age_restriction: str | None = None,
    field_size: int | None = None,
    track_abbrev: str | None = None,
    track_name: str | None = None,
) -> dict[str, Any]:
    """Apply edits to race_cards (and optionally tracks).

    Returns {"ok": bool, "error": str | None, "warnings": list[str]}.
    """
    warnings: list[str] = []
    try:
        # Optionally update track name/abbrev on the parent track row
        if track_abbrev is not None or track_name is not None:
            row = conn.execute(
                "SELECT track_id FROM race_cards WHERE card_id = ?", (card_id,)
            ).fetchone()
            if row:
                tid = row[0]
                if track_abbrev is not None:
                    conn.execute(
                        "UPDATE tracks SET abbrev = ? WHERE track_id = ?",
                        (track_abbrev.upper(), tid),
                    )
                if track_name is not None:
                    conn.execute(
                        "UPDATE tracks SET name = ? WHERE track_id = ?",
                        (track_name, tid),
                    )

        # Build SET clause for race_cards
        updates: dict[str, Any] = {}
        if card_date is not None:
            updates["card_date"] = card_date
        if race_number is not None:
            updates["race_number"] = int(race_number)
        if stakes_name is not None:
            updates["stakes_name"] = stakes_name.strip() or None
        if purse is not None:
            updates["purse"] = int(purse) if purse else None
        if distance_yards is not None:
            updates["distance_yards"] = int(distance_yards)
        if surface is not None:
            updates["surface"] = surface
        if race_class is not None:
            updates["race_class"] = race_class.strip() or None
        if age_restriction is not None:
            updates["age_restriction"] = age_restriction.strip() or None
        if field_size is not None:
            updates["field_size"] = int(field_size) if field_size else None

        if updates:
            set_sql = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [card_id]
            conn.execute(f"UPDATE race_cards SET {set_sql} WHERE card_id = ?", vals)

        conn.commit()
        return {"ok": True, "error": None, "warnings": warnings}

    except sqlite3.IntegrityError as e:
        conn.rollback()
        return {"ok": False, "error": str(e), "warnings": warnings}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e), "warnings": warnings}


def get_race_dependencies(conn: sqlite3.Connection, card_id: int) -> dict[str, int]:
    """Count dependent rows across all tables for the given card_id."""

    def _count(table: str, where: str, *params: Any) -> int:
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", params
            ).fetchone()[0]
        except Exception:
            return 0

    counts: dict[str, int] = {
        "entries":       _count("entries",       "card_id = ?", card_id),
        "score_runs":    _count("score_runs",     "card_id = ?", card_id),
        "feature_store": _count("feature_store",  "card_id = ?", card_id),
        "horse_starts":  _count("horse_starts",   "card_id = ?", card_id),
        "live_odds":     _count("live_odds",       "card_id = ?", card_id),
        "race_results":  _count("race_results",    "card_id = ?", card_id),
    }

    entry_ids = [
        r[0]
        for r in conn.execute(
            "SELECT entry_id FROM entries WHERE card_id = ?", (card_id,)
        ).fetchall()
    ]

    if entry_ids:
        ph = ",".join("?" * len(entry_ids))
        counts["entry_scores"]   = _count("entry_scores",   f"entry_id IN ({ph})", *entry_ids)
        counts["odds_snapshots"] = _count("odds_snapshots", f"entry_id IN ({ph})", *entry_ids)

        hs_ids = [
            r[0]
            for r in conn.execute(
                "SELECT start_id FROM horse_starts WHERE card_id = ?", (card_id,)
            ).fetchall()
        ]
        if hs_ids:
            hs_ph = ",".join("?" * len(hs_ids))
            counts["trip_flags"] = _count("trip_flags", f"start_id IN ({hs_ph})", *hs_ids)
        else:
            counts["trip_flags"] = 0

        # entry_scores may also be reachable via score_runs; take the max
        run_ids = [
            r[0]
            for r in conn.execute(
                "SELECT run_id FROM score_runs WHERE card_id = ?", (card_id,)
            ).fetchall()
        ]
        if run_ids:
            r_ph = ",".join("?" * len(run_ids))
            es_via_run = _count("entry_scores", f"run_id IN ({r_ph})", *run_ids)
            counts["entry_scores"] = max(counts["entry_scores"], es_via_run)
    else:
        counts["entry_scores"]   = 0
        counts["odds_snapshots"] = 0
        counts["trip_flags"]     = 0

    return counts


def soft_delete_race(conn: sqlite3.Connection, card_id: int) -> dict[str, Any]:
    """Mark race as hidden (is_hidden=1). Preserves all data."""
    try:
        ensure_is_hidden_column(conn)
        conn.execute(
            "UPDATE race_cards SET is_hidden = 1 WHERE card_id = ?", (card_id,)
        )
        conn.commit()
        return {"ok": True, "error": None}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Return True if a table with the given name exists in the current schema."""
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
    )


def hard_delete_race(conn: sqlite3.Connection, card_id: int) -> dict[str, Any]:
    """Cascade-delete a race and every dependent row.  Irreversible.

    Deletion order is topologically sorted so child rows are removed before
    their parents, which keeps PRAGMA foreign_keys ON throughout.  The entire
    operation runs inside a single transaction; any failure rolls back all
    deletes atomically.

    FK-enforcement policy
    ─────────────────────
    PRAGMA foreign_keys is *read* at entry for documentation; it is never
    mutated.  Turning it OFF is unnecessary because deleting children before
    parents satisfies all FK constraints.  If a future change requires FK to
    be disabled, the caller must: (a) read current state, (b) set OFF,
    (c) perform the minimal required work, (d) restore the original state
    immediately inside the same try/finally block — not as part of
    commit/rollback flow.

    Topological deletion order (children → parents)
    ────────────────────────────────────────────────
     1. trip_flags       FK → horse_starts.start_id
     2. odds_snapshots   FK → entries.entry_id
     3. entry_scores     FK → score_runs.run_id AND entries.entry_id
     4. horse_starts     FK → entries.entry_id, race_cards.card_id
     5. feature_store    FK → entries.entry_id, race_cards.card_id
     6. race_results     FK → entries.entry_id, race_cards.card_id  (migration table)
     7. live_odds        card_id column only, no FK declared         (migration table)
     8. score_runs       FK → race_cards.card_id
     9. entries          FK → race_cards.card_id
    10. race_cards       parent — deleted last

    Returns
    ───────
    {
        "ok":      True | False,
        "error":   None | str,
        "deleted": {"trip_flags": N, "odds_snapshots": N, ...},
        "total":   N,
    }
    The "deleted" dict is populated even on failure so the caller can log
    partial progress.
    """
    # Read FK state for documentation; we do not change it.
    _fk_state = conn.execute("PRAGMA foreign_keys").fetchone()[0]  # noqa: F841

    deleted: dict[str, int] = {}

    def _del(table: str, where: str, params: list) -> int:
        """DELETE rows, record count, return count."""
        conn.execute(f"DELETE FROM {table} WHERE {where}", params)
        n = conn.execute("SELECT changes()").fetchone()[0]
        deleted[table] = deleted.get(table, 0) + n
        return n

    try:
        # ── Collect indirect-delete IDs before the transaction mutates rows ──
        entry_ids: list[int] = [
            r[0] for r in conn.execute(
                "SELECT entry_id FROM entries WHERE card_id = ?", (card_id,)
            ).fetchall()
        ]
        run_ids: list[str] = [
            r[0] for r in conn.execute(
                "SELECT run_id FROM score_runs WHERE card_id = ?", (card_id,)
            ).fetchall()
        ]
        hs_ids: list[int] = [
            r[0] for r in conn.execute(
                "SELECT start_id FROM horse_starts WHERE card_id = ?", (card_id,)
            ).fetchall()
        ]

        # ── Step 1: trip_flags (leaf; FK → horse_starts.start_id) ────────────
        if hs_ids:
            ph = ",".join("?" * len(hs_ids))
            _del("trip_flags", f"start_id IN ({ph})", hs_ids)
        else:
            deleted["trip_flags"] = 0

        # ── Step 2: odds_snapshots (FK → entries.entry_id) ───────────────────
        if entry_ids:
            ph = ",".join("?" * len(entry_ids))
            _del("odds_snapshots", f"entry_id IN ({ph})", entry_ids)
        else:
            deleted["odds_snapshots"] = 0

        # ── Step 3: entry_scores (FK → score_runs.run_id AND entries.entry_id)
        #    Must precede both score_runs (step 8) and entries (step 9).
        if run_ids:
            ph = ",".join("?" * len(run_ids))
            _del("entry_scores", f"run_id IN ({ph})", run_ids)
        else:
            deleted["entry_scores"] = 0

        # ── Step 4: horse_starts (FK → entries + race_cards) ─────────────────
        _del("horse_starts", "card_id = ?", [card_id])

        # ── Step 5: feature_store (FK → entries + race_cards) ────────────────
        _del("feature_store", "card_id = ?", [card_id])

        # ── Steps 6-7: migration-added tables (may not exist in all installs) ─
        for tbl in ("race_results", "live_odds"):
            if _table_exists(conn, tbl):
                _del(tbl, "card_id = ?", [card_id])
            else:
                deleted[tbl] = 0

        # ── Step 8: score_runs (FK → race_cards) ─────────────────────────────
        _del("score_runs", "card_id = ?", [card_id])

        # ── Step 9: entries (FK → race_cards; all child refs cleared above) ───
        _del("entries", "card_id = ?", [card_id])

        # ── Step 10: race_cards — must be last ───────────────────────────────
        n_rc = _del("race_cards", "card_id = ?", [card_id])
        if n_rc != 1:
            raise RuntimeError(
                f"Expected to delete 1 race_cards row for card_id={card_id}, "
                f"got {n_rc}.  The card may not exist or was already deleted."
            )

        conn.commit()
        return {
            "ok":      True,
            "error":   None,
            "deleted": deleted,
            "total":   sum(deleted.values()),
        }

    except Exception as exc:
        conn.rollback()
        return {
            "ok":      False,
            "error":   str(exc),
            "deleted": deleted,
            "total":   sum(deleted.values()),
        }
