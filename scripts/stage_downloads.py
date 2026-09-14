"""
stage_downloads.py — Downloads-folder staging discipline for DerbyEdge Engine.

Purpose
-------
Prevents the card_id=74 class of incident: a stale/incomplete DK or TwinSpires
export sitting at the Downloads path gets silently overwritten by the real
file mid-workflow, AFTER an ingest step has already read the stale version.

Rule: every source file (DK Advanced markdown, DK Basic-tab XLSX, TwinSpires
summary) gets moved OUT of Downloads and INTO fixtures/ the moment it lands,
before any ingest command runs. Ingest scripts should never read directly
from a Downloads path.

Usage
-----
    python scripts/stage_downloads.py
    python scripts/stage_downloads.py --dry-run
    python scripts/stage_downloads.py --downloads "D:\Custom\Downloads"

Env overrides
-------------
    DE_DOWNLOADS_DIR   default: ~/Downloads
    DE_FIXTURES_DIR    default: <repo_root>/draftkings_racedata_pdfs/fixtures

Behavior
--------
- Scans Downloads (non-recursive) for files matching known DK/TwinSpires
  export patterns.
- For each match: computes SHA-256 + captures source last-modified time,
  moves the file into fixtures/ with its original name preserved, and
  appends one row to fixtures/_staging_manifest.csv.
- Refuses to silently clobber an existing staged file with the same target
  name; on collision it suffixes the new file with the source mtime so both
  versions are preserved, since this rule (never conflate two source files)
  is the entire point.
- Prints a one-line summary per staged file: name, sha256[:12], mtime.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DOWNLOADS = Path.home() / "Downloads"
DEFAULT_FIXTURES = REPO_ROOT / "draftkings_racedata_pdfs" / "fixtures"
MANIFEST_NAME = "_staging_manifest.csv"

SOURCE_PATTERNS = [
    "*_DK_Horse_R*.md",
    "*_DK_Horse_R*_Speed_Power_Style*.md",
    "*_DK_Horse_R*.xlsx",
    "*TwinSpires*Summary*.md",
]

MANIFEST_FIELDS = [
    "staged_at_utc",
    "original_filename",
    "staged_filename",
    "source_mtime_utc",
    "sha256",
    "size_bytes",
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_mtime_utc(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def find_candidates(downloads_dir: Path) -> list[Path]:
    seen: dict[str, Path] = {}
    for pattern in SOURCE_PATTERNS:
        for p in downloads_dir.glob(pattern):
            if p.is_file():
                seen[str(p.resolve())] = p
    return sorted(seen.values(), key=lambda p: p.name)


def append_manifest(fixtures_dir: Path, row: dict) -> None:
    manifest_path = fixtures_dir / MANIFEST_NAME
    write_header = not manifest_path.exists()
    with manifest_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def stage_file(src: Path, fixtures_dir: Path, dry_run: bool) -> dict:
    digest = sha256_of(src)
    mtime = source_mtime_utc(src)
    size = src.stat().st_size

    target = fixtures_dir / src.name
    if target.exists():
        existing_digest = sha256_of(target)
        if existing_digest == digest:
            target_name = src.name
        else:
            stamp = mtime.replace(":", "-")
            target_name = f"{src.stem}__{stamp}{src.suffix}"
            target = fixtures_dir / target_name
    else:
        target_name = src.name

    row = {
        "staged_at_utc": datetime.now(timezone.utc).isoformat(),
        "original_filename": src.name,
        "staged_filename": target_name,
        "source_mtime_utc": mtime,
        "sha256": digest,
        "size_bytes": size,
    }

    if not dry_run:
        fixtures_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        append_manifest(fixtures_dir, row)

    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, default=None)
    parser.add_argument("--fixtures", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    downloads_dir = args.downloads or Path(
        __import__("os").environ.get("DE_DOWNLOADS_DIR", str(DEFAULT_DOWNLOADS))
    )
    fixtures_dir = args.fixtures or Path(
        __import__("os").environ.get("DE_FIXTURES_DIR", str(DEFAULT_FIXTURES))
    )

    if not downloads_dir.exists():
        print(f"ERROR: Downloads dir not found: {downloads_dir}", file=sys.stderr)
        return 2

    candidates = find_candidates(downloads_dir)
    if not candidates:
        print(f"No matching source files found in {downloads_dir}")
        return 0

    print(f"Staging {len(candidates)} file(s) from {downloads_dir} -> {fixtures_dir}"
          f"{' [DRY RUN]' if args.dry_run else ''}")
    for src in candidates:
        row = stage_file(src, fixtures_dir, args.dry_run)
        print(
            f"  {row['original_filename']} -> {row['staged_filename']} | "
            f"sha256={row['sha256'][:12]}... | mtime={row['source_mtime_utc']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
