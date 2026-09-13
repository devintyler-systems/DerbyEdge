"""Read-only Equibase full-card result slicer and race-level indexing aid.

This service turns retained ``eqb_*_fullcard.pdf`` charts into one race-level
candidate *reference* row per deterministically recognized race section.  It

* never ingests into canonical tables, mutates SQLite, trains, scores,
  calibrates, or changes readiness logic;
* never writes split PDFs and never rewrites the source files;
* computes SHA-256 by streaming the bytes read-only.

Text extraction reuses the repository's approved pdfplumber helper
(:func:`src.services.pdf_ingest._extract_text`).  Every emitted row carries
``status = RECON_ONLY_NOT_ELIGIBLE``; a result index is *not* outcome-provenance
sufficiency on its own.
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from src.derbyedge.tracks import resolve_track

RESULT_RECON_LABEL = "RECON_ONLY_NOT_ELIGIBLE"
SLICER_VERSION = "1.0"

# Only retained Equibase full-card result PDFs are in scope.
_FULLCARD_NAME_RE = re.compile(r"^eqb_.+_fullcard\.pdf$", re.I)

# Repeated chart markers.  A race section header always carries track, a
# "Month D, YYYY" date, and (normally) a race number on one line.
_SECTION_START_RE = re.compile(
    r"^(?P<track>.+?) - (?P<month>[A-Z][a-z]+) (?P<day>\d{1,2}), (?P<year>\d{4}) - Race\b(?P<rest>.*)$"
)
_RACE_NUMBER_RE = re.compile(r"^\s*(\d{1,2})\b")
_COPYRIGHT_RE = re.compile(r"Equibase Company LLC", re.I)
_WINNER_RE = re.compile(r"^\s*Winner:\s*(?P<name>[^,\n]+?)\s*,", re.M)
_OFFICIAL_HINT_RE = re.compile(
    r"(Fractional Times|Final Time|Total WPS Pool|Wager Type\s+Winning Numbers)", re.I
)

UNCERTAINTY_CODES = (
    "MISSING_RACE_NUMBER",
    "AMBIGUOUS_TRACK_IDENTITY",
    "MISSING_RACE_DATE",
    "NO_RACE_SECTIONS_FOUND",
    "OFFICIAL_STATUS_NOT_FOUND",
    "WINNER_NOT_FOUND",
    "PDF_TEXT_EXTRACTION_FAILED",
)

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}


class ResultSliceRootError(ValueError):
    """Raised when a supplied scan root is missing or is not a local directory."""


@dataclasses.dataclass(frozen=True)
class RaceResultRef:
    source_file_path: str
    source_file_sha256: str
    track_code_or_name: str
    race_date: str
    race_number: int | None
    candidate_key: str
    official_status_detected: bool
    winner_detected: bool
    winner_name: str
    extraction_confidence: str
    uncertainty_code: str
    status: str


@dataclasses.dataclass(frozen=True)
class ResultIndex:
    roots: tuple[str, ...]
    slicer_version: str
    rows: tuple[RaceResultRef, ...]
    summary: dict


# --------------------------------------------------------------------------- #
# Filesystem primitives (strictly read-only)                                   #
# --------------------------------------------------------------------------- #
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_pdf_text(path: Path) -> str | None:
    """Return chart text, or ``None`` when extraction is not possible."""
    try:
        from src.services.pdf_ingest import _extract_text
    except Exception:  # pragma: no cover - import guard
        return None
    try:
        text = _extract_text(path.read_bytes())
    except Exception:
        return None
    return text or None


# --------------------------------------------------------------------------- #
# Header / identity helpers                                                     #
# --------------------------------------------------------------------------- #
def _iso_date(month: str, day: str, year: str) -> str | None:
    month_num = _MONTHS.get(month)
    if month_num is None:
        return None
    try:
        return datetime(int(year), month_num, int(day)).date().isoformat()
    except ValueError:
        return None


def _resolve_track_code(header_track: str | None, filename_track: str | None) -> tuple[str, bool]:
    """Return ``(track_code_or_name, ambiguous)``."""
    header_code = None
    if header_track:
        resolution = resolve_track(track_name=header_track)
        header_code = resolution["track_code"]
    file_code = None
    if filename_track:
        resolution = resolve_track(track_code=filename_track)
        file_code = resolution["track_code"] or filename_track.strip().upper() or None

    if header_code and file_code:
        return (header_code, header_code != file_code)
    if header_code:
        return (header_code, False)
    if file_code and header_track:
        # A header track that does not resolve, with a filename code that does,
        # is an unresolved identity -- treat as ambiguous rather than trusting one.
        return (file_code, True)
    if file_code:
        return (file_code, False)
    return ((header_track or "").strip(), bool(header_track))


# --------------------------------------------------------------------------- #
# Core slicing                                                                 #
# --------------------------------------------------------------------------- #
def _section_bounds(lines: list[str]) -> list[tuple[int, int, re.Match[str]]]:
    starts = [
        (idx, _SECTION_START_RE.match(line))
        for idx, line in enumerate(lines)
    ]
    starts = [(idx, match) for idx, match in starts if match]
    bounds: list[tuple[int, int, re.Match[str]]] = []
    for position, (idx, match) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        # Prefer to close the section at its copyright line when present.
        for cursor in range(idx + 1, end):
            if _COPYRIGHT_RE.search(lines[cursor]):
                end = cursor + 1
                break
        bounds.append((idx, end, match))
    return bounds


def _slice_section(
    section_text: str,
    header: re.Match[str],
    *,
    source_file_path: str,
    source_file_sha256: str,
    filename_track: str | None,
    filename_date: str | None,
) -> RaceResultRef:
    codes: list[str] = []

    header_track = header.group("track").strip()
    race_date = _iso_date(header.group("month"), header.group("day"), header.group("year"))
    if race_date is None:
        race_date = filename_date or ""
        if not race_date:
            codes.append("MISSING_RACE_DATE")

    number_match = _RACE_NUMBER_RE.match(header.group("rest").strip())
    race_number = int(number_match.group(1)) if number_match else None
    if race_number is None:
        codes.append("MISSING_RACE_NUMBER")

    track_value, ambiguous = _resolve_track_code(header_track, filename_track)
    if ambiguous:
        codes.append("AMBIGUOUS_TRACK_IDENTITY")

    winner_match = _WINNER_RE.search(section_text)
    winner_detected = winner_match is not None
    winner_name = winner_match.group("name").strip() if winner_match else ""
    if not winner_detected:
        codes.append("WINNER_NOT_FOUND")

    official = winner_detected and _OFFICIAL_HINT_RE.search(section_text) is not None
    if not official:
        codes.append("OFFICIAL_STATUS_NOT_FOUND")

    deterministic_identity = (
        race_number is not None and bool(race_date) and not ambiguous and bool(track_value)
    )
    candidate_key = (
        f"{track_value}|{race_date}|R{race_number}" if deterministic_identity else ""
    )

    if candidate_key and winner_detected and official:
        confidence = "HIGH"
    elif candidate_key:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return RaceResultRef(
        source_file_path=source_file_path,
        source_file_sha256=source_file_sha256,
        track_code_or_name=track_value,
        race_date=race_date,
        race_number=race_number,
        candidate_key=candidate_key,
        official_status_detected=bool(official),
        winner_detected=winner_detected,
        winner_name=winner_name,
        extraction_confidence=confidence,
        uncertainty_code="|".join(dict.fromkeys(codes)),
        status=RESULT_RECON_LABEL,
    )


def slice_result_text(
    text: str,
    *,
    source_file_path: str,
    source_file_sha256: str,
    filename_track: str | None = None,
    filename_date: str | None = None,
) -> list[RaceResultRef]:
    """Slice already-extracted chart text into race-level reference rows."""
    lines = text.splitlines()
    bounds = _section_bounds(lines)
    if not bounds:
        return [
            RaceResultRef(
                source_file_path=source_file_path,
                source_file_sha256=source_file_sha256,
                track_code_or_name=(
                    resolve_track(track_code=filename_track)["track_code"] or (filename_track or "")
                    if filename_track else ""
                ),
                race_date=filename_date or "",
                race_number=None,
                candidate_key="",
                official_status_detected=False,
                winner_detected=False,
                winner_name="",
                extraction_confidence="LOW",
                uncertainty_code="NO_RACE_SECTIONS_FOUND",
                status=RESULT_RECON_LABEL,
            )
        ]
    refs: list[RaceResultRef] = []
    for start, end, header in bounds:
        section_text = "\n".join(lines[start:end])
        refs.append(
            _slice_section(
                section_text, header,
                source_file_path=source_file_path,
                source_file_sha256=source_file_sha256,
                filename_track=filename_track,
                filename_date=filename_date,
            )
        )
    return refs


def _filename_identity(name: str) -> tuple[str | None, str | None]:
    match = re.match(
        r"^eqb_(?P<track>[A-Za-z0-9]{2,6})_(?P<date>\d{4}-\d{2}-\d{2})_fullcard\.pdf$", name, re.I
    )
    if not match:
        return None, None
    try:
        date_iso = datetime.strptime(match.group("date"), "%Y-%m-%d").date().isoformat()
    except ValueError:
        date_iso = None
    return match.group("track"), date_iso


def slice_result_pdf(path: str | Path) -> list[RaceResultRef]:
    """Slice one retained full-card result PDF (read-only)."""
    pdf = Path(path)
    sha256 = _sha256_file(pdf)
    filename_track, filename_date = _filename_identity(pdf.name)
    text = _extract_pdf_text(pdf)
    if text is None:
        return [
            RaceResultRef(
                source_file_path=pdf.resolve().as_posix(),
                source_file_sha256=sha256,
                track_code_or_name=(
                    resolve_track(track_code=filename_track)["track_code"] or (filename_track or "")
                    if filename_track else ""
                ),
                race_date=filename_date or "",
                race_number=None,
                candidate_key="",
                official_status_detected=False,
                winner_detected=False,
                winner_name="",
                extraction_confidence="LOW",
                uncertainty_code="PDF_TEXT_EXTRACTION_FAILED",
                status=RESULT_RECON_LABEL,
            )
        ]
    return slice_result_text(
        text,
        source_file_path=pdf.resolve().as_posix(),
        source_file_sha256=sha256,
        filename_track=filename_track,
        filename_date=filename_date,
    )


# --------------------------------------------------------------------------- #
# Root scanning + artifacts                                                    #
# --------------------------------------------------------------------------- #
def _iter_fullcard_pdfs(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        if path.is_file() and _FULLCARD_NAME_RE.match(path.name):
            yield path


def scan_result_roots(roots: Iterable[str | Path]) -> ResultIndex:
    """Inventory ``eqb_*_fullcard.pdf`` files under ``roots`` and slice each."""
    resolved: list[Path] = []
    seen: set[str] = set()
    for raw in roots:
        path = Path(raw)
        if not path.exists() or not path.is_dir():
            raise ResultSliceRootError(f"scan root is not an existing local directory: {raw}")
        key = path.resolve().as_posix()
        if key not in seen:
            seen.add(key)
            resolved.append(path)
    if not resolved:
        raise ResultSliceRootError("at least one --root is required")

    rows: list[RaceResultRef] = []
    pdf_count = 0
    for root in resolved:
        for pdf in _iter_fullcard_pdfs(root):
            pdf_count += 1
            rows.extend(slice_result_pdf(pdf))
    rows.sort(key=lambda r: (r.source_file_path, r.race_number or 0, r.candidate_key))
    rows_tuple = tuple(rows)

    indexed_sections = [r for r in rows_tuple if r.uncertainty_code != "NO_RACE_SECTIONS_FOUND"
                        and r.uncertainty_code != "PDF_TEXT_EXTRACTION_FAILED"]
    deterministic = [r for r in rows_tuple if r.candidate_key]
    uncertain = [r for r in rows_tuple if r.uncertainty_code]

    summary = {
        "slicer_version": SLICER_VERSION,
        "recon_label": RESULT_RECON_LABEL,
        "roots": [r.resolve().as_posix() for r in resolved],
        "fullcard_pdf_count": pdf_count,
        "race_sections_indexed": len(indexed_sections),
        "deterministic_candidate_key_count": len(deterministic),
        "uncertain_section_count": len(uncertain),
        "uncertainty_code_counts": _counts(
            code for r in rows_tuple for code in (r.uncertainty_code.split("|") if r.uncertainty_code else [])
        ),
        "extraction_confidence_counts": _counts(r.extraction_confidence for r in rows_tuple),
        "distinct_candidate_key_count": len({r.candidate_key for r in rows_tuple if r.candidate_key}),
    }
    return ResultIndex(
        roots=tuple(r.resolve().as_posix() for r in resolved),
        slicer_version=SLICER_VERSION,
        rows=rows_tuple,
        summary=summary,
    )


def _counts(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


_INDEX_COLUMNS = [
    "candidate_key", "track_code_or_name", "race_date", "race_number",
    "official_status_detected", "winner_detected", "winner_name",
    "extraction_confidence", "uncertainty_code", "status",
    "source_file_sha256", "source_file_path",
]


def write_result_index_artifacts(index: ResultIndex, output_dir: str | Path) -> dict[str, str]:
    """Write the two deterministic result-index artifacts under ``output_dir``."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    race_index_csv = out / "equibase_result_race_index.csv"
    summary_json = out / "equibase_result_race_index_summary.json"

    with race_index_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_INDEX_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in index.rows:
            writer.writerow(
                {
                    "candidate_key": row.candidate_key,
                    "track_code_or_name": row.track_code_or_name,
                    "race_date": row.race_date,
                    "race_number": "" if row.race_number is None else row.race_number,
                    "official_status_detected": row.official_status_detected,
                    "winner_detected": row.winner_detected,
                    "winner_name": row.winner_name,
                    "extraction_confidence": row.extraction_confidence,
                    "uncertainty_code": row.uncertainty_code,
                    "status": row.status,
                    "source_file_sha256": row.source_file_sha256,
                    "source_file_path": row.source_file_path,
                }
            )

    summary_json.write_text(
        json.dumps(index.summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "race_index_csv": race_index_csv.resolve().as_posix(),
        "summary_json": summary_json.resolve().as_posix(),
    }
