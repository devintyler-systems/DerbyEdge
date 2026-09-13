"""Explicit, source-isolated TwinSpires Speed/Power/Style ingestion command."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingest.twinspires_markdown import parse_twinspires_markdown  # noqa: E402
from src.services.twinspires_intake import (  # noqa: E402
    persist_twinspires_card, validate_twinspires_card, write_reconciliation_csv,
)
from src.utils.db import DB_PATH, get_connection  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest a TwinSpires Markdown source into its isolated observation lane")
    parser.add_argument("--card-id", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-as-of", help="Explicit timezone-aware ISO-8601 source timestamp; omitted means UNPROVEN")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "acceptance")
    args = parser.parse_args()
    raw = args.source.read_bytes()
    card = parse_twinspires_markdown(raw, source_filename=args.source.name, declared_as_of=args.source_as_of)
    conn = get_connection()
    try:
        validation = validate_twinspires_card(conn, args.card_id, card)
        artifact_id = persist_twinspires_card(conn, card, validation, raw_bytes=raw)
    finally:
        conn.close()
    reconciliation_path = args.output_dir / f"twinspires_card_{args.card_id}_reconciliation.csv"
    validation_path = args.output_dir / f"twinspires_card_{args.card_id}_source_validation.json"
    write_reconciliation_csv(validation, reconciliation_path)
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.write_text(json.dumps({
        "card_id": args.card_id, "source_provider": "twinspires", "source_sha256": card.source_sha256,
        "parser_version": card.parser_version, "as_of_status": card.as_of_status,
        "declared_as_of_timestamp": card.declared_as_of, "normalized_record_count": len(card.records),
        "validation_passed": validation.passed, "errors": validation.errors, "warnings": validation.warnings,
        "artifact_id": artifact_id,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"artifact_id={artifact_id} sha256={card.source_sha256} parser_version={card.parser_version}")
    print(f"records={len(card.records)} as_of_status={card.as_of_status} validation={'PASS' if validation.passed else 'FAIL'}")
    print(f"reconciliation_csv={reconciliation_path}\nsource_validation_json={validation_path}")
    return 0 if validation.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
