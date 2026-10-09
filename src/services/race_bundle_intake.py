"""Persist the non-DK-Advanced parts of a race bundle onto the canonical card.

The DK Advanced card is persisted by ``persist_validated_draftkings_markdown``
(with the bundle's UTC post time).  This adds the TwinSpires observation lane
and, when the file was received before post, one pre-post market capture.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

from src.ingest.race_bundle import RaceBundle
from src.ingest.twinspires_markdown import parse_twinspires_markdown
from src.services.market_snapshot_intake import MarketSnapshotError, ingest_market_snapshot
from src.services.twinspires_intake import persist_twinspires_card, validate_twinspires_card


@dataclasses.dataclass
class BundleExtrasResult:
    twinspires_artifact_id: int | None = None
    twinspires_status: str = "NOT_PRESENT"
    market_snapshot_rows: int = 0
    market_snapshot_status: str = "NOT_ATTEMPTED"
    warnings: list[str] = dataclasses.field(default_factory=list)


def persist_race_bundle_extras(conn: sqlite3.Connection, bundle: RaceBundle, card_id: int) -> BundleExtrasResult:
    result = BundleExtrasResult()
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
    }
