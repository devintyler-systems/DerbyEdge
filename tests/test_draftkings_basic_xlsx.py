"""DraftKings Basic-tab XLSX parsing must preserve live ODDS independently of ML."""
from __future__ import annotations

import pandas as pd
import pytest

from src.ingest.draftkings_basic_csv import (
    DraftKingsBasicCSVError,
    parse_draftkings_basic_csv,
)


_ROW = {
    "#": "4",
    "ML": "2",
    "RUNNER": "Burn Indy Burn",
    "WEIGHT": "122",
    "JOCKEY": "Walter De La Cruz",
    "TRAINER": "Jon G. Arnett",
    "SIRE": "Take Charge Indy",
    "DAM": "Teton Fire",
    "RUN STYLE": "E 7",
    "DAYS OFF": "49",
}


def test_xlsx_preserves_distinct_live_odds_and_morning_line(tmp_path, caplog):
    path = tmp_path / "basic.xlsx"
    pd.DataFrame([{**_ROW, "ODDS": "8/5"}]).to_excel(path, index=False)

    caplog.set_level("INFO", logger="derbyedge.ingest.source_guard")
    parsed = parse_draftkings_basic_csv(path)

    assert parsed.rows[0].current_odds == "8/5"
    assert parsed.rows[0].current_odds != _ROW["ML"]
    assert "INGEST_SOURCE_STAMP" in caplog.text
    assert "INGEST_SOURCE_OUTSIDE_FIXTURES" in caplog.text


def test_xlsx_without_odds_is_rejected_and_never_backfilled_from_ml(tmp_path):
    path = tmp_path / "basic_without_odds.xlsx"
    pd.DataFrame([_ROW]).to_excel(path, index=False)

    with pytest.raises(DraftKingsBasicCSVError, match="missing required column\\(s\\): ODDS"):
        parse_draftkings_basic_csv(path)
