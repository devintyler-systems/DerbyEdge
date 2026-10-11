"""A results chart for a stakes race with the sponsor in its title is a chart, not a 1/ST BET pre-race card."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.services.pdf_ingest import _extract_text, _is_1stbet, _is_equibase_chart, parse_results_pdf

ROOT = Path(__file__).resolve().parents[1]
CHART = sorted((ROOT / "data" / "raw" / "historical_results" / "2026" / "04").glob("*/eqb_CD_*_fullcard.pdf"))[0]
UNSUPPORTED = "Unsupported PDF format"


@pytest.fixture(scope="module")
def chart_text() -> str:
    return _extract_text(CHART.read_bytes())


def _with_sponsor_title(text: str) -> str:
    """The real chart text with its first race renamed the way the 2026-10-10 Charles Town stakes race was."""
    old = "MAIDEN SPECIAL WEIGHT - Thoroughbred"
    assert old in text[:1000]
    return text.replace(old, "WEST VIRGINIA 1/ST BET BREEDERS CLASSIC STAKES - Thoroughbred", 1)


def _parse(text: str, filename: str = "CD042526USA1.pdf") -> dict:
    with patch("src.services.pdf_ingest._extract_text", return_value=text):
        return parse_results_pdf(b"fake-pdf", filename=filename)


def test_reproduces_the_false_positive_the_brand_check_alone_fires_on_the_sponsor_title(chart_text):
    titled = _with_sponsor_title(chart_text)
    assert _is_1stbet(titled) is True                      # the brand layer reads the race title as the page header
    assert _is_1stbet(chart_text) is False                 # the untouched real chart is fine


def test_the_chart_structure_is_recognised_so_the_sponsor_title_no_longer_rejects_it(chart_text):
    titled = _with_sponsor_title(chart_text)
    assert _is_equibase_chart(titled) and _is_equibase_chart(chart_text)
    res = _parse(titled)
    assert UNSUPPORTED not in (res.get("error") or "")
    assert (res.get("parse_diagnostics") or {}).get("detected_format") != "1stbet_pp_card"
    assert res["ok"] is True and res["runners"]            # and it goes on to parse the chart normally


@pytest.mark.parametrize("text", [
    "5/7/26, 1:21 PM 1/ST BET - The Easy & Smart Way to Bet the Races\nINDIANA GRAND R5\n1PP1 YOTOWIN\n",
    "https://legacy.1stbet.com/race/ind-5\nHORSESHOE INDIANAPOLIS R 5 ALW\n",
    "1STBET race card IND R5\n1 YOTOWIN J Evin A. Roman T Rogelio Labra ML 8\n",
])
def test_a_real_1st_bet_pre_race_page_is_still_rejected_with_the_same_message(text):
    assert _is_equibase_chart(text) is False
    res = _parse(text, filename="IND_R5.pdf")
    assert res["ok"] is False and UNSUPPORTED in res["error"] and "1/ST BET race detail page" in res["error"]
    assert res["parse_diagnostics"]["detected_format"] == "1stbet_pp_card"


def test_the_compact_structural_1st_bet_export_is_still_rejected():
    text = ("HORSESHOE INDIANAPOLISR 5121 PM7 HorsesALW38,0001MDirt Fast\n"
            "1PP1YOTOWINJ Evin A. Roman T Rogelio Labra-ML 8\n2PP2INNISFREE LASSJ Alberto Burgos T John Haran-ML 9\n"
            "3PP3TAP BONNETJ Marcelino Pedroza, Jr. T Anthony J. Granitz-ML 5\n")
    res = _parse(text, filename="IND_R5.pdf")
    assert res["ok"] is False and UNSUPPORTED in res["error"]


def test_chart_markers_need_the_charts_own_structure_not_a_single_word():
    assert _is_equibase_chart("Last Raced Pgm Horse Name (Jockey) WgtM/E PP 1/4") is True
    assert _is_equibase_chart("Fractional Times: :23.1 :47.2\nTotal WPS Pool: $100,000") is True
    for weak in ("", "Fractional Times only", "Total WPS Pool only", "Last Raced", "Official results at 1/ST BET", "Pgm Horse Name"):
        assert _is_equibase_chart(weak) is False, weak
