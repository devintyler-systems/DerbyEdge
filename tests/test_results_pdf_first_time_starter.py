"""Equibase chart rows for first-time starters print '---' in the Last Raced column (SA R10 10-12-26, Wild West End).

The row was silently skipped, so the import showed 9 of 10 finishers, gave Psalm 9th instead of 10th, and the scratch
back-fill then marked the horse that ran as scratched.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.services.pdf_ingest import parse_results_pdf
from src.services.results_intake import ingest_results
from tests.conftest import insert_minimal_race

CHART_TEXT = (Path(__file__).parent / "fixtures" / "SA_R10_10-12-26_chart_text.txt").read_text(encoding="utf-8")
ACTIVE = {"track_code": "SA", "race_date": "2026-10-12", "race_number": 10, "track_name": "Santa Anita"}


def _parse(text: str = CHART_TEXT) -> dict:
    with patch("src.services.pdf_ingest._extract_text", return_value=text):
        return parse_results_pdf(b"%PDF", active_race=ACTIVE, filename="SA101226USA10.pdf")


def test_a_first_time_starter_row_is_read_and_the_finish_order_is_not_shifted():
    r = _parse()
    assert r["ok"] and r["field_size"] == 10
    order = [(x["finish_position"], x["horse_name"], x["program_number"], x["final_odds"]) for x in r["runners"]]
    assert order[8] == (9, "Wild West End", 2, "29.40")
    assert order[9] == (10, "Psalm", 3, "8.80")
    assert [x[0] for x in order] == list(range(1, 11))


def test_a_chart_row_that_cannot_be_read_fails_loudly_instead_of_being_skipped():
    broken = CHART_TEXT.replace("22Aug264DMR5 3 Psalm(Ayuso,Armando) 121 b 3 6 6", "22Aug264DMR5 3 Psalm(Ayuso,Armando) 121 b", 1)
    r = _parse(broken)
    assert not r["ok"] and not r["runners"]
    assert "1 chart row(s) could not be read" in r["error"] and "Psalm" in r["error"]


def test_a_real_finisher_clears_a_scratch_flag_an_earlier_import_set_wrongly(mem_conn):
    ids = insert_minimal_race(mem_conn)
    mem_conn.execute("UPDATE entries SET scratch_flag=1 WHERE entry_id=?", (ids["entry_ids"][0],))
    mem_conn.commit()
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    rows = [{"race_date": "2026-05-02", "track_code": "CD", "race_number": 1, "horse_name": n,
             "finish_position": i, "is_scratched": False} for i, n in enumerate(names, start=1)]
    result = ingest_results(mem_conn, rows)
    assert result["n_inserted"] == 5
    flags = [f[0] for f in mem_conn.execute(
        "SELECT scratch_flag FROM entries WHERE card_id=? ORDER BY post_position", (ids["card_id"],))]
    assert flags == [0, 0, 0, 0, 0]
