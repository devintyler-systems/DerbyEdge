"""Integration tests: Entry Details profile + speed helpers return non-null
stats when firstbet_pp_starts and firstbet_career_stats data are present."""
from __future__ import annotations

import sqlite3
import pytest

from src.services.firstbet_enrich import ensure_firstbet_pp_table
from src.services.horse_profile import get_horse_profile, get_speed_figures


@pytest.fixture
def econn(mem_conn: sqlite3.Connection):
    ensure_firstbet_pp_table(mem_conn)
    yield mem_conn


def _full_seed(conn: sqlite3.Connection) -> dict:
    """Insert a complete horse record with PP and career data."""
    conn.execute("INSERT OR IGNORE INTO tracks (name, abbrev) VALUES ('River Downs','RD')")
    tid = conn.execute("SELECT track_id FROM tracks WHERE abbrev='RD'").fetchone()[0]
    conn.execute(
        "INSERT INTO race_cards (track_id, card_date, race_number, distance_yards, surface, field_size)"
        " VALUES (?,?,5,1210,'dirt',10)",
        (tid, "2026-06-15"),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute("INSERT INTO horses (name, sire, dam) VALUES ('River King','Big River','Delta Queen')")
    hid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute("INSERT INTO people (full_name, role) VALUES ('Tom Train','trainer')")
    tid2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO people (full_name, role) VALUES ('Al Jock','jockey')")
    jid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # entries row: no seed speed/career figures (simulates 1/ST BET-only ingest)
    conn.execute(
        "INSERT INTO entries (card_id, horse_id, post_position, morning_line_odds, trainer_id, jockey_id)"
        " VALUES (?,?,3,6.0,?,?)",
        (cid, hid, tid2, jid),
    )
    eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # firstbet_career_stats
    conn.execute(
        """INSERT INTO firstbet_career_stats
               (entry_id, card_id, career_win_pct, career_place_pct,
                career_itm_pct, recent_5_itm, recent_5_wins)
           VALUES (?,?,0.15,0.25,0.40,2,1)""",
        (eid, cid),
    )

    # firstbet_pp_starts — 5 starts
    pp_data = [
        (1, "2026-05-01", "RD", 1, 9,  "D",   "6f",    "4.20"),
        (2, "2026-04-01", "RD", 3, 10, "D",   "6f",    "6.80"),
        (3, "2026-03-01", "RD", 2, 8,  "D",   "5.5f",  "3.50"),
        (4, "2026-02-01", "TG", 5, 12, "T",   "7f",    "10.00"),
        (5, "2026-01-01", "RD", 1, 7,  "D",   "6f",    "2.90"),
    ]
    for rank, date, trk, fp, fs, surf, dist, odds in pp_data:
        conn.execute(
            """INSERT INTO firstbet_pp_starts
                   (entry_id, card_id, start_rank, race_date, track_code,
                    finish_position, field_size, surface, distance_text, odds_str)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (eid, cid, rank, date, trk, fp, fs, surf, dist, odds),
        )

    conn.commit()
    return {"entry_id": eid, "horse_id": hid, "card_id": cid,
            "trainer_id": tid2, "jockey_id": jid}


class TestEntryDetailsWithPPs:
    def test_career_stats_nonnull(self, econn):
        seed = _full_seed(econn)
        prof = get_horse_profile(econn, seed["entry_id"])
        assert prof["career_win_pct"] is not None
        assert prof["career_itm_pct"] is not None

    def test_last5_nonnull(self, econn):
        seed = _full_seed(econn)
        prof = get_horse_profile(econn, seed["entry_id"])
        assert prof["last5_starts"] == 5
        assert prof["last5_wins"] == 2      # ranks 1 and 5 finished first

    def test_dirt_last5_derived(self, econn):
        seed = _full_seed(econn)
        prof = get_horse_profile(econn, seed["entry_id"])
        # 4 dirt starts out of 5
        assert prof["dirt_last5_starts"] == 4
        assert prof["dirt_last5_wins"] == 2  # ranks 1 and 5 won on dirt

    def test_last_race_date_populated(self, econn):
        seed = _full_seed(econn)
        prof = get_horse_profile(econn, seed["entry_id"])
        assert prof["last_race_date"] == "2026-05-01"
        assert prof["last_race_finish"] == 1   # rank-1 pp: finished first

    def test_speed_figures_nonnull(self, econn):
        seed = _full_seed(econn)
        spd = get_speed_figures(econn, seed["entry_id"])
        assert spd["source"] == "de_derived"
        assert spd["speed_best"] is not None
        assert spd["speed_last"] is not None
        assert spd["speed_avg"] is not None
        assert 60 <= spd["speed_last"] <= 120
        assert spd["speed_best"] >= spd["speed_last"]

    def test_speed_best_is_win(self, econn):
        seed = _full_seed(econn)
        spd = get_speed_figures(econn, seed["entry_id"])
        # rank-1 start: finish_pos=1 in field of 9 → 120
        assert spd["speed_best"] == pytest.approx(120.0)

    def test_bloodstock_populated(self, econn):
        seed = _full_seed(econn)
        prof = get_horse_profile(econn, seed["entry_id"])
        assert prof["sire"] == "Big River"
        assert prof["dam"] == "Delta Queen"
