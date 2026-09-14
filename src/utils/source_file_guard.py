"""
source_file_guard.py — ingest-time source file stamping.

Call stamp_source(path) at the exact moment an ingest reader opens a source
file (DK Advanced markdown, DK Basic-tab XLSX, TwinSpires summary). It logs
SHA-256 + last-modified timestamp so "file actually used" is visible
immediately at ingest time, not discovered after the fact.

This does NOT replace the Downloads-staging discipline in
scripts/stage_downloads.py — it is a second, independent tripwire in case a
file gets ingested from somewhere other than fixtures/ (e.g. an ad-hoc path
during debugging).

Integration (add to each ingest reader's entry point, e.g.
draftkings_basic_csv.py, draftkings_markdown.py, twinspires_markdown.py):

    from src.utils.source_file_guard import stamp_source, warn_if_outside_fixtures

    def load_xlsx(path: Path, ...):
        stamp = stamp_source(path)
        warn_if_outside_fixtures(path)
        logger.info("ingest_source_stamp", extra=stamp)
        ...
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("derbyedge.ingest.source_guard")

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "draftkings_racedata_pdfs" / "fixtures"


@dataclass(frozen=True)
class SourceStamp:
    path: str
    sha256: str
    mtime_utc: str
    size_bytes: int
    stamped_at_utc: str


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stamp_source(path: Path) -> dict:
    """Return + log sha256/mtime/size for a source file at ingest time."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"source_file_guard: source file does not exist: {path}")

    stamp = SourceStamp(
        path=str(path.resolve()),
        sha256=_sha256_of(path),
        mtime_utc=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        size_bytes=path.stat().st_size,
        stamped_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    payload = asdict(stamp)
    logger.info(
        "INGEST_SOURCE_STAMP path=%s sha256=%s mtime_utc=%s size_bytes=%d",
        payload["path"], payload["sha256"], payload["mtime_utc"], payload["size_bytes"],
    )
    return payload


def warn_if_outside_fixtures(path: Path) -> bool:
    """Return True (and log a warning) if path is not under fixtures/.

    Reading directly from Downloads mid-pipeline is exactly the failure mode
    that produced the card_id=74 ML/ODDS misclassification incident. This is
    a warning, not a hard gate, so it never blocks a legitimate one-off debug
    read — but it must never be silent.
    """
    path = Path(path).resolve()
    try:
        path.relative_to(FIXTURES_DIR.resolve())
        return False
    except ValueError:
        logger.warning(
            "INGEST_SOURCE_OUTSIDE_FIXTURES path=%s expected_root=%s "
            "-- run scripts/stage_downloads.py before ingest",
            path, FIXTURES_DIR,
        )
        return True
