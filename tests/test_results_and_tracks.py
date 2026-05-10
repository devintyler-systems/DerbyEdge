"""Regression tests: track normalization, null-result grading, pre-ingest scratch substitution.

Covers:
  - tracks.resolve_track / normalize_track_name for PRM aliases
  - Race History TP Won rendering: no results → "—" not "✗"
  - Scratch detected via entries.scratch_flag (no race_results row required)
    - effective TP substituted correctly
    - effective_tp_won evaluated on substituted runner
  - ingest_results track-code resolution (PRA / PRM)
  - ingest_results back-fills scratch_flag for absent entries (Bug 1)
  - load_race_review deduplicates to most-recent run per race (Bug 2)
  - actual_winner sourced from finish_position=1, not official_finish (Bug 3)
"""
from __future__ import annotations

import sqlite3
import pytest

from tests.conftest import insert_minimal_race
from src.derbyedge.tracks import normalize_track_name, resolve_track
from src.services.results_intake import (
    ensure_race_review_view,
    evaluate_score_run,
    get_effective_top_pick,
    ingest_results,
    load_race_review,
)


# ── Suite 1: Track normalization ──────────────────────────────────────────────

class TestTrackNormalization:
    def test_prm_primary_code(self):
        res = resolve_track(track_code="PRM")
        assert res["track_code"] == "PRM"
        assert res["resolution_source"] == "parsed_code"
        assert "Prairie Meadows" in res["track_name_canonical"]

    def test_pra_alias_resolves_to_prm(self):
        """PRA (legacy operator alias) must resolve to PRM, not be unresolved."""
        res = resolve_track(track_code="PRA")
        assert res["track_code"] == "PRM"
        assert res["resolution_source"] == "alias_exact"

    def test_prairie_meadows_name_resolves(self):
        res = resolve_track(track_name="Prairie Meadows")
        assert res["track_code"] == "PRM"
        assert res["resolution_source"] == "alias_exact"

    def test_prairie_meadows_racetrack_name_resolves(self):
        res = resolve_track(track_name="Prairie Meadows Racetrack")
        assert res["track_code"] == "PRM"

    def test_normalize_case_insensitive(self):
        assert normalize_track_name("PRAIRIE MEADOWS") == "prairie meadows"

    def test_existing_ct_unbroken(self):
        """Charles Town must still resolve correctly after PRM was added."""
        res = resolve_track(track_code="CT")
        assert res["track_code"] == "CT"
        assert res["resolution_source"] == "parsed_code"


# ── Suite 2: TP Won null-result grading ──────────────────────────────────────

class TestNullResultGrading:
    """A race with no ingested results must not be graded as a TP loss.

    The race_review view returns effective_tp_won = 0 when no race_results exist.
    The UI fix in app.py (and this test) verifies the correct rendering logic:
    when actual_winner is None (no results) the outcome should be shown as
    pending ("—"), not as a loss ("✗").
    """

    def _tp_won_display(self, row: dict) -> str:
        """Mirror the app.py display logic exactly."""
        if row.get("effective_tp_won"):
            return "✓"
        if not row.get("actual_winner"):
            return "—"
        return "✗"

    def test_no_results_shows_pending_not_loss(self, mem_conn):
        insert_minimal_race(mem_conn)
        rows = load_race_review(mem_conn)
        assert len(rows) == 1
        row = rows[0]
        # No race_results → actual_winner is None
        assert row["actual_winner"] is None
        # effective_tp_won = 0 (SQL default when no results)
        assert not row["effective_tp_won"]
        # Display logic must produce "—", not "✗"
        assert self._tp_won_display(row) == "—"

    def test_results_ingested_winner_matches_tp_shows_win(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        # Rank-1 (Alpha) wins; others finish in order
        for rank, (eid, hid) in enumerate(
            zip(ids["entry_ids"], ids["horse_ids"]), start=1
        ):
            mem_conn.execute(
                """INSERT INTO race_results
                       (card_id, entry_id, horse_id, official_finish,
                        finish_position, is_scratched, ingested_at)
                   VALUES (?, ?, ?, ?, ?, 0, '2026-05-08T12:00:00Z')""",
                (ids["card_id"], eid, hid, rank, rank),
            )
        mem_conn.commit()
        rows = load_race_review(mem_conn)
        assert rows[0]["actual_winner"] == "Alpha"
        assert rows[0]["effective_tp_won"] == 1
        assert self._tp_won_display(rows[0]) == "✓"

    def test_results_ingested_tp_loses_shows_loss(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        # Rank-2 (Bravo) wins; Rank-1 (Alpha) finishes 2nd
        for rank, (eid, hid) in enumerate(
            zip(ids["entry_ids"], ids["horse_ids"]), start=1
        ):
            official = 2 if rank == 1 else (1 if rank == 2 else rank)
            mem_conn.execute(
                """INSERT INTO race_results
                       (card_id, entry_id, horse_id, official_finish,
                        finish_position, is_scratched, ingested_at)
                   VALUES (?, ?, ?, ?, ?, 0, '2026-05-08T12:00:00Z')""",
                (ids["card_id"], eid, hid, official, official),
            )
        mem_conn.commit()
        rows = load_race_review(mem_conn)
        assert rows[0]["actual_winner"] == "Bravo"
        assert rows[0]["effective_tp_won"] == 0
        assert self._tp_won_display(rows[0]) == "✗"


# ── Suite 3: Pre-ingest scratch via entries.scratch_flag ─────────────────────

class TestPreIngestScratchSubstitution:
    """entries.scratch_flag=1 with no race_results row must be treated as scratched.

    This is the CT R9 scenario: Beautiful Noise (rank-1) was scratched before
    ingest; no race_results row exists for her.  The effective TP should be
    the rank-2 runner, and TP Won should be evaluated on that runner.
    """

    def test_entry_scratch_flag_detected_without_results_row(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        # Mark rank-1 (Alpha, entry_ids[0]) as scratched via entries.scratch_flag ONLY
        # — no race_results row inserted
        mem_conn.execute(
            "UPDATE entries SET scratch_flag=1 WHERE entry_id=?",
            (ids["entry_ids"][0],),
        )
        mem_conn.commit()
        # get_effective_top_pick must skip Alpha and return Bravo (rank 2)
        result = get_effective_top_pick(mem_conn, ids["run_id"], ids["card_id"])
        assert result is not None
        assert result["horse_name"] == "Bravo"
        assert result["rank"] == 2

    def test_race_review_effective_tp_substituted_via_scratch_flag(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        mem_conn.execute(
            "UPDATE entries SET scratch_flag=1 WHERE entry_id=?",
            (ids["entry_ids"][0],),
        )
        mem_conn.commit()
        rows = load_race_review(mem_conn)
        assert len(rows) == 1
        assert rows[0]["original_tp"] == "Alpha"
        assert rows[0]["original_tp_scratched"] == 1
        assert rows[0]["effective_tp"] == "Bravo"

    def test_evaluate_score_run_skips_entry_scratched_runner(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        # Alpha scratched via entries.scratch_flag; Bravo wins
        mem_conn.execute(
            "UPDATE entries SET scratch_flag=1 WHERE entry_id=?",
            (ids["entry_ids"][0],),
        )
        # Ingest results: Bravo 1st, Charlie 2nd, Delta 3rd, Echo 4th
        for i, (eid, hid) in enumerate(
            zip(ids["entry_ids"][1:], ids["horse_ids"][1:]), start=1
        ):
            mem_conn.execute(
                """INSERT INTO race_results
                       (card_id, entry_id, horse_id, official_finish,
                        finish_position, is_scratched, ingested_at)
                   VALUES (?, ?, ?, ?, ?, 0, '2026-05-08T12:00:00Z')""",
                (ids["card_id"], eid, hid, i, i),
            )
        mem_conn.commit()
        result = evaluate_score_run(mem_conn, ids["run_id"], ids["card_id"])
        assert result is not None
        assert result["original_tp_scratched"] is True
        assert result["effective_tp"] == "Bravo"
        assert result["effective_tp_won"] is True

    def test_race_review_effective_tp_won_with_entry_scratch(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        mem_conn.execute(
            "UPDATE entries SET scratch_flag=1 WHERE entry_id=?",
            (ids["entry_ids"][0],),
        )
        for i, (eid, hid) in enumerate(
            zip(ids["entry_ids"][1:], ids["horse_ids"][1:]), start=1
        ):
            mem_conn.execute(
                """INSERT INTO race_results
                       (card_id, entry_id, horse_id, official_finish,
                        finish_position, is_scratched, ingested_at)
                   VALUES (?, ?, ?, ?, ?, 0, '2026-05-08T12:00:00Z')""",
                (ids["card_id"], eid, hid, i, i),
            )
        mem_conn.commit()
        rows = load_race_review(mem_conn)
        assert rows[0]["effective_tp"] == "Bravo"
        assert rows[0]["effective_tp_won"] == 1
        assert rows[0]["actual_winner"] == "Bravo"


# ── Suite 4: ingest_results track-code resolution ─────────────────────────────

class TestIngestTrackResolution:
    """PRA in a results CSV must resolve to PRM and match the PRM race card in DB."""

    def _seed_prm_race(self, conn: sqlite3.Connection) -> dict:
        """Create a minimal PRM race card and score run."""
        conn.execute("PRAGMA foreign_keys = OFF")
        cur = conn.execute(
            "INSERT INTO tracks (name, abbrev) VALUES ('Prairie Meadows', 'PRM')"
        )
        track_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO race_cards
                   (track_id, card_date, race_number, distance_yards, surface)
               VALUES (?, '2026-05-08', 8, 1400, 'dirt')""",
            (track_id,),
        )
        card_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO horses (name) VALUES ('Sugah Down')"
        )
        horse_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO entries (card_id, horse_id, post_position, morning_line_odds)
               VALUES (?, ?, 1, 2.0)""",
            (card_id, horse_id),
        )
        entry_id = cur.lastrowid
        conn.commit()
        return {"card_id": card_id, "horse_id": horse_id, "entry_id": entry_id}

    def test_pra_track_code_resolves_to_prm_card(self, mem_conn):
        ids = self._seed_prm_race(mem_conn)
        rows = [
            {
                "race_date":    "2026-05-08",
                "track_code":   "PRA",   # legacy alias — must resolve to PRM
                "race_number":  8,
                "horse_name":   "Sugah Down",
                "finish_position": 1,
                "is_scratched": False,
            }
        ]
        result = ingest_results(mem_conn, rows)
        assert result["n_inserted"] == 1, (
            f"Expected 1 inserted row but got {result['n_inserted']}; "
            f"warnings: {result['warnings']}"
        )
        assert result["n_unmatched_race"] == 0


# ── Suite 5: Scratch back-fill on ingest ─────────────────────────────────────

_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]


def _make_result_rows(ids: dict, names: list[str], *, skip_first: bool = False) -> list[dict]:
    """Build ingest_results-compatible rows for the CD R1 fixture."""
    start = 1 if skip_first else 0
    rows = []
    for i, (eid, name) in enumerate(
        zip(ids["entry_ids"][start:], names[start:]), start=1
    ):
        rows.append({
            "race_date":       "2026-05-02",
            "track_code":      "CD",
            "race_number":     1,
            "horse_name":      name,
            "finish_position": i,
            "is_scratched":    False,
        })
    return rows


class TestScratchBackfillOnIngest:
    """ingest_results() must back-fill scratch_flag=1 for entries absent from results.

    This is the CT R9 / Beautiful Noise scenario: a runner that was scratched
    before ingest will have no race_results row.  ingest_results() must detect
    the absence and set entries.scratch_flag=1 so the race_review view treats
    the runner as scratched without requiring a manual DB update.
    """

    def test_absent_entry_gets_scratch_flag_set(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        rows = _make_result_rows(ids, _NAMES, skip_first=True)  # Alpha missing
        result = ingest_results(mem_conn, rows)
        assert result["n_inserted"] == 4
        sf = mem_conn.execute(
            "SELECT scratch_flag FROM entries WHERE entry_id=?",
            (ids["entry_ids"][0],),
        ).fetchone()[0]
        assert sf == 1, "Alpha (absent from results) must be back-filled as scratched"

    def test_present_entries_not_flagged(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        rows = _make_result_rows(ids, _NAMES, skip_first=False)  # all 5 present
        ingest_results(mem_conn, rows)
        flags = mem_conn.execute(
            "SELECT scratch_flag FROM entries WHERE card_id=?", (ids["card_id"],)
        ).fetchall()
        assert all(f[0] == 0 for f in flags), "No entry should be flagged when all are in results"

    def test_startup_migration_backfills_historical_absence(self, mem_conn):
        """ensure_race_review_view() must back-fill entries absent from already-ingested races."""
        ids = insert_minimal_race(mem_conn)
        # Manually write race_results for Bravo–Echo, leaving Alpha absent
        for i, (eid, hid) in enumerate(
            zip(ids["entry_ids"][1:], ids["horse_ids"][1:]), start=1
        ):
            mem_conn.execute(
                """INSERT INTO race_results
                       (card_id, entry_id, horse_id, finish_position,
                        official_finish, is_scratched, ingested_at)
                   VALUES (?, ?, ?, ?, ?, 0, '2026-05-02T12:00:00Z')""",
                (ids["card_id"], eid, hid, i, i),
            )
        mem_conn.commit()
        # Simulate an app restart — ensure_race_review_view() is called on startup
        ensure_race_review_view(mem_conn)
        sf = mem_conn.execute(
            "SELECT scratch_flag FROM entries WHERE entry_id=?",
            (ids["entry_ids"][0],),
        ).fetchone()[0]
        assert sf == 1, "Startup migration must back-fill Alpha as scratched"

    def test_startup_migration_idempotent(self, mem_conn):
        """Calling ensure_race_review_view() twice must not error or double-flag."""
        ids = insert_minimal_race(mem_conn)
        for i, (eid, hid) in enumerate(
            zip(ids["entry_ids"][1:], ids["horse_ids"][1:]), start=1
        ):
            mem_conn.execute(
                """INSERT INTO race_results
                       (card_id, entry_id, horse_id, finish_position,
                        official_finish, is_scratched, ingested_at)
                   VALUES (?, ?, ?, ?, ?, 0, '2026-05-02T12:00:00Z')""",
                (ids["card_id"], eid, hid, i, i),
            )
        mem_conn.commit()
        ensure_race_review_view(mem_conn)
        ensure_race_review_view(mem_conn)  # second call must not raise or corrupt
        flags = mem_conn.execute(
            "SELECT scratch_flag FROM entries WHERE card_id=?", (ids["card_id"],)
        ).fetchall()
        scratch_count = sum(f[0] for f in flags)
        assert scratch_count == 1, "Only Alpha should be flagged; idempotency must hold"

    def test_race_review_reflects_backfilled_scratch(self, mem_conn):
        """After back-fill, race_review.effective_tp must skip the absent runner."""
        ids = insert_minimal_race(mem_conn)
        rows = _make_result_rows(ids, _NAMES, skip_first=True)  # Alpha missing
        ingest_results(mem_conn, rows)
        review = load_race_review(mem_conn)
        assert len(review) == 1
        row = review[0]
        assert row["original_tp"] == "Alpha"
        assert row["original_tp_scratched"] == 1
        assert row["effective_tp"] == "Bravo"


# ── Suite 6: Race History deduplication ──────────────────────────────────────

class TestDeduplicateRaceHistory:
    """load_race_review() must return one row per race (most-recent score_run).

    PRM R8 scenario: two score_runs for the same race_card produce one row in
    Race History, not two.  The returned row must reflect the later run.
    """

    def _add_second_run(self, conn: sqlite3.Connection, ids: dict) -> str:
        """Insert a second score_run for the same card with a strictly later timestamp."""
        cur = conn.execute(
            """SELECT model_id FROM score_runs WHERE run_id=?""", (ids["run_id"],)
        )
        model_id = cur.fetchone()[0]
        # Pin the first run to a known-older timestamp so the ordering is deterministic
        conn.execute(
            "UPDATE score_runs SET run_timestamp='2026-05-02T10:00:00Z' WHERE run_id=?",
            (ids["run_id"],),
        )
        run_id2 = "run-002"
        conn.execute(
            """INSERT INTO score_runs
                   (run_id, card_id, model_id, model_type, derby_override_active,
                    chaos_active, chaos_intensity, quality_tier, run_timestamp)
               VALUES (?, ?, ?, 'fallback', 1, 1, 0.08, 'seed_only',
                       '2026-05-02T13:00:00Z')""",
            (run_id2, ids["card_id"], model_id),
        )
        # Minimal entry_scores so the run appears in race_review
        for rank, (eid, name) in enumerate(zip(ids["entry_ids"], _NAMES), start=1):
            prob = round(0.30 - (rank - 1) * 0.05, 2)
            conn.execute(
                """INSERT INTO entry_scores
                       (run_id, entry_id, horse_name, post_position, morning_line_odds,
                        win_probability, rank, bet_tag, market_implied_prob,
                        chaos_score, chaos_boost, chaos_tier, chaos_eligible)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'neutral', ?, ?, ?, ?, 0)""",
                (
                    run_id2, eid, name, rank, float(rank * 2),
                    prob, rank,
                    round(1.0 / (rank * 2 + 1), 4),
                    round(prob + 0.02, 4), 0.02,
                    "light" if rank <= 2 else "none",
                ),
            )
        conn.commit()
        return run_id2

    def test_two_runs_produce_one_row(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        self._add_second_run(mem_conn, ids)
        rows = load_race_review(mem_conn)
        assert len(rows) == 1, (
            f"Expected 1 row but got {len(rows)}; run_ids: {[r['run_id'] for r in rows]}"
        )

    def test_returns_most_recent_run(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        run_id2 = self._add_second_run(mem_conn, ids)
        rows = load_race_review(mem_conn)
        assert rows[0]["run_id"] == run_id2, (
            f"Expected most-recent run {run_id2!r}, got {rows[0]['run_id']!r}"
        )

    def test_single_run_unaffected(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        rows = load_race_review(mem_conn)
        assert len(rows) == 1
        assert rows[0]["run_id"] == ids["run_id"]


# ── Suite 7: actual_winner from finish_position ───────────────────────────────

class TestWinnerFromFinishPosition:
    """actual_winner must be populated even when official_finish is NULL.

    The winner JOIN was changed from official_finish=1 to finish_position=1 so
    that seed_only-tier ingest (which may store finish_position without setting
    official_finish) still surfaces the correct winner name.
    """

    def test_winner_found_when_official_finish_null(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        # Insert a race_results row with finish_position=1 but official_finish=NULL
        # (simulates an ingest path that doesn't populate official_finish)
        mem_conn.execute(
            """INSERT INTO race_results
                   (card_id, entry_id, horse_id, finish_position,
                    official_finish, is_scratched, ingested_at)
               VALUES (?, ?, ?, 1, NULL, 0, '2026-05-02T12:00:00Z')""",
            (ids["card_id"], ids["entry_ids"][0], ids["horse_ids"][0]),
        )
        mem_conn.commit()
        rows = load_race_review(mem_conn)
        assert rows[0]["actual_winner"] == "Alpha", (
            "actual_winner must be populated from finish_position=1 even when official_finish is NULL"
        )

    def test_winner_still_works_with_official_finish_set(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        # Standard ingest path: both finish_position and official_finish are set
        for rank, (eid, hid) in enumerate(
            zip(ids["entry_ids"], ids["horse_ids"]), start=1
        ):
            mem_conn.execute(
                """INSERT INTO race_results
                       (card_id, entry_id, horse_id, official_finish,
                        finish_position, is_scratched, ingested_at)
                   VALUES (?, ?, ?, ?, ?, 0, '2026-05-02T12:00:00Z')""",
                (ids["card_id"], eid, hid, rank, rank),
            )
        mem_conn.commit()
        rows = load_race_review(mem_conn)
        assert rows[0]["actual_winner"] == "Alpha"

    def test_scratched_winner_excluded(self, mem_conn):
        """A row with finish_position=1 but is_scratched=1 must not be the winner."""
        ids = insert_minimal_race(mem_conn)
        mem_conn.execute(
            """INSERT INTO race_results
                   (card_id, entry_id, horse_id, finish_position,
                    official_finish, is_scratched, ingested_at)
               VALUES (?, ?, ?, 1, 1, 1, '2026-05-02T12:00:00Z')""",
            (ids["card_id"], ids["entry_ids"][0], ids["horse_ids"][0]),
        )
        mem_conn.execute(
            """INSERT INTO race_results
                   (card_id, entry_id, horse_id, finish_position,
                    official_finish, is_scratched, ingested_at)
               VALUES (?, ?, ?, 2, 2, 0, '2026-05-02T12:00:00Z')""",
            (ids["card_id"], ids["entry_ids"][1], ids["horse_ids"][1]),
        )
        mem_conn.commit()
        rows = load_race_review(mem_conn)
        assert rows[0]["actual_winner"] != "Alpha", (
            "A scratched runner with finish_position=1 must not be returned as winner"
        )
