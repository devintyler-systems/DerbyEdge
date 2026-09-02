"""Tests for src/services/horse_profile.get_horse_profile and get_connections_stats."""
from __future__ import annotations

import sqlite3
import pytest

from src.services.firstbet_enrich import ensure_firstbet_pp_table
from src.services.horse_profile import get_horse_profile, get_connections_stats


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def pconn(mem_conn: sqlite3.Connection):
    """mem_conn extended with firstbet tables and one seeded horse."""
    ensure_firstbet_pp_table(mem_conn)
    yield mem_conn


def _seed_horse(conn: sqlite3.Connection, *, pp_starts: list[dict] | None = None,
                career_stats: dict | None = None,
                entry_overrides: dict | None = None) -> dict:
    """Insert track, card, horse, people, entry and optional PP data.

    Returns {entry_id, horse_id, card_id, trainer_id, jockey_id}.
    """
    conn.execute("INSERT OR IGNORE INTO tracks (name, abbrev) VALUES ('Test Track','TT')")
    track_id = conn.execute("SELECT track_id FROM tracks WHERE abbrev='TT'").fetchone()[0]
    conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size)"
        " VALUES (?,?,1,1100,'dirt',8)",
        (track_id, "2026-06-01"),
    )
    card_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute("INSERT INTO horses (name, sire, dam) VALUES ('Thunder','Storm','Lightning')")
    horse_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute("INSERT INTO people (full_name, role) VALUES ('Bob Smith','trainer')")
    trainer_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO people (full_name, role) VALUES ('Jane Doe','jockey')")
    jockey_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    overrides = entry_overrides or {}
    conn.execute(
        """INSERT INTO entries (card_id, horse_id, post_position, morning_line_odds,
               trainer_id, jockey_id,
               career_starts, career_wins, career_places, career_shows, career_earnings,
               dirt_starts, dirt_wins, dist_starts, dist_wins,
               last_race_days, last_race_finish)
           VALUES (?,?,1,5.0,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            card_id, horse_id, trainer_id, jockey_id,
            overrides.get("career_starts"),
            overrides.get("career_wins"),
            overrides.get("career_places"),
            overrides.get("career_shows"),
            overrides.get("career_earnings"),
            overrides.get("dirt_starts"),
            overrides.get("dirt_wins"),
            overrides.get("dist_starts"),
            overrides.get("dist_wins"),
            overrides.get("last_race_days"),
            overrides.get("last_race_finish"),
        ),
    )
    entry_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Insert career stats if provided
    if career_stats:
        conn.execute(
            """INSERT INTO firstbet_career_stats
                   (entry_id, card_id, career_win_pct, career_place_pct,
                    career_itm_pct, recent_5_itm, recent_5_wins)
               VALUES (?,?,?,?,?,?,?)""",
            (
                entry_id, card_id,
                career_stats.get("career_win_pct"),
                career_stats.get("career_place_pct"),
                career_stats.get("career_itm_pct"),
                career_stats.get("recent_5_itm"),
                career_stats.get("recent_5_wins"),
            ),
        )

    # Insert pp_starts if provided
    for rank, s in enumerate(pp_starts or [], start=1):
        conn.execute(
            """INSERT INTO firstbet_pp_starts
                   (entry_id, card_id, start_rank, race_date, track_code,
                    finish_position, field_size, surface, distance_text, odds_str)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                entry_id, card_id, rank,
                s.get("race_date"), s.get("track_code"),
                s.get("finish_pos"), s.get("field_size"),
                s.get("surface"), s.get("distance_text"), s.get("odds_str"),
            ),
        )

    conn.commit()
    return {
        "entry_id":   entry_id,
        "horse_id":   horse_id,
        "card_id":    card_id,
        "trainer_id": trainer_id,
        "jockey_id":  jockey_id,
    }


_SAMPLE_PP = [
    {"race_date": "2025-12-01", "track_code": "TT", "finish_pos": 2,
     "field_size": 8,  "surface": "D", "distance_text": "6f",  "odds_str": "3.80"},
    {"race_date": "2025-11-01", "track_code": "TT", "finish_pos": 1,
     "field_size": 10, "surface": "D", "distance_text": "6f",  "odds_str": "5.20"},
    {"race_date": "2025-10-01", "track_code": "TT", "finish_pos": 4,
     "field_size": 9,  "surface": "T", "distance_text": "5.5f","odds_str": "8.00"},
]


# ---------------------------------------------------------------------------
# get_horse_profile tests
# ---------------------------------------------------------------------------

class TestGetHorseProfile:
    def test_missing_entry_returns_empty(self, pconn):
        assert get_horse_profile(pconn, 99999) == {}

    def test_last5_derived_from_pp_starts(self, pconn):
        seed = _seed_horse(pconn, pp_starts=_SAMPLE_PP)
        prof = get_horse_profile(pconn, seed["entry_id"])
        assert prof["last5_starts"] == 3
        assert prof["last5_wins"] == 1    # rank-2 start: finish_pos=1
        assert prof["last5_places"] == 1  # rank-1 start: finish_pos=2
        assert prof["last5_shows"] == 0

    def test_career_stats_from_firstbet(self, pconn):
        seed = _seed_horse(
            pconn,
            pp_starts=_SAMPLE_PP,
            career_stats={"career_win_pct": 0.20, "career_place_pct": 0.30,
                          "career_itm_pct": 0.45, "recent_5_itm": 2, "recent_5_wins": 1},
        )
        prof = get_horse_profile(pconn, seed["entry_id"])
        assert prof["career_win_pct"] == pytest.approx(0.20)
        assert prof["career_itm_pct"] == pytest.approx(0.45)
        assert prof["pct_source"] == "firstbet"
        assert prof["recent_5_wins"] == 1
        assert prof["recent_5_itm"] == 2

    def test_career_stats_prefer_entries_when_present(self, pconn):
        seed = _seed_horse(
            pconn,
            pp_starts=_SAMPLE_PP,
            career_stats={"career_win_pct": 0.50},  # firstbet says 50%
            entry_overrides={
                "career_starts": 20, "career_wins": 3,
                "career_places": 4,  "career_shows": 2,
            },
        )
        prof = get_horse_profile(pconn, seed["entry_id"])
        assert prof["pct_source"] == "entries"
        assert prof["career_win_pct"] == pytest.approx(3 / 20)
        assert prof["career_starts"] == 20

    def test_dirt_derived_from_pp_when_entries_null(self, pconn):
        seed = _seed_horse(pconn, pp_starts=_SAMPLE_PP)
        prof = get_horse_profile(pconn, seed["entry_id"])
        # 2 dirt starts (ranks 1 & 2); 1 dirt win (rank 2: finish_pos=1)
        assert prof["dirt_last5_starts"] == 2
        assert prof["dirt_last5_wins"] == 1

    def test_dirt_uses_entries_when_present(self, pconn):
        seed = _seed_horse(
            pconn,
            pp_starts=_SAMPLE_PP,
            entry_overrides={"dirt_starts": 7, "dirt_wins": 2},
        )
        prof = get_horse_profile(pconn, seed["entry_id"])
        assert prof["dirt_last5_starts"] == 7   # entries value, not pp-derived 2
        assert prof["dirt_last5_wins"] == 2

    def test_last_race_date_from_pp(self, pconn):
        seed = _seed_horse(pconn, pp_starts=_SAMPLE_PP)
        prof = get_horse_profile(pconn, seed["entry_id"])
        assert prof["last_race_date"] == "2025-12-01"
        assert prof["last_race_finish"] == 2   # rank-1 start finish_pos

    def test_bloodstock_populated(self, pconn):
        seed = _seed_horse(pconn)
        prof = get_horse_profile(pconn, seed["entry_id"])
        assert prof["sire"] == "Storm"
        assert prof["dam"] == "Lightning"

    def test_no_pp_still_returns_empty_last5(self, pconn):
        seed = _seed_horse(pconn)
        prof = get_horse_profile(pconn, seed["entry_id"])
        assert prof["last5_starts"] == 0
        assert prof["career_win_pct"] is None


# ---------------------------------------------------------------------------
# get_connections_stats tests
# ---------------------------------------------------------------------------

class TestGetConnectionsStats:
    def test_returns_zeros_when_no_results(self, pconn):
        seed = _seed_horse(pconn)
        stats = get_connections_stats(pconn, seed["trainer_id"], seed["jockey_id"])
        assert stats["trainer"]["starts"] == 0
        assert stats["jockey"]["starts"] == 0
        assert stats["combo"]["starts"] == 0
        assert stats["trainer"]["sparse"] is True

    def test_counts_wins_from_race_results(self, pconn):
        seed = _seed_horse(pconn)
        # Insert a race result: our horse wins
        pconn.execute(
            """INSERT INTO race_results
                   (card_id, entry_id, horse_id, official_finish,
                    is_scratched, is_disqualified, ingested_at)
               VALUES (?,?,?,1,0,0,'2026-06-01T00:00:00Z')""",
            (seed["card_id"], seed["entry_id"], seed["horse_id"]),
        )
        pconn.commit()
        stats = get_connections_stats(pconn, seed["trainer_id"], seed["jockey_id"])
        assert stats["trainer"]["starts"] == 1
        assert stats["trainer"]["wins"] == 1
        assert stats["jockey"]["starts"] == 1
        assert stats["combo"]["starts"] == 1
        assert stats["combo"]["wins"] == 1

    def test_none_ids_return_sparse(self, pconn):
        stats = get_connections_stats(pconn, None, None)
        assert stats["trainer"]["sparse"] is True
        assert stats["jockey"]["sparse"] is True
        assert stats["combo"]["sparse"] is True

    def test_win_pct_none_when_no_starts(self, pconn):
        seed = _seed_horse(pconn)
        stats = get_connections_stats(pconn, seed["trainer_id"], seed["jockey_id"])
        assert stats["trainer"]["win_pct"] is None
