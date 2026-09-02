"""Print the text extracted from a 1/ST PDF for parser diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.pdf_ingest import _extract_text
from src.ingest.firstbet_pdf import parse_firstbet_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "pdf",
        nargs="?",
        type=Path,
        default=Path("1stbet_racedata_pdfs/New Race/Saratoga_R8_9-2-26.pdf"),
    )
    parser.add_argument("--chars", type=int, default=30_000)
    parser.add_argument("--normalized", action="store_true")
    args = parser.parse_args()
    pdf_bytes = args.pdf.read_bytes()
    source_sha = hashlib.sha256(pdf_bytes).hexdigest()
    text = _extract_text(pdf_bytes)
    print(f"chars={len(text)} lines={len(text.splitlines())}")
    if args.normalized:
        payload, audit = parse_firstbet_text(
            text,
            filename=args.pdf.name,
            sha256=source_sha,
            uploaded_at_utc="2026-09-02T20:24:00Z",
        )
        print(json.dumps({"source": payload["source"], "race": payload["race"], "entries": payload["entries"], "audit": audit}, indent=2))
    else:
        print(text[: args.chars])


if __name__ == "__main__":
    main()
