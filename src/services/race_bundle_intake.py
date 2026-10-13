"""Persist the non-DK-Advanced parts of a race bundle onto the canonical card.

The DK Advanced card is persisted by ``persist_validated_draftkings_markdown``
(with the bundle's UTC post time).  This adds the TwinSpires observation lane
and, when the file was received before post, one pre-post market capture.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from typing import Any

from src.ingest.race_bundle import RaceBundle
from src.ingest.twinspires_markdown import parse_twinspires_markdown
from src.ingest.twinspires_tabs import distance_category
from src.services.market_snapshot_intake import MarketSnapshotError, ingest_market_snapshot
from src.services.twinspires_intake import persist_twinspires_card, validate_twinspires_card


_RACE_STATS_DDL = """
CREATE TABLE IF NOT EXISTS twinspires_race_stats (
    race_stats_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id        INTEGER NOT NULL REFERENCES race_cards(card_id),
    captured_at    TEXT    NOT NULL,          -- when the app received the bundle (ISO-8601 UTC); the as-of for this block
    raw_text       TEXT    NOT NULL,          -- the block exactly as pasted (newlines normalised)
    raw_sha256     TEXT    NOT NULL,
    parsed_json    TEXT    NOT NULL,          -- pars, race-type stats, track / post bias with sample sizes
    parser_version TEXT    NOT NULL,
    bundle_sha256  TEXT,
    stored_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(card_id, raw_sha256)               -- re-importing the same block does not duplicate it or move its capture time
);
CREATE INDEX IF NOT EXISTS idx_twinspires_race_stats_card ON twinspires_race_stats(card_id);
"""


_TAB_DDL = """
CREATE TABLE IF NOT EXISTS twinspires_tab_stats (
    tab_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id           INTEGER NOT NULL REFERENCES race_cards(card_id),
    tab_kind          TEXT    NOT NULL,        -- ts_speed | ts_class | ts_pace
    captured_at       TEXT    NOT NULL,        -- when the app received the bundle (ISO-8601 UTC); the as-of for this tab
    late_capture      INTEGER NOT NULL,        -- 1 when received at/after post: never usable as a live feature
    race_distance     TEXT,                    -- the DK card's distance as printed
    distance_category TEXT,                    -- sprint | route: pace calls differ, so never pool across them
    raw_text          TEXT    NOT NULL,        -- the tab exactly as pasted (newlines normalised)
    raw_sha256        TEXT    NOT NULL,
    parsed_json       TEXT    NOT NULL,        -- per runner: printed column -> value and rank, style as printed; "-" is null
    parser_version    TEXT    NOT NULL,
    bundle_sha256     TEXT,
    stored_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(card_id, tab_kind, raw_sha256)      -- re-importing the same tab does not duplicate it or move its capture time
);
CREATE INDEX IF NOT EXISTS idx_twinspires_tab_stats_card ON twinspires_tab_stats(card_id);
"""


def ensure_tab_stats_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_TAB_DDL)


def persist_ts_tabs(conn: sqlite3.Connection, bundle: RaceBundle, card_id: int) -> dict[str, str]:
    """Store each pasted Speed / Class / Pace tab raw + parsed against the card.  Returns {kind: status}."""
    if not bundle.ts_tabs:
        return {}
    ensure_tab_stats_table(conn)
    distance = bundle.card.race.distance
    out: dict[str, str] = {}
    for kind, tab in bundle.ts_tabs.items():
        existing = conn.execute("SELECT 1 FROM twinspires_tab_stats WHERE card_id=? AND tab_kind=? AND raw_sha256=?",
                                (card_id, kind, tab.raw_sha256)).fetchone()
        if existing:
            out[kind] = "ALREADY_STORED"
            continue
        conn.execute(
            """INSERT INTO twinspires_tab_stats (card_id, tab_kind, captured_at, late_capture, race_distance, distance_category,
               raw_text, raw_sha256, parsed_json, parser_version, bundle_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (card_id, kind, bundle.captured_at.isoformat(), int(bundle.late_capture), distance, distance_category(distance),
             tab.raw_text, tab.raw_sha256, json.dumps(tab.to_dict(), sort_keys=True), tab.parser_version, bundle.bundle_sha256))
        out[kind] = "STORED"
    conn.commit()
    return out


def ensure_race_stats_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_RACE_STATS_DDL)


def persist_race_stats(conn: sqlite3.Connection, bundle: RaceBundle, card_id: int) -> tuple[int | None, str]:
    """Store the RACE STATS block raw + parsed against the card.  Returns (row id, status)."""
    stats = bundle.race_stats
    if stats is None:
        return None, "NOT_PRESENT"
    ensure_race_stats_table(conn)
    existing = conn.execute("SELECT race_stats_id FROM twinspires_race_stats WHERE card_id=? AND raw_sha256=?",
                            (card_id, stats.raw_sha256)).fetchone()
    if existing:
        return int(existing[0]), "ALREADY_STORED"
    conn.execute(
        """INSERT INTO twinspires_race_stats (card_id, captured_at, raw_text, raw_sha256, parsed_json, parser_version, bundle_sha256)
           VALUES (?,?,?,?,?,?,?)""",
        (card_id, bundle.captured_at.isoformat(), stats.raw_text, stats.raw_sha256,
         json.dumps(stats.to_dict(), sort_keys=True), stats.parser_version, bundle.bundle_sha256))
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]), "STORED"


@dataclasses.dataclass
class BundleExtrasResult:
    twinspires_artifact_id: int | None = None
    twinspires_status: str = "NOT_PRESENT"
    market_snapshot_rows: int = 0
    market_snapshot_status: str = "NOT_ATTEMPTED"
    warnings: list[str] = dataclasses.field(default_factory=list)
    race_stats_id: int | None = None
    race_stats_status: str = "NOT_PRESENT"
    ts_tab_status: dict[str, str] = dataclasses.field(default_factory=dict)


def persist_race_bundle_extras(conn: sqlite3.Connection, bundle: RaceBundle, card_id: int) -> BundleExtrasResult:
    result = BundleExtrasResult()
    result.race_stats_id, result.race_stats_status = persist_race_stats(conn, bundle, card_id)
    result.ts_tab_status = persist_ts_tabs(conn, bundle, card_id)
    if bundle.twinspires is None:
        return result
    raw = bundle.twinspires_text.encode("utf-8")
    ts = parse_twinspires_markdown(
        raw, source_filename="race_bundle_twinspires",
        declared_as_of=bundle.captured_at.isoformat(),
    )
    validation = validate_twinspires_card(conn, card_id, ts)
    result.twinspires_artifact_id = persist_twinspires_card(conn, ts, validation, raw_bytes=raw)
    result.twinspires_status = "PASS" if validation.passed else "FAIL: " + "; ".join(validation.errors)
    if not validation.passed:
        result.warnings.append(f"TwinSpires lane not persisted as observations: {result.twinspires_status}")
        return result
    if bundle.late_capture:
        result.market_snapshot_status = "SKIPPED_LATE_CAPTURE"
        return result
    if bundle.post_utc is None:
        result.market_snapshot_status = "SKIPPED_NO_POST_TIME"
        return result
    try:
        result.market_snapshot_rows = ingest_market_snapshot(
            conn, card_id, raw, provider="twinspires", captured_at=bundle.captured_at.isoformat(),
        )
        result.market_snapshot_status = "CAPTURED"
    except MarketSnapshotError as exc:
        result.market_snapshot_status = f"REJECTED: {exc}"
        result.warnings.append(f"market snapshot not captured: {exc}")
    return result


def bundle_summary(bundle: RaceBundle) -> dict[str, Any]:
    card = bundle.card
    return {
        "bundle_sha256": bundle.bundle_sha256,
        "sections_found": sorted(bundle.sections),
        "program_entries": len(card.entries),
        "active_starters": sum(1 for e in card.entries if e.is_active),
        "scratched_or_inactive": [
            f"{e.program_number} {e.horse_name} ({e.entry_status})" for e in card.entries if not e.is_active
        ],
        "post_time_utc": bundle.post_utc,
        "post_time_source": bundle.post_source,
        "captured_at": bundle.captured_at.isoformat(),
        "late_capture": bundle.late_capture,
        "weights_merged_from_basic": bundle.reconciliation.weights_merged,
        "programs_cross_checked": bundle.reconciliation.programs_checked,
        "conflicts": bundle.reconciliation.conflicts,
        "race_stats_present": bundle.race_stats is not None,
        "twinspires_tabs_present": sorted(bundle.ts_tabs),
    }
