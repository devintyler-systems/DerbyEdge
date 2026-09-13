"""Record a bounded human attestation for one persisted source artifact.

This script never derives provenance.  It records an operator's supplied
attestation only after checking that its timestamp is strictly before every
linked race boundary.  It has no training, scoring, feature, or ingestion
dependencies.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "derbyedge.db"
OUTPUT_DIR = ROOT / "output" / "acceptance"
ATTESTATION_COLUMNS = (
    ("attested_by", "TEXT"),
    ("attested_at", "TEXT"),
    ("attestation_note", "TEXT"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_attestation_columns(conn: sqlite3.Connection) -> list[str]:
    """Add only the three authorized metadata columns when absent."""
    existing = _columns(conn, "source_artifacts")
    added: list[str] = []
    for name, kind in ATTESTATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE source_artifacts ADD COLUMN {name} {kind}")
            added.append(name)
    return added


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 instant/date-time without fabricating a timezone."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("attested timestamp must be valid ISO-8601") from exc
    return parsed


def _boundary_datetime(card_date: Any, scheduled_post: Any, start_date: Any) -> tuple[datetime, str] | None:
    """Return a persisted race boundary; never make one up when none exists."""
    if scheduled_post and card_date:
        scheduled = str(scheduled_post).strip()
        try:
            return _parse_iso(scheduled), "race_cards.scheduled_post_time_utc"
        except ValueError:
            try:
                return datetime.strptime(f"{str(card_date)[:10]} {scheduled}", "%Y-%m-%d %I:%M %p"), "race_cards.card_date+scheduled_post_time_utc"
            except ValueError:
                pass
    for value, source in ((card_date, "race_cards.card_date"), (start_date, "horse_starts.start_date")):
        if value:
            try:
                return _parse_iso(str(value)), source
            except ValueError:
                continue
    return None


def _compare_datetimes(left: datetime, right: datetime) -> bool:
    """Compare like-for-like timestamps, normalizing only explicitly zoned values."""
    if (left.tzinfo is None) != (right.tzinfo is None):
        raise ValueError("attested timestamp and persisted race boundary use incompatible timezone precision")
    if left.tzinfo is not None:
        left, right = left.astimezone(timezone.utc), right.astimezone(timezone.utc)
    return left < right


def _status_counts(conn: sqlite3.Connection, artifact_id: int) -> dict[str, int]:
    return dict(sorted(Counter(
        str(row[0] or "NULL")
        for row in conn.execute(
            "SELECT provenance_status FROM horse_starts WHERE source_artifact_id=?", (artifact_id,)
        )
    ).items()))


def _artifact(conn: sqlite3.Connection, artifact_id: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT artifact_id,source_filename,validation_status,declared_as_of_timestamp "
        "FROM source_artifacts WHERE artifact_id=?", (artifact_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"source artifact {artifact_id} does not exist")
    return row


def _validate_boundaries(conn: sqlite3.Connection, artifact_id: int, attested: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT hs.start_id,hs.start_date,rc.card_date,rc.scheduled_post_time_utc
           FROM horse_starts hs LEFT JOIN race_cards rc ON rc.card_id=hs.card_id
           WHERE hs.source_artifact_id=? ORDER BY hs.start_id""",
        (artifact_id,),
    ).fetchall()
    if not rows:
        raise ValueError("source artifact has no linked horse_starts race boundary")
    boundaries: list[dict[str, Any]] = []
    for row in rows:
        resolved = _boundary_datetime(row[2], row[3], row[1])
        if resolved is None:
            raise ValueError(f"linked horse_start {row[0]} has no parseable race boundary")
        boundary, source = resolved
        if not _compare_datetimes(attested, boundary):
            raise ValueError(f"attested timestamp is not strictly before linked race boundary for horse_start {row[0]}")
        boundaries.append({"start_id": int(row[0]), "boundary": boundary.isoformat(), "boundary_source": source})
    return boundaries


def attest(
    conn: sqlite3.Connection,
    *,
    artifact_id: int,
    attested_timestamp: str,
    attested_by: str,
    note: str,
    dry_run: bool = False,
    fail_after_update: bool = False,
) -> dict[str, Any]:
    """Validate and apply exactly one operator attestation in one transaction."""
    if not attested_by.strip():
        raise ValueError("attested_by must not be empty")
    attested = _parse_iso(attested_timestamp)
    artifact = _artifact(conn, artifact_id)
    boundaries = _validate_boundaries(conn, artifact_id, attested)
    before = _status_counts(conn, artifact_id)
    expected_after = Counter(before)
    expected_after["REPAIRABLE"] += expected_after.pop("REPAIRABLE_PARTIAL", 0)
    report: dict[str, Any] = {
        "success": True,
        "dry_run": dry_run,
        "artifact_id": artifact_id,
        "source_filename": artifact["source_filename"],
        "previous_validation_status": artifact["validation_status"],
        "previous_declared_as_of_timestamp": artifact["declared_as_of_timestamp"],
        "attestation_type": "OPERATOR_ATTESTED",
        "attested_timestamp": attested_timestamp,
        "attested_by": attested_by,
        "attestation_note": note,
        "linked_race_count": len(boundaries),
        "race_boundary_sources": sorted({row["boundary_source"] for row in boundaries}),
        "before_provenance_status_counts": before,
        "after_provenance_status_counts": dict(sorted(expected_after.items())),
        "would_add_columns": [name for name, _ in ATTESTATION_COLUMNS if name not in _columns(conn, "source_artifacts")],
    }
    if dry_run:
        return report

    conn.execute("BEGIN IMMEDIATE")
    try:
        report["added_columns"] = ensure_attestation_columns(conn)
        recorded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        conn.execute(
            """UPDATE source_artifacts SET validation_status='OPERATOR_ATTESTED',
               declared_as_of_timestamp=?,attested_by=?,attested_at=?,attestation_note=?
               WHERE artifact_id=?""",
            (attested_timestamp, attested_by, recorded_at, note, artifact_id),
        )
        if fail_after_update:
            raise RuntimeError("injected attestation failure")
        conn.execute(
            """UPDATE horse_starts SET source_as_of_ts=?,provenance_status='REPAIRABLE'
               WHERE source_artifact_id=? AND provenance_status='REPAIRABLE_PARTIAL'""",
            (attested_timestamp, artifact_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    report["attested_at"] = recorded_at
    report["after_provenance_status_counts"] = _status_counts(conn, artifact_id)
    return report


def run(
    db_path: Path = DB_PATH,
    output_dir: Path = OUTPUT_DIR,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run an attestation and emit the corresponding acceptance report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        report = attest(conn, **kwargs)
    finally:
        conn.close()
    path = output_dir / f"attestation_report_{kwargs['artifact_id']}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Record one pre-race operator source-artifact attestation")
    parser.add_argument("--artifact-id", type=int, required=True)
    parser.add_argument("--attested-timestamp", required=True)
    parser.add_argument("--attested-by", required=True)
    parser.add_argument("--note", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    try:
        report = run(
            args.db, args.output_dir, artifact_id=args.artifact_id,
            attested_timestamp=args.attested_timestamp, attested_by=args.attested_by,
            note=args.note, dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"attestation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "artifact_id": report["artifact_id"], "dry_run": report["dry_run"],
        "before": report["before_provenance_status_counts"],
        "after": report["after_provenance_status_counts"],
        "report_path": report["report_path"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
