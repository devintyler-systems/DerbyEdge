"""B1: scratches, coupled entries and also-eligibles keep a faithful entry list."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest.draftkings_markdown import (
    ENTRY_RE, parse_draftkings_markdown, validate_draftkings_markdown_card,
)
from src.services.draftkings_markdown_intake import persist_validated_draftkings_markdown

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"
AS_OF = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    schema = "\n".join(
        ln for ln in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in ln
    )
    conn.executescript(schema)
    return conn


def _parse(name: str, raw: str | None = None, weight: int | None = 120):
    raw = raw if raw is not None else (FX / name).read_text(encoding="utf-8")
    card = parse_draftkings_markdown(raw, source_path=name, as_of=AS_OF)
    if weight is not None:
        for e in card.entries:
            if e.weight is None:
                e.weight = weight          # stands in for the Basic-grid overlay
    return card


def _persist(card):
    conn = _db()
    v = validate_draftkings_markdown_card(card)
    assert v.passed, v.errors
    return conn, v, persist_validated_draftkings_markdown(conn, card, v, source_filename="x_R1_1-1-26.md")


@pytest.mark.parametrize("name,total,scratched", [
    ("DMR_DK_Horse_R10_9-7-26.md", 13, 1),
    ("MNR_DK_Horse_R4_9-14-26.md", 8, 2),
])
def test_scratches_are_persisted_inactive_and_not_counted(name, total, scratched):
    card = _parse(name)
    conn, v, result = _persist(card)
    assert len(card.entries) == total
    assert v.parsed_unique_runner_count == total - scratched
    assert v.scratched_runner_count == scratched
    rows = conn.execute("SELECT scratch_flag FROM entries").fetchall()
    assert len(rows) == total and sum(r[0] for r in rows) == scratched
    assert result.persisted_runner_count == total - scratched
    assert conn.execute("SELECT field_size FROM race_cards").fetchone()[0] == total - scratched


def test_a_scratch_does_not_need_weight_or_connections():
    card = _parse("MNR_DK_Horse_R4_9-14-26.md", weight=None)
    for e in card.entries:
        if e.is_active:
            e.weight = 120
    assert validate_draftkings_markdown_card(card).passed


def test_the_source_post_position_is_used_not_the_program_digit():
    card = _parse("DMR_DK_Horse_R10_9-7-26.md")
    assert [e.post_position for e in card.entries] == list(range(1, 14))


def _renumber(raw: str, horse: str, label: str) -> str:
    m = next(m for m in ENTRY_RE.finditer(raw) if m.group("horse").strip() == horse)
    return raw[:m.start("program")] + label + raw[m.end("program"):]


def test_coupled_entry_keeps_both_horses():
    raw = (FX / "SAR_DK_Horse_R6_9-4-26.md").read_text(encoding="utf-8")
    raw = _renumber(raw, "Shangrala Road", "1A")
    card = _parse("SAR_DK_Horse_R6_9-4-26.md", raw=raw)
    posts = [e.post_position for e in card.entries]
    assert len(set(posts)) == len(posts) == 10
    conn, v, result = _persist(card)
    names = {r[0] for r in conn.execute("SELECT h.name FROM entries e JOIN horses h USING(horse_id)")}
    assert {"Magnum's Macrobrst", "Shangrala Road"} <= names and len(names) == 10
    assert any("post position" in w for w in v.warnings)


def test_reimport_with_a_late_scratch_flips_the_flag_without_duplicating():
    card = _parse("SAR_DK_Horse_R6_9-4-26.md")
    conn, v, first = _persist(card)
    assert first.persisted_runner_count == 10
    card2 = _parse("SAR_DK_Horse_R6_9-4-26.md", raw=(FX / "SAR_DK_Horse_R6_9-4-26.md").read_text().replace("Paul's Recovery", "Paul's Recovery ", 1))
    card2.entries[8].is_scratched = True
    card2.entries[8].entry_status = "SCRATCHED"
    v2 = validate_draftkings_markdown_card(card2)
    second = persist_validated_draftkings_markdown(conn, card2, v2, source_filename="y_R6_9-4-26.md")
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 10
    assert second.persisted_runner_count == 9


def test_also_eligible_label_is_parsed_and_not_counted_as_a_starter():
    raw = (FX / "SAR_DK_Horse_R6_9-4-26.md").read_text(encoding="utf-8")
    raw = _renumber(raw, "Shangrala Road", "AE1")
    card = _parse("SAR_DK_Horse_R6_9-4-26.md", raw=raw)
    ae = [e for e in card.entries if e.entry_status == "AE"]
    assert [e.horse_name for e in ae] == ["Shangrala Road"] and len(card.entries) == 10
    v = validate_draftkings_markdown_card(card)
    assert v.parsed_unique_runner_count == 9 and v.scratched_runner_count == 1
    conn, v, result = _persist(card)
    assert result.persisted_runner_count == 9
