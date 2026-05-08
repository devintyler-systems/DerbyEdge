"""Regression tests: scratch-aware TP logic, scorer alignment, chaos persistence.

Covers:
  - results_intake.get_effective_top_pick
  - results_intake.evaluate_score_run
  - results_intake.load_race_review  (race_review view semantics)
  - results_intake.load_outcomes_frame
  - scorer feat_df scratch filtering (array alignment guard)
  - scorer._chaos_outputs_for_run inactive / empty-field paths
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.conftest import insert_minimal_race
from src.services.results_intake import (
    evaluate_score_run,
    get_effective_top_pick,
    load_outcomes_frame,
    load_race_review,
)
from src.models.scorer import _chaos_outputs_for_run


# ── shared helper ─────────────────────────────────────────────────────────────

def _rr(conn, card_id, entry_id, horse_id, *,
        official_finish, finish_position=None, is_scratched=0):
    conn.execute(
        """INSERT INTO race_results
               (card_id, entry_id, horse_id, official_finish, finish_position,
                is_scratched, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, '2026-05-02T12:00:00Z')""",
        (card_id, entry_id, horse_id, official_finish, finish_position, is_scratched),
    )


# ── Suite 1: get_effective_top_pick ───────────────────────────────────────────

class TestGetEffectiveTopPick:
    def test_no_scratch_returns_rank1(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        result = get_effective_top_pick(mem_conn, ids["run_id"], ids["card_id"])
        assert result is not None
        assert result["entry_id"] == ids["entry_ids"][0]
        assert result["rank"] == 1

    def test_scratched_rank1_returns_rank2(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        _rr(mem_conn, ids["card_id"], ids["entry_ids"][0], ids["horse_ids"][0],
            official_finish=None, is_scratched=1)
        mem_conn.commit()
        result = get_effective_top_pick(mem_conn, ids["run_id"], ids["card_id"])
        assert result is not None
        assert result["entry_id"] == ids["entry_ids"][1]
        assert result["rank"] == 2

    def test_all_scratched_returns_none(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        for eid, hid in zip(ids["entry_ids"], ids["horse_ids"]):
            _rr(mem_conn, ids["card_id"], eid, hid,
                official_finish=None, is_scratched=1)
        mem_conn.commit()
        result = get_effective_top_pick(mem_conn, ids["run_id"], ids["card_id"])
        assert result is None


# ── Suite 2: evaluate_score_run ───────────────────────────────────────────────

class TestEvaluateScoreRun:
    def test_returns_none_without_results(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        assert evaluate_score_run(mem_conn, ids["run_id"], ids["card_id"]) is None

    def test_rank1_wins_recorded(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        for rank, (eid, hid) in enumerate(
            zip(ids["entry_ids"], ids["horse_ids"]), start=1
        ):
            _rr(mem_conn, ids["card_id"], eid, hid,
                official_finish=rank, finish_position=rank)
        mem_conn.commit()
        result = evaluate_score_run(mem_conn, ids["run_id"], ids["card_id"])
        assert result is not None
        assert result["top_pick_won"] is True
        assert result["original_tp_scratched"] is False

    def test_scratched_rank1_effective_tp_wins(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        # Rank-1 (Alpha) scratched
        _rr(mem_conn, ids["card_id"], ids["entry_ids"][0], ids["horse_ids"][0],
            official_finish=None, finish_position=None, is_scratched=1)
        # Rank-2 (Bravo) wins
        _rr(mem_conn, ids["card_id"], ids["entry_ids"][1], ids["horse_ids"][1],
            official_finish=1, finish_position=1)
        # Ranks 3-5 finish in order
        for i in range(2, 5):
            _rr(mem_conn, ids["card_id"], ids["entry_ids"][i], ids["horse_ids"][i],
                official_finish=i, finish_position=i)
        mem_conn.commit()
        result = evaluate_score_run(mem_conn, ids["run_id"], ids["card_id"])
        assert result is not None
        assert result["original_tp_scratched"] is True
        assert result["effective_tp"] == "Bravo"
        assert result["effective_tp_won"] is True


# ── Suite 3: scorer array alignment ───────────────────────────────────────────

class TestScorerAlignment:
    def test_scratch_filter_drops_scratched_entry(self):
        """Exact filtering idiom from scorer.py: feat_df filtered to live entry IDs."""
        feat_df = pd.DataFrame({
            "entry_id":       [1, 2, 3],
            "pace_fit_score": [0.5, 0.6, 0.7],
        })
        live_eids = {1, 3}  # entry 2 is scratched / not in entries_df
        filtered = (
            feat_df[feat_df["entry_id"].astype(int).isin(live_eids)]
            .reset_index(drop=True)
        )
        assert len(filtered) == 2
        assert filtered.iloc[0]["entry_id"] == 1
        assert filtered.iloc[1]["entry_id"] == 3

    def test_chaos_outputs_inactive_returns_passthrough(self):
        """When derby_active=False all chaos outputs are zero-impact."""
        win_probs = np.array([0.30, 0.25, 0.20, 0.15, 0.10])
        cs, cb, ct, ce, applied, intensity = _chaos_outputs_for_run(
            entries_df=pd.DataFrame({"post_position": range(1, 6)}),
            feat_df=pd.DataFrame(),
            win_probs=win_probs,
            form_arr=np.full(5, 0.5),
            surf_dist_arr=np.full(5, 0.5),
            derby_active=False,
        )
        assert applied is False
        assert intensity == 0.0
        np.testing.assert_array_equal(cs, win_probs)
        np.testing.assert_array_equal(cb, np.zeros(5))
        assert ct == ["none"] * 5
        np.testing.assert_array_equal(ce, np.zeros(5, dtype=int))

    def test_chaos_outputs_empty_field_no_crash(self):
        """Zero-entry field short-circuits even when derby_active=True."""
        cs, cb, ct, ce, applied, intensity = _chaos_outputs_for_run(
            entries_df=pd.DataFrame(),
            feat_df=pd.DataFrame(),
            win_probs=np.array([], dtype=float),
            form_arr=np.array([]),
            surf_dist_arr=np.array([]),
            derby_active=True,
        )
        assert applied is False
        assert len(cs) == 0


# ── Suite 4: race_review view – chaos and scratch semantics ───────────────────

class TestChaosRaceReview:
    def test_chaos_active_flows_through_view(self, mem_conn):
        ids = insert_minimal_race(mem_conn, chaos_active=1, chaos_intensity=0.08)
        rows = load_race_review(mem_conn)
        assert len(rows) == 1
        assert rows[0]["chaos_active"] == 1
        assert rows[0]["chaos_intensity"] == pytest.approx(0.08)

    def test_chaos_inactive_shows_zero(self, mem_conn):
        ids = insert_minimal_race(
            mem_conn, chaos_active=0, derby_override_active=0, chaos_intensity=None
        )
        rows = load_race_review(mem_conn)
        assert len(rows) == 1
        assert rows[0]["chaos_active"] == 0

    def test_effective_tp_differs_when_rank1_scratched(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        _rr(mem_conn, ids["card_id"], ids["entry_ids"][0], ids["horse_ids"][0],
            official_finish=None, is_scratched=1)
        mem_conn.commit()
        rows = load_race_review(mem_conn)
        assert rows[0]["original_tp"] == "Alpha"
        assert rows[0]["effective_tp"] == "Bravo"
        assert rows[0]["original_tp_scratched"] == 1

    def test_original_tp_entry_id_matches_rank1_entry(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        rows = load_race_review(mem_conn)
        assert rows[0]["original_tp_entry_id"] == ids["entry_ids"][0]


# ── Suite 5: load_outcomes_frame ──────────────────────────────────────────────

class TestLoadOutcomesFrame:
    def test_chaos_active_field_present(self, mem_conn):
        ids = insert_minimal_race(mem_conn, chaos_active=1)
        for rank, (eid, hid) in enumerate(
            zip(ids["entry_ids"], ids["horse_ids"]), start=1
        ):
            _rr(mem_conn, ids["card_id"], eid, hid,
                official_finish=rank, finish_position=rank)
        mem_conn.commit()
        rows = load_outcomes_frame(mem_conn)
        assert len(rows) > 0
        assert "chaos_active" in rows[0]
        assert rows[0]["chaos_active"] == 1

    def test_original_tp_scratched_reflected(self, mem_conn):
        ids = insert_minimal_race(mem_conn)
        # Rank-1 scratched
        _rr(mem_conn, ids["card_id"], ids["entry_ids"][0], ids["horse_ids"][0],
            official_finish=None, finish_position=None, is_scratched=1)
        # Rank-2 wins; others finish
        for i in range(1, 5):
            _rr(mem_conn, ids["card_id"], ids["entry_ids"][i], ids["horse_ids"][i],
                official_finish=i, finish_position=i)
        mem_conn.commit()
        rows = load_outcomes_frame(mem_conn)
        assert len(rows) > 0
        assert rows[0]["original_tp_scratched"] == 1
