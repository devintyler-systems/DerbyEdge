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
from unittest.mock import patch
import pytest

from tests.conftest import insert_minimal_race
from src.derbyedge.tracks import normalize_track_name, normalize_track_text, resolve_track
from src.services.pdf_ingest import _extract_track, parse_results_pdf
from src.services.results_intake import (
    ensure_race_review_view,
    evaluate_score_run,
    get_effective_top_pick,
    ingest_results,
    load_outcomes_frame,
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


# ── Suite 8: PTF / winner odds pipeline ──────────────────────────────────────

_ODDS = [3.50, 5.00, 8.00, 12.00, 20.00]  # Alpha lowest → PTF


def _insert_results_with_odds(
    conn: sqlite3.Connection,
    ids: dict,
    *,
    winner_idx: int = 0,
    odds: list[float | None] | None = None,
    scratch_idx: int | None = None,
) -> None:
    """Insert race_results for all 5 horses with optional per-horse odds."""
    if odds is None:
        odds = _ODDS[:]
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    for i, (eid, hid, name, odd) in enumerate(
        zip(ids["entry_ids"], ids["horse_ids"], names, odds)
    ):
        finish = 1 if i == winner_idx else i + 2
        is_scr = 1 if i == scratch_idx else 0
        conn.execute(
            """INSERT INTO race_results
                   (card_id, entry_id, horse_id, post_position,
                    finish_position, official_finish, is_scratched,
                    official_odds_decimal, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-05-02T12:00:00Z')""",
            (ids["card_id"], eid, hid, i + 1, finish, finish, is_scr, odd),
        )
    conn.commit()


class TestPTFAndWinnerOdds:
    """PTF/winner odds must populate in outcomes frame after results ingest."""

    def test_ingest_persists_official_odds(self, mem_conn):
        """ingest_results must store official_odds_decimal when provided."""
        ids = insert_minimal_race(mem_conn)
        rows = [
            {
                "race_date":           "2026-05-02",
                "track_code":          "CD",
                "race_number":         1,
                "horse_name":          "Alpha",
                "finish_position":     1,
                "official_odds_decimal": 3.50,
            }
        ]
        result = ingest_results(mem_conn, rows)
        assert result["n_inserted"] == 1
        stored = mem_conn.execute(
            "SELECT official_odds_decimal FROM race_results WHERE entry_id=?",
            (ids["entry_ids"][0],),
        ).fetchone()[0]
        assert stored == pytest.approx(3.50)

    def test_outcomes_frame_populates_ptf_and_winner(self, mem_conn):
        """load_outcomes_frame returns populated PTF and winner_official_odds."""
        ids = insert_minimal_race(mem_conn)
        _insert_results_with_odds(mem_conn, ids, winner_idx=0)
        rows = load_outcomes_frame(mem_conn)
        assert rows, "outcomes frame must not be empty after ingest"
        row = rows[0]
        assert row["post_time_favorite_name"] == "Alpha"
        assert row["post_time_favorite_odds"] == pytest.approx(3.50)
        assert row["post_time_favorite_won"] == 1
        assert row["winner_name"] == "Alpha"
        assert row["winner_official_odds"] == pytest.approx(3.50)

    def test_winner_not_ptf_still_populates_winner_odds(self, mem_conn):
        """winner_official_odds populates even when the winner was not the favorite."""
        ids = insert_minimal_race(mem_conn)
        # Alpha is PTF (lowest odds 3.50), but Charlie wins (idx=2)
        _insert_results_with_odds(mem_conn, ids, winner_idx=2)
        rows = load_outcomes_frame(mem_conn)
        row = rows[0]
        assert row["post_time_favorite_name"] == "Alpha"
        assert row["post_time_favorite_won"] == 0
        assert row["winner_name"] == "Charlie"
        assert row["winner_official_odds"] == pytest.approx(_ODDS[2])

    def test_scratched_horse_cannot_be_ptf(self, mem_conn):
        """A scratched runner must be excluded from PTF even if its odds are lowest."""
        ids = insert_minimal_race(mem_conn)
        # Alpha scratched (is_scratched=1, lowest odds) → PTF must be Bravo
        _insert_results_with_odds(mem_conn, ids, winner_idx=1, scratch_idx=0)
        rows = load_outcomes_frame(mem_conn)
        row = rows[0]
        assert row["post_time_favorite_name"] != "Alpha", (
            "Scratched Alpha must not be PTF even with lowest odds"
        )
        assert row["post_time_favorite_name"] == "Bravo"

    def test_tied_odds_ptf_deterministic_by_post_position(self, mem_conn):
        """When two horses share the lowest odds, the lower post_position wins."""
        ids = insert_minimal_race(mem_conn)
        # Alpha (pp=1) and Bravo (pp=2) both at 3.50 — Alpha must win the tie
        tied_odds = [3.50, 3.50, 8.00, 12.00, 20.00]
        _insert_results_with_odds(mem_conn, ids, winner_idx=2, odds=tied_odds)
        rows = load_outcomes_frame(mem_conn)
        row = rows[0]
        assert row["post_time_favorite_name"] == "Alpha", (
            "Alpha (pp=1) must be PTF over Bravo (pp=2) when odds are tied"
        )
        assert row["post_time_favorite_odds"] == pytest.approx(3.50)

    def test_missing_odds_degrade_gracefully(self, mem_conn):
        """Races with no official_odds_decimal must not crash; PTF fields are None/blank."""
        ids = insert_minimal_race(mem_conn)
        # Insert results with NO odds (all None)
        _insert_results_with_odds(mem_conn, ids, winner_idx=0, odds=[None] * 5)
        rows = load_outcomes_frame(mem_conn)
        row = rows[0]
        assert row["post_time_favorite_name"] is None, (
            "No odds → PTF name must be None, not a false assignment"
        )
        assert row["post_time_favorite_odds"] is None
        assert row["winner_name"] == "Alpha", "winner_name must still populate from finish_position"

    def test_ptf_won_zero_when_ptf_did_not_win(self, mem_conn):
        """post_time_favorite_won is 0 when the PTF finished off the board."""
        ids = insert_minimal_race(mem_conn)
        _insert_results_with_odds(mem_conn, ids, winner_idx=4)  # Echo wins, Alpha is PTF
        rows = load_outcomes_frame(mem_conn)
        row = rows[0]
        assert row["post_time_favorite_name"] == "Alpha"
        assert row["post_time_favorite_won"] == 0
        assert row["winner_name"] == "Echo"


# ── Suite 8: LAD track resolution ────────────────────────────────────────────

class TestLADTrackResolution:
    """Louisiana Downs (LAD) alias variants must all resolve to code 'LAD'."""

    def test_lad_primary_code(self):
        res = resolve_track(track_code="LAD")
        assert res["track_code"] == "LAD"
        assert res["resolution_source"] == "parsed_code"
        assert "Louisiana Downs" in res["track_name_canonical"]

    def test_lad_canonical_name(self):
        res = resolve_track(track_name="Louisiana Downs")
        assert res["track_code"] == "LAD"
        assert res["resolution_source"] == "alias_exact"

    def test_lad_racetrack_alias(self):
        res = resolve_track(track_name="Louisiana Downs Racetrack")
        assert res["track_code"] == "LAD"

    def test_lad_bossier_city_alias(self):
        res = resolve_track(track_name="Louisiana Downs Bossier City")
        assert res["track_code"] == "LAD"

    def test_existing_evd_unbroken(self):
        """Evangeline Downs (EVD) must not be displaced after LAD was added."""
        res = resolve_track(track_code="EVD")
        assert res["track_code"] == "EVD"
        assert res["resolution_source"] == "parsed_code"


# ── Suite 9: normalize_track_text ────────────────────────────────────────────

class TestNormalizeTrackText:
    """normalize_track_text must strip OCR noise and produce UPPERCASE output."""

    def test_clean_mixed_case(self):
        assert normalize_track_text("Louisiana Downs") == "LOUISIANA DOWNS"

    def test_already_upper(self):
        assert normalize_track_text("LOUISIANA DOWNS") == "LOUISIANA DOWNS"

    def test_ocr_question_mark(self):
        assert normalize_track_text("LOUISIANA? DOWNS") == "LOUISIANA DOWNS"

    def test_extra_spaces(self):
        assert normalize_track_text("Louisiana   Downs") == "LOUISIANA DOWNS"

    def test_mixed_noise_with_date(self):
        result = normalize_track_text("LOUISIANA? DOWNS - May 12, 2026 - Race 5")
        assert result.startswith("LOUISIANA DOWNS")

    def test_unicode_ligature_stripped(self):
        # Non-ASCII punctuation must be removed, not left in output
        result = normalize_track_text("Louisiana’s Downs")
        assert "'" not in result
        assert "’" not in result


# ── Suite 10: _extract_track OCR noise tolerance ─────────────────────────────

class TestExtractTrackNoise:
    """_extract_track must resolve LAD variants including OCR-noisy headers."""

    def test_clean_canonical(self):
        code, _ = _extract_track("Louisiana Downs - May 12, 2026 - Race 5\n")
        assert code == "LAD"

    def test_all_caps(self):
        code, _ = _extract_track("LOUISIANA DOWNS - May 12, 2026 - Race 5\n")
        assert code == "LAD"

    def test_ocr_question_mark_between_words(self):
        code, _ = _extract_track("LOUISIANA? DOWNS - May 12, 2026 - Race 5\n")
        assert code == "LAD", (
            "OCR artifact '?' between words must not block track resolution"
        )

    def test_extra_spaces_between_words(self):
        code, _ = _extract_track("Louisiana   Downs - May 12, 2026 - Race 5\n")
        assert code == "LAD"

    def test_unrelated_track_not_matched(self):
        code, _ = _extract_track("Some Unknown Venue - May 12, 2026 - Race 5\n")
        assert code is None


# ── Suite 11: parse_results_pdf active-race fallback ─────────────────────────

# Minimal fake results-chart text: noisy track header so _extract_track returns
# None, but contains a parseable date, race number, and two finisher rows.
_FAKE_NOISY_CHART = (
    "UNKNWN?? VENUE - May 12, 2026 - Race 5\n"
    "1  FAST HORSE      2.10\n"
    "2  SLOW HORSE      4.50\n"
)

_FAKE_LAD_CHART = (
    "LOUISIANA? DOWNS - May 12, 2026 - Race 5\n"
    "1  FAST HORSE      2.10\n"
    "2  SLOW HORSE      4.50\n"
)


class TestResultsPdfActiveFallback:
    """parse_results_pdf active_race fallback: fills track_code when PDF header
    extraction fails but the parsed date+race match the active race exactly."""

    def _call(self, text: str, active_race=None):
        with patch("src.services.pdf_ingest._extract_text", return_value=text):
            return parse_results_pdf(b"fake-pdf", active_race=active_race)

    def test_lad_resolved_via_normalized_alias(self):
        """'LOUISIANA? DOWNS' should resolve to LAD via the normalized scan
        without needing the active-race fallback."""
        result = self._call(_FAKE_LAD_CHART)
        assert result["track_code"] == "LAD"
        # "active race fallback" warning must NOT appear; the no-finishers error
        # message also contains the word "fallback" so we check the specific phrase.
        assert not any("active race" in w.lower() for w in result["warnings"])

    def test_fallback_fills_track_code_on_noisy_header(self):
        result = self._call(
            _FAKE_NOISY_CHART,
            active_race={"track_code": "LAD", "race_date": "2026-05-12", "race_number": 5},
        )
        assert result["track_code"] == "LAD"
        assert any("active race" in w.lower() for w in result["warnings"])

    def test_fallback_not_used_when_date_mismatches(self):
        result = self._call(
            _FAKE_NOISY_CHART,
            active_race={"track_code": "LAD", "race_date": "2026-05-11", "race_number": 5},
        )
        assert result["track_code"] is None
        assert any("Could not extract track code" in w for w in result["warnings"])

    def test_fallback_not_used_when_race_number_mismatches(self):
        result = self._call(
            _FAKE_NOISY_CHART,
            active_race={"track_code": "LAD", "race_date": "2026-05-12", "race_number": 9},
        )
        assert result["track_code"] is None

    def test_fallback_not_used_when_active_race_is_none(self):
        result = self._call(_FAKE_NOISY_CHART, active_race=None)
        assert result["track_code"] is None

    def test_diagnostics_include_extraction_path(self):
        result = self._call(
            _FAKE_NOISY_CHART,
            active_race={"track_code": "LAD", "race_date": "2026-05-12", "race_number": 5},
        )
        diag = result.get("parse_diagnostics") or {}
        assert diag.get("track_extraction_path") == "active_race_fallback"

    def test_diagnostics_extracted_path_when_no_fallback(self):
        result = self._call(_FAKE_LAD_CHART)
        diag = result.get("parse_diagnostics") or {}
        assert diag.get("track_extraction_path") == "extracted"
