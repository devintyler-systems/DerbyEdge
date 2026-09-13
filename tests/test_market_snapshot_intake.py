"""Current ODDS capture, provenance, and scorer market-edge contracts."""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.ingest.draftkings_basic_csv import parse_draftkings_basic_csv
from src.ingest.twinspires_markdown import parse_twinspires_markdown
from src.models.scorer import _complete_live_market_probs, _market_edges_and_tags
from src.services.market_snapshot_intake import (
    MarketSnapshotError, fractional_odds, ingest_market_snapshot,
    latest_valid_market_snapshot,
)
from src.services.odds_intake import market_eligibility


ROOT = Path(__file__).resolve().parents[1]
CT = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "CT_TwinSpires__R7_Summary_9-12-26.md"
CAPTURE = "2026-09-12T21:00:00Z"
POST = "2026-09-12T22:00:00Z"


def _card() -> tuple[sqlite3.Connection, list[tuple[int, int]]]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO tracks (name,abbrev) VALUES ('Charles Town','CT')")
    conn.execute(
        """INSERT INTO race_cards
           (track_id,card_date,race_number,distance_yards,field_size,scheduled_post_time_utc)
           VALUES (1,'2026-09-12',7,880,10,?)""", (POST,)
    )
    entries = []
    for record in parse_twinspires_markdown(CT).records:
        conn.execute("INSERT INTO horses (name) VALUES (?)", (record.horse_name,))
        horse_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        post = int(record.program_number)
        conn.execute(
            """INSERT INTO entries
               (card_id,horse_id,post_position,program_number,morning_line_odds)
               VALUES (1,?,?,?,5)""", (horse_id, post, record.program_number)
        )
        entries.append((int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]), post))
    conn.commit()
    return conn, entries


def _dk_csv(*, first_odds: str = "5/2") -> str:
    lines = ["#,ODDS,ML,RUNNER,WEIGHT,JOCKEY,TRAINER,SIRE,DAM,RUN STYLE,DAYS OFF"]
    for index, record in enumerate(parse_twinspires_markdown(CT).records):
        current = first_odds if index == 0 else record.current_odds
        lines.append(
            f"{record.program_number},{current},30/1,{record.horse_name},122,Jockey,Trainer,Sire,Dam,E/P,29"
        )
    return "\n".join(lines)


def _frame(entries: list[tuple[int, int]]) -> pd.DataFrame:
    return pd.DataFrame(entries, columns=["entry_id", "post_position"])


def test_source_field_mapping_and_fractional_conversion():
    twinspires = parse_twinspires_markdown(CT)
    assert (twinspires.records[0].program_number, twinspires.records[0].current_odds) == ("1", "5/2")
    assert twinspires.records[1].current_odds == "9"
    dk = parse_draftkings_basic_csv(_dk_csv())
    assert (dk.rows[0].program_number, dk.rows[0].current_odds) == ("1", "5/2")
    assert (dk.rows[0].horse_name, dk.rows[0].weight) == ("Truly an Honor", 122)
    assert fractional_odds("5/2") == (5, 2)
    assert fractional_odds("9") == (9, 1)
    assert fractional_odds("2.5") == (5, 2)


def test_pre_post_twinspires_capture_populates_market_edge_and_tag():
    conn, entries = _card()
    count = ingest_market_snapshot(conn, 1, CT, provider="twinspires", captured_at=CAPTURE)
    assert count == len(entries) == 10
    row = conn.execute(
        """SELECT odds_numerator,odds_denominator,implied_prob,source,
                  source_provider,program_number FROM odds_snapshots
           WHERE entry_id=?""", (entries[0][0],)
    ).fetchone()
    assert tuple(row[:2]) == (5, 2)
    assert row[2] == pytest.approx(2 / 7, abs=1e-6)
    assert tuple(row[3:]) == ("book", "twinspires", "1")
    market = _complete_live_market_probs(conn, 1, _frame(entries))
    assert market is not None and market[0] == pytest.approx(2 / 7, abs=1e-6)
    model = np.full(len(entries), 0.1)
    model[0] = 0.4
    edges, tags = _market_edges_and_tags(
        model, market, collapsed=False, bet_threshold=0.025, underlay_threshold=-0.015,
    )
    assert edges[0] == pytest.approx(round(0.4 - 2 / 7, 4), abs=1e-4)
    assert tags[0] == "bet"
    assert all(math.isfinite(edge) for edge in edges)
    conn.close()


def test_newer_dk_basic_capture_uses_odds_not_ml():
    conn, entries = _card()
    ingest_market_snapshot(conn, 1, CT, provider="twinspires", captured_at=CAPTURE)
    inserted = ingest_market_snapshot(
        conn, 1, _dk_csv(first_odds="2/1"), provider="draftkings",
        captured_at="2026-09-12T21:30:00Z",
    )
    assert inserted == len(entries)
    latest = latest_valid_market_snapshot(conn, 1, [entry_id for entry_id, _ in entries])
    assert latest is not None and latest[entries[0][0]] == pytest.approx(1 / 3, abs=1e-6)
    assert ingest_market_snapshot(
        conn, 1, _dk_csv(first_odds="2/1"), provider="draftkings",
        captured_at="2026-09-12T21:30:00Z",
    ) == 0
    # Even a directly inserted late quote cannot displace the proven batch.
    conn.execute(
        """INSERT INTO odds_snapshots
           (entry_id,snapshot_time,odds_numerator,odds_denominator,source,
            source_provider,source_artifact_sha256,program_number)
           VALUES (?, '2026-09-12T22:01:00+00:00', 1, 1, 'book',
                   'draftkings', 'late', '1')""", (entries[0][0],)
    )
    assert latest_valid_market_snapshot(conn, 1, [entry_id for entry_id, _ in entries]) == latest
    conn.close()


def test_missing_capture_keeps_value_and_tags_null():
    conn, entries = _card()
    assert _complete_live_market_probs(conn, 1, _frame(entries)) is None
    edges, tags = _market_edges_and_tags(
        np.full(len(entries), 0.1), None, collapsed=False,
        bet_threshold=0.025, underlay_threshold=-0.015,
    )
    assert np.isnan(edges).all() and tags == [None] * len(entries)
    conn.close()


@pytest.mark.parametrize("captured_at", [None, POST, "2026-09-12T22:00:01Z", "2026-09-12T21:00:00"])
def test_unproven_or_post_post_time_rejected(captured_at):
    conn, entries = _card()
    with pytest.raises(MarketSnapshotError):
        ingest_market_snapshot(conn, 1, CT, provider="twinspires", captured_at=captured_at)
    assert conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 0
    assert _complete_live_market_probs(conn, 1, _frame(entries)) is None
    if captured_at == POST:
        assert market_eligibility("live_tote", POST, POST)[0] is False
    conn.close()


def test_missing_scheduled_post_rejects_capture():
    conn, _ = _card()
    conn.execute("UPDATE race_cards SET scheduled_post_time_utc=NULL WHERE card_id=1")
    with pytest.raises(MarketSnapshotError, match="scheduled post time"):
        ingest_market_snapshot(conn, 1, CT, provider="twinspires", captured_at=CAPTURE)
    assert conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 0
    conn.close()


def test_malformed_or_partial_odds_fails_without_partial_insert(tmp_path):
    conn, entries = _card()
    malformed = tmp_path / "bad.md"
    malformed.write_text(CT.read_text(encoding="utf-8").replace("5/2\nM: 2", "oops\nM: 2", 1), encoding="utf-8")
    with pytest.raises(MarketSnapshotError, match="invalid current ODDS"):
        ingest_market_snapshot(conn, 1, malformed, provider="twinspires", captured_at=CAPTURE)
    with pytest.raises(MarketSnapshotError, match="invalid current ODDS"):
        ingest_market_snapshot(conn, 1, _dk_csv(first_odds="SCR"),
                               provider="draftkings", captured_at=CAPTURE)
    with pytest.raises(MarketSnapshotError, match="incomplete ODDS capture"):
        ingest_market_snapshot(conn, 1, "\n".join(_dk_csv().splitlines()[:-1]),
                               provider="draftkings", captured_at=CAPTURE)
    assert conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 0
    assert _complete_live_market_probs(conn, 1, _frame(entries)) is None
    conn.close()
