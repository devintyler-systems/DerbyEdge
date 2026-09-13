"""Add and conservatively backfill durable provenance fields for horse_starts.

This is an additive, idempotent migration.  It never writes source artifacts,
features, scores, entries, or any model-related data.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "db" / "derbyedge.db"
OUTPUT_DIR = ROOT / "output" / "acceptance"
STATUSES = ("REPAIRABLE", "REPAIRABLE_PARTIAL", "INELIGIBLE_NO_ARTIFACT", "INELIGIBLE_POSTRACE")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_provenance_columns(conn: sqlite3.Connection) -> list[str]:
    """Add only missing approved horse_starts columns."""
    existing, added = _columns(conn, "horse_starts"), []
    # The actual source_artifacts primary key is artifact_id, not id.
    definitions = (
        ("source_artifact_id", "source_artifact_id INTEGER REFERENCES source_artifacts(artifact_id)"),
        ("source_as_of_ts", "source_as_of_ts TEXT"),
        ("provenance_status", "provenance_status TEXT CHECK(provenance_status IN ('REPAIRABLE','REPAIRABLE_PARTIAL','INELIGIBLE_NO_ARTIFACT','INELIGIBLE_POSTRACE'))"),
    )
    for name, definition in definitions:
        if name not in existing:
            conn.execute(f"ALTER TABLE horse_starts ADD COLUMN {definition}")
            added.append(name)
    return added


def _pre_race(as_of: Any, race_date: Any) -> bool:
    return bool(as_of and race_date and str(as_of)[:10] < str(race_date)[:10])


def _at_or_after(as_of: Any, race_date: Any) -> bool:
    return bool(as_of and race_date and str(as_of)[:10] >= str(race_date)[:10])


def _artifact_by_document(conn: sqlite3.Connection) -> dict[str, tuple[int, str | None]]:
    """Return only exact, durable source-document-to-artifact matches.

    A provider label or a DK import row is deliberately not an artifact link.
    Future source_artifact rows with an exact SHA can be safely resolved.
    """
    rows = conn.execute("SELECT artifact_id,sha256,declared_as_of_timestamp FROM source_artifacts").fetchall()
    lookup: dict[str, tuple[int, str | None]] = {}
    for artifact_id, sha256, as_of in rows:
        if sha256:
            lookup[f"dk_markdown:{sha256}"] = (int(artifact_id), as_of)
            lookup[str(sha256)] = (int(artifact_id), as_of)
    return lookup


def backfill_provenance(conn: sqlite3.Connection, *, fail_after: int | None = None) -> dict[str, Any]:
    """Backfill in one transaction; test-only failure injection proves rollback."""
    artifact_lookup = _artifact_by_document(conn)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(
        """SELECT hs.start_id,hs.source_document_id,rc.card_date
           FROM horse_starts hs LEFT JOIN race_cards rc ON rc.card_id=hs.card_id
           ORDER BY hs.start_id"""
    )]
    counts: Counter[str] = Counter()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for index, row in enumerate(rows, start=1):
            resolved = artifact_lookup.get(row.get("source_document_id") or "")
            if resolved is None:
                artifact_id = as_of = None
                status = "INELIGIBLE_NO_ARTIFACT"
            else:
                artifact_id, as_of = resolved
                if as_of is None or not row.get("card_date"):
                    status = "REPAIRABLE_PARTIAL"
                elif _pre_race(as_of, row["card_date"]):
                    status = "REPAIRABLE"
                elif _at_or_after(as_of, row["card_date"]):
                    # The requested backfill policy intentionally keeps this
                    # reparable-but-invalid-as-of case distinct from no artifact.
                    status = "REPAIRABLE_PARTIAL"
                else:
                    status = "REPAIRABLE_PARTIAL"
            conn.execute("UPDATE horse_starts SET source_artifact_id=?,source_as_of_ts=?,provenance_status=? WHERE start_id=?", (artifact_id, as_of, status, row["start_id"]))
            counts[status] += 1
            if fail_after is not None and index >= fail_after:
                raise RuntimeError("injected backfill failure")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"row_count": len(rows), "classification_counts": dict(sorted(counts.items())), "resolvable_document_count": sum(1 for row in rows if (row.get("source_document_id") or "") in artifact_lookup)}


def _family_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    # Keep the migration independent from model/audit modules.  It reports
    # family dimensions directly from the persisted race card only.
    rows = conn.execute("""SELECT rc.surface,rc.distance_furlongs,rc.stakes_name,rc.race_class,hs.provenance_status
                           FROM horse_starts hs LEFT JOIN race_cards rc ON rc.card_id=hs.card_id""").fetchall()
    from src.services.model_family import classify_model_family
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for surface, distance, stakes, race_class, status in rows:
        grouped[classify_model_family(surface, distance, stakes, race_class)][status or "INELIGIBLE_NO_ARTIFACT"] += 1
    return [{"model_family": family, **{status: grouped[family][status] for status in STATUSES}} for family in sorted(grouped)]


def migrate(db_path: Path = DB_PATH, output_dir: Path = OUTPUT_DIR, *, fail_after: int | None = None) -> dict[str, Any]:
    """Run the complete additive migration and emit its acceptance report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        added = ensure_provenance_columns(conn)
        # ALTER TABLE DDL is committed before the required one-transaction
        # data backfill, so failure cannot leave a partially backfilled table.
        conn.commit()
        backfill = backfill_provenance(conn, fail_after=fail_after)
        family_counts = _family_counts(conn)
    finally:
        conn.close()
    verify_conn = sqlite3.connect(db_path)
    try:
        present = sorted({"source_artifact_id", "source_as_of_ts", "provenance_status"}.intersection(_columns(verify_conn, "horse_starts")))
    finally:
        verify_conn.close()
    report = {"success": True, "database_path": str(db_path), "added_columns": added, "provenance_columns_present": present, "source_artifact_key": "artifact_id", "source_artifact_as_of_column": "declared_as_of_timestamp", "schema_gap": "NO_DURABLE_HORSE_STARTS_TO_SOURCE_ARTIFACTS_JOIN" if not backfill["resolvable_document_count"] else None, "backfill": backfill, "per_family": family_counts, "executed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    report_path = output_dir / "horse_starts_provenance_migration_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Additive horse_starts provenance migration")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    try:
        report = migrate(args.db, args.output_dir)
    except Exception as exc:
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("family,REPAIRABLE,REPAIRABLE_PARTIAL,INELIGIBLE_NO_ARTIFACT")
    for row in report["per_family"]:
        print(f"{row['model_family']},{row['REPAIRABLE']},{row['REPAIRABLE_PARTIAL']},{row['INELIGIBLE_NO_ARTIFACT']}")
    print(report["report_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
