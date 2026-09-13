"""Proven pre-post market captures from DK Basic and TwinSpires summaries."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from src.ingest.draftkings_basic_csv import (
    DraftKingsBasicCSVError, normalize_program_number, parse_draftkings_basic_csv,
)
from src.ingest.twinspires_markdown import parse_twinspires_markdown
from src.utils.horse_norm import normalize_horse_name


class MarketSnapshotError(ValueError):
    """The source cannot establish one complete, pre-post market capture."""


def fractional_odds(raw: str | None) -> tuple[int, int]:
    """Parse displayed to-one odds: 5/2 or 9 (meaning 9/1)."""
    value = (raw or "").strip()
    pieces = value.split("/")
    if len(pieces) not in (1, 2):
        raise MarketSnapshotError(f"invalid current ODDS: {raw!r}")
    try:
        if any(not part or not all(ch.isdigit() or ch == "." for ch in part) for part in pieces):
            raise InvalidOperation
        numerator = Decimal(pieces[0])
        denominator = Decimal(pieces[1]) if len(pieces) == 2 else Decimal(1)
        if not numerator.is_finite() or not denominator.is_finite() or numerator <= 0 or denominator <= 0:
            raise InvalidOperation
        ratio = Fraction(numerator) / Fraction(denominator)
    except (InvalidOperation, ValueError, ZeroDivisionError):
        raise MarketSnapshotError(f"invalid current ODDS: {raw!r}") from None
    return ratio.numerator, ratio.denominator


def _pre_post_time(captured_at: str | None, scheduled_post: str | None) -> str:
    if not captured_at:
        raise MarketSnapshotError("capture timestamp is required")
    if not scheduled_post:
        raise MarketSnapshotError("scheduled post time is required")
    try:
        capture = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        post = datetime.fromisoformat(scheduled_post.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise MarketSnapshotError("capture or scheduled post timestamp is invalid") from None
    if capture.tzinfo is None or post.tzinfo is None:
        raise MarketSnapshotError("capture and scheduled post need explicit timezones")
    if capture >= post:
        raise MarketSnapshotError("capture timestamp must precede scheduled post")
    return capture.astimezone(timezone.utc).isoformat()


def _ensure_capture_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(odds_snapshots)")}
    for name in ("source_provider", "source_artifact_sha256", "program_number"):
        if name not in columns:
            conn.execute(f"ALTER TABLE odds_snapshots ADD COLUMN {name} TEXT")


def ingest_market_snapshot(
    conn: sqlite3.Connection, card_id: int, source: str | Path | bytes, *,
    provider: str, captured_at: str | None,
) -> int:
    """Insert one complete capture atomically; never infer time or identity."""
    if provider not in {"draftkings", "twinspires"}:
        raise MarketSnapshotError(f"unsupported market source: {provider}")
    race = conn.execute(
        "SELECT scheduled_post_time_utc FROM race_cards WHERE card_id=?", (card_id,)
    ).fetchone()
    if race is None:
        raise MarketSnapshotError(f"card_id={card_id} not found")
    capture = _pre_post_time(captured_at, race[0])
    try:
        if provider == "draftkings":
            parsed = parse_draftkings_basic_csv(source)
            rows = [(row.program_number, row.horse_name, row.current_odds) for row in parsed.rows]
        else:
            parsed = parse_twinspires_markdown(source)
            if parsed.parser_errors:
                raise MarketSnapshotError("TwinSpires parse failed: " + "; ".join(parsed.parser_errors))
            rows = [(row.program_number, row.horse_name, row.current_odds) for row in parsed.records]
    except (DraftKingsBasicCSVError, OSError, UnicodeError) as exc:
        raise MarketSnapshotError(str(exc)) from exc

    entries = conn.execute(
        """SELECT e.entry_id,e.program_number,e.scratch_flag,h.name
           FROM entries e JOIN horses h ON h.horse_id=e.horse_id
           WHERE e.card_id=?""", (card_id,)
    ).fetchall()
    by_program = {}
    for entry_id, program, scratched, name in entries:
        key = normalize_program_number(program)
        if key is None or key in by_program:
            raise MarketSnapshotError("canonical program numbers are missing or ambiguous")
        by_program[key] = (int(entry_id), bool(scratched), str(name))
    active = {entry_id for entry_id, scratched, _ in by_program.values() if not scratched}
    resolved: dict[int, tuple[str, int, int]] = {}
    for program, horse_name, raw_odds in rows:
        key = normalize_program_number(program)
        if key not in by_program:
            raise MarketSnapshotError(f"source program {program!r} is not on card {card_id}")
        entry_id, scratched, canonical_name = by_program[key]
        if scratched:
            continue
        if entry_id in resolved:
            raise MarketSnapshotError(f"duplicate current odds for program {program!r}")
        if not horse_name or normalize_horse_name(horse_name) != normalize_horse_name(canonical_name):
            raise MarketSnapshotError(f"runner identity mismatch at program {program!r}")
        numerator, denominator = fractional_odds(raw_odds)
        resolved[entry_id] = (str(program), numerator, denominator)
    if not active or set(resolved) != active:
        raise MarketSnapshotError(f"incomplete ODDS capture: {len(resolved)}/{len(active)} active runners")

    conn.execute("SAVEPOINT market_capture")
    try:
        _ensure_capture_columns(conn)
        existing = conn.execute(
            """SELECT entry_id,odds_numerator,odds_denominator FROM odds_snapshots
               WHERE snapshot_time=? AND source='book' AND source_provider=?
               AND source_artifact_sha256=? AND entry_id IN
               (SELECT entry_id FROM entries WHERE card_id=?)""",
            (capture, provider, parsed.source_sha256, card_id),
        ).fetchall()
        if existing:
            if len(existing) != len(resolved) or any(
                entry_id not in resolved or
                float(num) / float(den) != resolved[entry_id][1] / resolved[entry_id][2]
                for entry_id, num, den in existing
            ):
                raise MarketSnapshotError("conflicting capture already exists for this source and timestamp")
            inserted = 0
        else:
            conn.executemany(
                """INSERT INTO odds_snapshots
                   (entry_id,snapshot_time,odds_numerator,odds_denominator,source,
                    source_provider,source_artifact_sha256,program_number)
                   VALUES (?,?,?,?, 'book',?,?,?)""",
                [(entry_id, capture, numerator, denominator, provider, parsed.source_sha256, program)
                 for entry_id, (program, numerator, denominator) in resolved.items()],
            )
            inserted = len(resolved)
        conn.execute("RELEASE SAVEPOINT market_capture")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT market_capture")
        conn.execute("RELEASE SAVEPOINT market_capture")
        raise
    conn.commit()
    return inserted


def latest_valid_market_snapshot(
    conn: sqlite3.Connection, card_id: int, entry_ids: list[int],
) -> dict[int, float] | None:
    """Read the latest complete, unambiguous, still-pre-post book capture."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(odds_snapshots)")}
    if not {"source_provider", "source_artifact_sha256"} <= columns:
        return None
    race = conn.execute(
        "SELECT scheduled_post_time_utc FROM race_cards WHERE card_id=?", (card_id,)
    ).fetchone()
    if not race or not race[0]:
        return None
    rows = conn.execute(
        """SELECT s.entry_id,s.snapshot_time,s.implied_prob,
                  s.source_provider,s.source_artifact_sha256
           FROM odds_snapshots s JOIN entries e ON e.entry_id=s.entry_id
           WHERE e.card_id=? AND e.scratch_flag=0 AND s.source='book'""",
        (card_id,),
    ).fetchall()
    groups: dict[tuple[str, str, str], list[tuple[int, float]]] = defaultdict(list)
    for entry_id, timestamp, probability, provider, sha in rows:
        if provider in {"draftkings", "twinspires"} and sha:
            groups[(timestamp, provider, sha)].append((int(entry_id), float(probability)))
    eligible = []
    expected = set(entry_ids)
    for (timestamp, _provider, _sha), quotes in groups.items():
        try:
            normalized_time = _pre_post_time(timestamp, race[0])
        except MarketSnapshotError:
            continue
        observed = dict(quotes)
        if len(observed) != len(quotes) or set(observed) != expected:
            continue
        if not all(0 < value < 1 for value in observed.values()):
            continue
        eligible.append((normalized_time, observed))
    if not eligible:
        return None
    eligible.sort(key=lambda item: item[0], reverse=True)
    if len(eligible) > 1 and eligible[0][0] == eligible[1][0]:
        return None  # competing books at the same capture time
    return eligible[0][1]
