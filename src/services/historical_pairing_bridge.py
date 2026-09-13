"""Read-only exact-key pairing bridge for historical-snapshot reconnaissance.

Consumes the recon card inventory
(``historical_source_recon_inventory.csv``) plus the Equibase race-level result
index (``equibase_result_race_index.csv``) and attempts **exact
``track|YYYY-MM-DD|R{n}`` candidate-key pairing only**.

It never ingests, never mutates SQLite, and never makes anything eligible.  Each
output row is labelled ``RECON_ONLY_NOT_ELIGIBLE`` and still enumerates the
contract evidence that remains missing (as-of timestamp, full-field
reconciliation, feature-vector completeness, outcome provenance, ...).
"""
from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path
from typing import Iterable, Sequence

from src.services.historical_source_recon import (
    RECON_LABEL,
    CandidatePair,
    _BLOCKING_STATUSES,
    _missing_evidence_for_pair,
)

BRIDGE_VERSION = "1.0"

PAIRED_EXACT_KEY = "PAIRED_EXACT_KEY"
CARD_ONLY = "CARD_ONLY"
RESULT_ONLY = "RESULT_ONLY"
KEY_MISMATCH = "KEY_MISMATCH"
AMBIGUOUS_RESULT_KEY = "AMBIGUOUS_RESULT_KEY"


class BridgeInputError(ValueError):
    """Raised for malformed inputs or missing input files."""


@dataclasses.dataclass(frozen=True)
class BridgeRow:
    candidate_key: str
    pair_status: str
    track_code: str
    race_date: str
    race_number: str
    card_paths: tuple[str, ...]
    result_paths: tuple[str, ...]
    result_official_detected: bool
    result_winner_detected: bool
    missing_evidence: dict[str, str]
    missing_evidence_count: int
    recon_label: str
    note: str


@dataclasses.dataclass(frozen=True)
class BridgeReport:
    bridge_version: str
    rows: tuple[BridgeRow, ...]
    summary: dict


def _key_parts(key: str) -> tuple[str, str, int | None]:
    parts = key.split("|")
    if len(parts) != 3:
        return (key, "", None)
    track, date, race = parts
    race_num = None
    if race.upper().startswith("R") and race[1:].isdigit():
        race_num = int(race[1:])
    return (track, date, race_num)


def _as_rows(value, label: str) -> list[dict]:
    if value is None or isinstance(value, (str, bytes)):
        raise BridgeInputError(f"{label} must be an iterable of row mappings")
    try:
        rows = list(value)
    except TypeError as exc:  # pragma: no cover - defensive
        raise BridgeInputError(f"{label} is not iterable") from exc
    for row in rows:
        if not hasattr(row, "get"):
            raise BridgeInputError(f"{label} contains a non-mapping row: {row!r}")
    return rows


def _missing_evidence_map(pair: CandidatePair) -> dict[str, str]:
    return {
        item.schema_field: item.status
        for item in _missing_evidence_for_pair(pair)
        if item.status in _BLOCKING_STATUSES
    }


def build_bridge(card_inventory_rows, result_index_rows) -> BridgeReport:
    """Pair recon card rows against result-index rows on exact candidate key."""
    card_rows = _as_rows(card_inventory_rows, "card_inventory_rows")
    result_rows = _as_rows(result_index_rows, "result_index_rows")

    cards: dict[str, list[dict]] = {}
    for row in card_rows:
        if str(row.get("classification", "PRE_RACE_CARD_CANDIDATE")) != "PRE_RACE_CARD_CANDIDATE":
            continue
        key = (row.get("candidate_key") or "").strip()
        if key:
            cards.setdefault(key, []).append(row)

    results: dict[str, list[dict]] = {}
    for row in result_rows:
        key = (row.get("candidate_key") or "").strip()
        if key:
            results.setdefault(key, []).append(row)

    # Track|date -> race numbers, for KEY_MISMATCH near-miss detection.
    result_track_date: dict[tuple[str, str], set[int]] = {}
    for key in results:
        track, date, race = _key_parts(key)
        if race is not None:
            result_track_date.setdefault((track, date), set()).add(race)

    rows: list[BridgeRow] = []
    all_keys = sorted(set(cards) | set(results))
    for key in all_keys:
        track, date, race_num = _key_parts(key)
        card_hits = cards.get(key, [])
        result_hits = results.get(key, [])
        card_paths = tuple(sorted(str(r.get("path", "")) for r in card_hits))
        result_paths = tuple(sorted(str(r.get("source_file_path", "")) for r in result_hits))

        official = any(str(r.get("official_status_detected", "")).lower() == "true" for r in result_hits)
        winner = any(str(r.get("winner_detected", "")).lower() == "true" for r in result_hits)

        note = ""
        if card_hits and result_hits:
            if len(result_paths) > 1 or len(result_hits) > 1:
                status = AMBIGUOUS_RESULT_KEY
                note = "multiple result sections share this candidate key; not paired"
            else:
                status = PAIRED_EXACT_KEY
                note = "exact candidate-key match; recon only, not training eligible"
        elif card_hits:
            near = result_track_date.get((track, date), set())
            if near and race_num not in near:
                status = KEY_MISMATCH
                note = f"result index has {track} {date} race(s) {sorted(near)}, not R{race_num}"
            else:
                status = CARD_ONLY
                note = "no retained result section found for this exact key"
        else:
            if len(result_hits) > 1:
                status = AMBIGUOUS_RESULT_KEY
                note = "multiple result sections share this candidate key"
            else:
                status = RESULT_ONLY
                note = "no recon pre-race card candidate found for this exact key"

        pair = CandidatePair(
            candidate_key=key,
            track_code=track,
            race_date=date,
            race_number=race_num,
            pre_race_paths=card_paths,
            result_paths=result_paths if status == PAIRED_EXACT_KEY else (),
            pair_status=status,
            recon_label=RECON_LABEL,
        )
        missing = _missing_evidence_map(pair)

        rows.append(
            BridgeRow(
                candidate_key=key,
                pair_status=status,
                track_code=track,
                race_date=date,
                race_number="" if race_num is None else str(race_num),
                card_paths=card_paths,
                result_paths=result_paths,
                result_official_detected=official,
                result_winner_detected=winner,
                missing_evidence=missing,
                missing_evidence_count=len(missing),
                recon_label=RECON_LABEL,
                note=note,
            )
        )

    summary = {
        "bridge_version": BRIDGE_VERSION,
        "recon_label": RECON_LABEL,
        "card_candidate_key_count": len(cards),
        "result_candidate_key_count": len(results),
        "row_count": len(rows),
        "paired_exact_key_count": sum(1 for r in rows if r.pair_status == PAIRED_EXACT_KEY),
        "card_only_count": sum(1 for r in rows if r.pair_status == CARD_ONLY),
        "result_only_count": sum(1 for r in rows if r.pair_status == RESULT_ONLY),
        "key_mismatch_count": sum(1 for r in rows if r.pair_status == KEY_MISMATCH),
        "ambiguous_result_key_count": sum(1 for r in rows if r.pair_status == AMBIGUOUS_RESULT_KEY),
        "contract_complete_pair_count": sum(
            1 for r in rows if r.pair_status == PAIRED_EXACT_KEY and r.missing_evidence_count == 0
        ),
    }
    return BridgeReport(bridge_version=BRIDGE_VERSION, rows=tuple(rows), summary=summary)


# --------------------------------------------------------------------------- #
# CSV IO                                                                       #
# --------------------------------------------------------------------------- #
def read_card_inventory_csv(path: str | Path) -> list[dict]:
    file_path = Path(path)
    if not file_path.is_file():
        raise BridgeInputError(f"card inventory CSV not found: {path}")
    with file_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_result_index_csv(path: str | Path) -> list[dict]:
    file_path = Path(path)
    if not file_path.is_file():
        raise BridgeInputError(f"result index CSV not found: {path}")
    with file_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


_BRIDGE_COLUMNS = [
    "candidate_key", "pair_status", "track_code", "race_date", "race_number",
    "card_candidate_count", "result_section_count", "result_official_detected",
    "result_winner_detected", "missing_evidence_count", "missing_evidence",
    "recon_label", "note", "card_paths", "result_paths",
]


def write_bridge_artifacts(report: BridgeReport, output_dir: str | Path) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    bridge_csv = out / "historical_pairing_bridge.csv"
    summary_json = out / "historical_pairing_bridge_summary.json"

    with bridge_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_BRIDGE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in report.rows:
            writer.writerow(
                {
                    "candidate_key": row.candidate_key,
                    "pair_status": row.pair_status,
                    "track_code": row.track_code,
                    "race_date": row.race_date,
                    "race_number": row.race_number,
                    "card_candidate_count": len(row.card_paths),
                    "result_section_count": len(row.result_paths),
                    "result_official_detected": row.result_official_detected,
                    "result_winner_detected": row.result_winner_detected,
                    "missing_evidence_count": row.missing_evidence_count,
                    "missing_evidence": ";".join(
                        f"{field}:{status}" for field, status in sorted(row.missing_evidence.items())
                    ),
                    "recon_label": row.recon_label,
                    "note": row.note,
                    "card_paths": " | ".join(row.card_paths),
                    "result_paths": " | ".join(row.result_paths),
                }
            )

    summary_json.write_text(
        json.dumps(report.summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "bridge_csv": bridge_csv.resolve().as_posix(),
        "summary_json": summary_json.resolve().as_posix(),
    }
