"""Tests for src/analysis/calibration_analysis.py"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.analysis.calibration_analysis import run_analysis

# Minimal set of columns the analysis expects.
_REQUIRED_COLS = [
    "run_id", "card_id", "top_pick_hit", "winner_rank",
    "winner_official_odds", "confidence_bucket", "confidence_score",
    "chaos_active", "ptf_aligned", "value_gap_top_vs_ptf",
]

_BASE_ROWS = [
    {
        "run_id": "aaa111", "card_id": "1",
        "top_pick_hit": 1, "winner_rank": 1, "winner_official_odds": 3.0,
        "confidence_bucket": "HIGH", "confidence_score": 0.80,
        "chaos_active": 0, "ptf_aligned": 1,
        "value_gap_top_vs_ptf": 0.07,
    },
    {
        "run_id": "bbb222", "card_id": "2",
        "top_pick_hit": 0, "winner_rank": 4, "winner_official_odds": "",
        "confidence_bucket": "LOW", "confidence_score": 0.30,
        "chaos_active": 1, "ptf_aligned": 0,
        "value_gap_top_vs_ptf": -0.02,
    },
    {
        "run_id": "ccc333", "card_id": "3",
        "top_pick_hit": 0, "winner_rank": 2, "winner_official_odds": "",
        "confidence_bucket": "LOW", "confidence_score": 0.25,
        "chaos_active": 0, "ptf_aligned": 0,
        "value_gap_top_vs_ptf": "",
    },
]


def _write_snapshot(directory: Path, rows: list[dict], cols: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "calibration_snapshot_20260101.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class TestAnalysisRunsWithMinimalSnapshot:
    def test_completes_without_raising(self, tmp_path: Path) -> None:
        _write_snapshot(tmp_path, _BASE_ROWS, _REQUIRED_COLS)
        run_analysis(output_dir=tmp_path)  # must not raise

    def test_section_headers_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _write_snapshot(tmp_path, _BASE_ROWS, _REQUIRED_COLS)
        run_analysis(output_dir=tmp_path)
        out = capsys.readouterr().out
        assert "Hit-rate by confidence_bucket" in out
        assert "chaos_active" in out
        assert "ptf_aligned" in out
        assert "Flat-bet ROI" in out
        assert "Value-gap slice" in out


class TestAnalysisHandlesMissingValueGapColumn:
    def test_skips_value_gap_section(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        cols_without_gap = [c for c in _REQUIRED_COLS if c != "value_gap_top_vs_ptf"]
        rows = [{k: v for k, v in r.items() if k != "value_gap_top_vs_ptf"} for r in _BASE_ROWS]
        _write_snapshot(tmp_path, rows, cols_without_gap)
        run_analysis(output_dir=tmp_path)  # must not raise
        out = capsys.readouterr().out
        assert "Skipping" in out
        assert "value_gap_top_vs_ptf" in out

    def test_skips_when_all_null(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rows = [{**r, "value_gap_top_vs_ptf": ""} for r in _BASE_ROWS]
        _write_snapshot(tmp_path, rows, _REQUIRED_COLS)
        run_analysis(output_dir=tmp_path)
        out = capsys.readouterr().out
        assert "Skipping" in out


class TestAnalysisHandlesNoFiles:
    def test_no_snapshot_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        run_analysis(output_dir=tmp_path)  # empty dir — must not raise
        out = capsys.readouterr().out
        assert "No calibration_snapshot" in out


class TestAnalysisHandlesEmptyFile:
    def test_empty_csv_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        path = tmp_path / "calibration_snapshot_20260101.csv"
        path.write_text("run_id,card_id\n", encoding="utf-8")
        run_analysis(output_dir=tmp_path)  # must not raise
        out = capsys.readouterr().out
        assert "empty" in out
