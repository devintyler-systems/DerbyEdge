"""Tests for src/services/horse_profile.get_speed_figures and _perf_score."""
from __future__ import annotations

import sqlite3
import pytest

from src.services.firstbet_enrich import ensure_firstbet_pp_table
from src.services.horse_profile import get_speed_figures, _perf_score


# ---------------------------------------------------------------------------
# _perf_score unit tests
# ---------------------------------------------------------------------------

class TestPerfScore:
    def test_win_scores_120(self):
        assert _perf_score(1, 10) == 120.0

    def test_last_scores_60(self):
        assert _perf_score(10, 10) == 60.0

    def test_midfield(self):
        score = _perf_score(5, 10)
        assert 60 < score < 120

    def test_invalid_inputs_return_none(self):
        assert _perf_score(None, 10) is None
        assert _perf_score(1, None) is None
        assert _perf_score("x", 10) is None

    def test_field_size_1_returns_none(self):
        # Denominator would be zero
        assert _perf_score(1, 1) is None

    def test_finish_clamped_to_field(self):
        # finish_pos > field_size shouldn't crash
        assert _perf_score(15, 10) == 60.0


# ---------------------------------------------------------------------------
# get_speed_figures fixture helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def sconn(mem_conn: sqlite3.Connection):
    ensure_firstbet_pp_table(mem_conn)
    yield mem_conn


def _insert_entry(conn, *, speed_last=None, speed_best=None, speed_avg=None, beyer=None):
    conn.execute("INSERT OR IGNORE INTO tracks (name, abbrev) VALUES ('SpeedTrack','ST')")
    tid = conn.execute("SELECT track_id FROM tracks WHERE abbrev='ST'").fetchone()[0]
    conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size)"
        " VALUES (?,?,1,1100,'dirt',8)", (tid, "2026-06-01"),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO horses (name) VALUES ('Speedy')")
    hid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO entries (card_id, horse_id, post_position, morning_line_odds,
               last_speed_fig, best_speed_fig, avg_speed_fig, beyer_fig)
           VALUES (?,?,1,5.0,?,?,?,?)""",
        (cid, hid, speed_last, speed_best, speed_avg, beyer),
    )
    eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return {"entry_id": eid, "horse_id": hid, "card_id": cid}


def _add_pp_starts(conn, entry_id, card_id, starts):
    for rank, (fp, fs) in enumerate(starts, start=1):
        conn.execute(
            """INSERT INTO firstbet_pp_starts
                   (entry_id, card_id, start_rank, finish_position, field_size)
               VALUES (?,?,?,?,?)""",
            (entry_id, card_id, rank, fp, fs),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# get_speed_figures tests
# ---------------------------------------------------------------------------

class TestGetSpeedFigures:
    def test_uses_entries_when_seeded(self, sconn):
        seed = _insert_entry(sconn, speed_last=82, speed_best=91, speed_avg=86.5)
        spd = get_speed_figures(sconn, seed["entry_id"])
        assert spd["source"] == "entries"
        assert spd["speed_last"] == 82
        assert spd["speed_best"] == 91
        assert spd["speed_avg"] == 86.5

    def test_derives_from_pp_when_entries_null(self, sconn):
        seed = _insert_entry(sconn)
        _add_pp_starts(sconn, seed["entry_id"], seed["card_id"],
                       [(2, 8), (1, 10), (4, 9)])
        spd = get_speed_figures(sconn, seed["entry_id"])
        assert spd["source"] == "de_derived"
        assert spd["speed_last"] == _perf_score(2, 8)    # most-recent start
        assert spd["speed_best"] == max(
            _perf_score(2, 8), _perf_score(1, 10), _perf_score(4, 9)
        )

    def test_speed_best_ge_speed_last(self, sconn):
        seed = _insert_entry(sconn)
        _add_pp_starts(sconn, seed["entry_id"], seed["card_id"],
                       [(3, 8), (1, 6), (5, 10)])
        spd = get_speed_figures(sconn, seed["entry_id"])
        assert spd["speed_best"] >= spd["speed_last"]

    def test_de_scores_in_expected_range(self, sconn):
        seed = _insert_entry(sconn)
        _add_pp_starts(sconn, seed["entry_id"], seed["card_id"],
                       [(1, 10), (10, 10)])
        spd = get_speed_figures(sconn, seed["entry_id"])
        assert 60.0 <= spd["speed_last"] <= 120.0
        assert 60.0 <= spd["speed_best"] <= 120.0

    def test_no_data_returns_none_source_none(self, sconn):
        seed = _insert_entry(sconn)
        spd = get_speed_figures(sconn, seed["entry_id"])
        assert spd["source"] == "none"
        assert spd["speed_last"] is None
        assert spd["speed_best"] is None

    def test_missing_entry_returns_source_none(self, sconn):
        spd = get_speed_figures(sconn, 99999)
        assert spd["source"] == "none"

    def test_race_results_speed_figure_used_as_second_priority(self, sconn):
        seed = _insert_entry(sconn)
        # Add a race result with a speed figure for this horse
        sconn.execute(
            """INSERT INTO race_results
                   (card_id, entry_id, horse_id, speed_figure,
                    official_finish, is_scratched, is_disqualified, ingested_at)
               VALUES (?,?,?,88,2,0,0,'2026-06-01T00:00:00Z')""",
            (seed["card_id"], seed["entry_id"], seed["horse_id"]),
        )
        sconn.commit()
        spd = get_speed_figures(sconn, seed["entry_id"])
        assert spd["source"] == "race_results"
        assert spd["speed_last"] == 88
