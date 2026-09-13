"""One-time, transactional DK source-artifact backfill.

Only an on-disk document whose bytes hash to the retained import SHA can become
an artifact. Filename dates remain explicitly unproven and produce PARTIAL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _date_from_filename(value: str) -> str | None:
    match = re.search(r"_R\d+_(\d{1,2}-\d{1,2}-(?:\d{2}|\d{4}))", Path(value).name, re.I)
    if not match:
        return None
    for fmt in ("%m-%d-%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(match.group(1), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_import_fk(conn: sqlite3.Connection) -> None:
    if "source_artifact_id" not in _columns(conn, "dk_markdown_imports"):
        conn.execute("ALTER TABLE dk_markdown_imports ADD COLUMN source_artifact_id INTEGER REFERENCES source_artifacts(artifact_id)")
        conn.commit()


def _family_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    from src.services.model_family import classify_model_family
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for row in conn.execute("SELECT rc.surface,rc.distance_furlongs,rc.stakes_name,rc.race_class,hs.provenance_status FROM horse_starts hs LEFT JOIN race_cards rc ON rc.card_id=hs.card_id"):
        grouped[classify_model_family(*row[:4])][row[4] or "INELIGIBLE_NO_ARTIFACT"] += 1
    return {family: dict(sorted(counts.items())) for family, counts in sorted(grouped.items())}


def backfill(conn: sqlite3.Connection, *, fail_after: int | None = None) -> dict:
    """Backfill exact retained-byte artifacts in one transaction."""
    _ensure_import_fk(conn)
    imports = list(conn.execute("SELECT import_id,file_sha256,source_filename,source_path,parser_version,card_id FROM dk_markdown_imports ORDER BY import_id"))
    before = _family_counts(conn)
    created = linked = skipped = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for index, (import_id, sha, filename, source_path, parser_version, card_id) in enumerate(imports, 1):
            declared = _date_from_filename(filename)
            raw_bytes = None
            if declared and source_path and Path(source_path).is_file():
                candidate = Path(source_path).read_bytes()
                if hashlib.sha256(candidate).hexdigest() == sha:
                    raw_bytes = candidate
            if raw_bytes is None:
                skipped += 1
                # No raw document/verified SHA means no durable artifact.
                conn.execute("UPDATE horse_starts SET source_artifact_id=NULL,source_as_of_ts=NULL,provenance_status='INELIGIBLE_NO_ARTIFACT' WHERE source_document_id=?", (f"dk_markdown:{sha}",))
            else:
                conn.execute(
                    """INSERT INTO source_artifacts (source_provider,source_filename,source_path,raw_bytes,sha256,parser_version,
                       ingestion_timestamp,declared_as_of_timestamp,as_of_status,validation_status,validation_errors_json,
                       normalized_record_count,normalized_records_json,card_id)
                       VALUES ('draftkings_markdown',?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_provider,sha256,parser_version,card_id) DO NOTHING""",
                    (filename, source_path, raw_bytes, sha, parser_version, datetime.now(timezone.utc).isoformat(), declared,
                     "UNPROVEN", "UNPROVEN", json.dumps({"as_of_provenance": "FILENAME_DERIVED"}, sort_keys=True), 0, "[]", card_id),
                )
                artifact = conn.execute("SELECT artifact_id FROM source_artifacts WHERE source_provider='draftkings_markdown' AND sha256=? AND parser_version=? AND card_id=?", (sha, parser_version, card_id)).fetchone()
                if artifact is None:
                    raise RuntimeError("artifact insert/select failed")
                artifact_id = int(artifact[0]); created += 1
                conn.execute("UPDATE dk_markdown_imports SET source_artifact_id=? WHERE import_id=?", (artifact_id, import_id))
                cursor = conn.execute("UPDATE horse_starts SET source_artifact_id=?,source_as_of_ts=?,provenance_status='REPAIRABLE_PARTIAL' WHERE source_document_id=?", (artifact_id, declared, f"dk_markdown:{sha}"))
                linked += cursor.rowcount
            if fail_after is not None and index >= fail_after:
                raise RuntimeError("injected backfill failure")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"imports_seen": len(imports), "artifact_insert_or_existing_count": created, "horse_starts_linked": linked, "imports_skipped": skipped, "before": before, "after": _family_counts(conn)}


def run(db_path: Path, output_dir: Path, *, fail_after: int | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        result = backfill(conn, fail_after=fail_after)
    finally:
        conn.close()
    report = {"success": True, "database_path": str(db_path), "as_of_provenance": "FILENAME_DERIVED", "validation_status": "UNPROVEN", "repairable_status": "REPAIRABLE_PARTIAL", **result, "executed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    path = output_dir / "dk_backfill_report.json"; path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(path); return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Transactional DK source-artifact backfill")
    parser.add_argument("--db", type=Path, default=ROOT / "db" / "derbyedge.db")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "acceptance")
    args = parser.parse_args()
    try:
        report = run(args.db, args.output_dir)
    except Exception as exc:
        print(f"backfill failed: {type(exc).__name__}: {exc}", file=sys.stderr); return 1
    print(json.dumps(report["after"], sort_keys=True)); print(report["report_path"]); return 0


if __name__ == "__main__":
    raise SystemExit(main())
