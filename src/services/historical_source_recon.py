"""Read-only reconnaissance over local candidate historical-source artifacts.

This service **does not** ingest, parse into canonical tables, mutate SQLite,
train, score, calibrate, or change eligibility.  It answers one question: which
local files could be assembled into
:mod:`historical_pre_race_snapshot_manifest` contract manifests, and which exact
contract evidence is still missing.

Every record it emits is labelled :data:`RECON_LABEL` (``RECON_ONLY_NOT_ELIGIBLE``).
A filename date or a filesystem timestamp is metadata only -- it can never satisfy
the source-as-of proof requirement, so no such value is ever derived here.
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

RECON_LABEL = "RECON_ONLY_NOT_ELIGIBLE"
RECON_VERSION = "1.0"

# Extensions we are willing to reason about at all.  Anything else is UNSUPPORTED.
_SUPPORTED_EXTENSIONS = frozenset({".md", ".markdown", ".pdf", ".xlsx", ".xml"})

CLASS_PRE_RACE = "PRE_RACE_CARD_CANDIDATE"
CLASS_RESULT = "RESULT_CANDIDATE"
CLASS_UNKNOWN = "UNKNOWN_CANDIDATE"
CLASS_UNSUPPORTED = "UNSUPPORTED"

_HEADER_BYTES = 8192

# Filename tokens (case-insensitive) that mark a retained outcome/result artifact.
_RESULT_NAME_RE = re.compile(r"(result|chart|fullcard|payoff|chartbook|official)", re.I)
_RESULT_DIR_RE = re.compile(r"(^|[\\/])(historical_results|results|charts|outcomes)([\\/]|$)", re.I)

# DraftKings card filename, optionally prefixed by a 12-hex ingestion-run id and
# optionally carrying a rendered-overlay infix (e.g. ``_Speed_Power_Style``).
_DK_NAME_RE = re.compile(
    r"^(?:[0-9a-f]{6,16}_)?(?P<track>[A-Za-z]{2,5})_DK_Horse"
    r"(?P<overlay>(?:_[A-Za-z]+)*?)"
    r"_R(?P<race>\d{1,2})"
    r"(?:_(?P<mm>\d{1,2})-(?P<dd>\d{1,2})-(?P<yy>\d{2}|\d{4}))?"
    r"\.(?:md|markdown|pdf|xlsx)$",
    re.I,
)
_EQB_RESULT_NAME_RE = re.compile(
    r"^eqb_(?P<track>[A-Za-z]{2,5})_(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:_R(?P<race>\d{1,2}))?_(?:fullcard|chart|chartbook|results?)\.pdf$",
    re.I,
)
_SIMD_NAME_RE = re.compile(
    r"^SIMD(?P<date>\d{8})(?P<track>[A-Za-z]{2,5})_(?P<country>[A-Za-z]{2,3})\.xml$",
    re.I,
)
# Generic fallbacks used only when a provider-specific pattern does not match.
_GENERIC_DATE_ISO_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_GENERIC_DATE_COMPACT_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_GENERIC_RACE_RE = re.compile(r"(?:^|[_\-\s])R(?P<race>\d{1,2})(?:[_\-\s.]|$)", re.I)
_DK_POST_RE = re.compile(r"(?m)^\s*(\d{1,2}:\d{2})\s*$\s*^\s*([AP]M)\s*$")
_DK_RACE_HEADER_RE = re.compile(r"(?m)^\s*RACE\s+\d+\s*$", re.I)

# Manifest evidence status vocabulary.
_AVAILABLE = "AVAILABLE"
_FILENAME_DERIVED = "FILENAME_DERIVED_NOT_PROVEN"
_REQUIRES_MANIFEST = "REQUIRES_MANIFEST"
_REQUIRES_PARSE = "REQUIRES_PARSE"
_REQUIRES_RESULT = "REQUIRES_RESULT_ARTIFACT"

_BLOCKING_STATUSES = frozenset({_REQUIRES_MANIFEST, _REQUIRES_PARSE, _REQUIRES_RESULT})

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "historical_pre_race_snapshot_manifest.schema.json"
)


class ReconRootError(ValueError):
    """Raised when a supplied scan root is missing or is not a local directory."""


@dataclasses.dataclass(frozen=True)
class CandidateArtifact:
    path: str
    root: str
    size_bytes: int
    sha256: str
    extension: str
    header_signature: str
    classification: str
    provider_candidate: str | None
    track_code: str | None
    track_name: str | None
    track_token_raw: str | None
    race_date: str | None
    filename_date_status: str
    race_number: int | None
    scheduled_post_time_raw: str | None
    scheduled_post_time_status: str
    source_as_of_status: str
    candidate_key: str | None
    recon_label: str
    notes: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class CandidatePair:
    candidate_key: str
    track_code: str | None
    race_date: str | None
    race_number: int | None
    pre_race_paths: tuple[str, ...]
    result_paths: tuple[str, ...]
    pair_status: str
    recon_label: str


@dataclasses.dataclass(frozen=True)
class MissingEvidenceItem:
    candidate_key: str
    schema_field: str
    status: str
    detail: str


@dataclasses.dataclass(frozen=True)
class ReconReport:
    roots: tuple[str, ...]
    recon_version: str
    inventory: tuple[CandidateArtifact, ...]
    pairs: tuple[CandidatePair, ...]
    missing_evidence: tuple[MissingEvidenceItem, ...]
    summary: dict


# --------------------------------------------------------------------------- #
# Filesystem primitives (strictly read-only)                                   #
# --------------------------------------------------------------------------- #
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _header_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(_HEADER_BYTES)


def _header_signature(head: bytes, extension: str) -> str:
    if head.startswith(b"%PDF-"):
        return "PDF"
    if head[:4] == b"PK\x03\x04":
        return "ZIP_OOXML" if extension == ".xlsx" else "ZIP"
    text = head.decode("utf-8", errors="ignore")
    lowered = text.lower()
    if "entryracecard" in lowered or "simulcast.xsd" in lowered:
        return "SIMD_XML"
    if text.lstrip().startswith("<?xml") or "<race" in lowered:
        return "XML"
    if _looks_like_dk_markdown(text):
        return "DK_MARKDOWN"
    if text.strip():
        return "TEXT"
    return "UNKNOWN"


def _looks_like_dk_markdown(text: str) -> bool:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    first_resolves = resolve_track(track_name=lines[0])["track_code"] is not None
    return bool(first_resolves and _DK_RACE_HEADER_RE.search(text) and "PROGRAM" in text)


# --------------------------------------------------------------------------- #
# Filename / header metadata extraction                                        #
# --------------------------------------------------------------------------- #
def _iso_from_mdy(mm: str, dd: str, yy: str) -> str | None:
    for fmt in ("%m-%d-%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(f"{mm}-{dd}-{yy}", fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _iso_from_compact(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _iso_from_iso(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


@dataclasses.dataclass
class _NameMeta:
    provider: str | None = None
    track_token: str | None = None
    race_number: int | None = None
    race_date: str | None = None
    filename_date_status: str = "ABSENT"
    name_class_hint: str | None = None
    notes: list[str] = dataclasses.field(default_factory=list)


def _parse_filename(name: str, rel_dir: str) -> _NameMeta:
    meta = _NameMeta()

    dk = _DK_NAME_RE.match(name)
    if dk:
        meta.track_token = dk.group("track")
        meta.race_number = int(dk.group("race"))
        overlay = (dk.group("overlay") or "").strip("_")
        ext = Path(name).suffix.lower()
        if ext in (".md", ".markdown"):
            meta.provider = "draftkings_markdown"
        elif ext == ".pdf":
            meta.provider = "draftkings_pdf"
        elif ext == ".xlsx":
            meta.provider = "draftkings_workbook"
        if overlay:
            meta.notes.append(f"rendered overlay infix '{overlay}' is not a race card")
            meta.name_class_hint = CLASS_UNKNOWN
        else:
            meta.name_class_hint = CLASS_PRE_RACE
        if dk.group("mm"):
            iso = _iso_from_mdy(dk.group("mm"), dk.group("dd"), dk.group("yy"))
            if iso:
                meta.race_date, meta.filename_date_status = iso, "PARSED_FROM_FILENAME"
            else:
                meta.filename_date_status = "UNPARSEABLE"
        return meta

    eqb = _EQB_RESULT_NAME_RE.match(name)
    if eqb:
        meta.provider = "equibase_results_pdf"
        meta.track_token = eqb.group("track")
        meta.race_number = int(eqb.group("race")) if eqb.group("race") else None
        iso = _iso_from_iso(eqb.group("date"))
        if iso:
            meta.race_date, meta.filename_date_status = iso, "PARSED_FROM_FILENAME"
        else:
            meta.filename_date_status = "UNPARSEABLE"
        meta.name_class_hint = CLASS_RESULT
        return meta

    simd = _SIMD_NAME_RE.match(name)
    if simd:
        meta.provider = "equibase_simd_xml"
        meta.track_token = simd.group("track")
        iso = _iso_from_compact(simd.group("date"))
        if iso:
            meta.race_date, meta.filename_date_status = iso, "PARSED_FROM_FILENAME"
        else:
            meta.filename_date_status = "UNPARSEABLE"
        meta.name_class_hint = CLASS_PRE_RACE
        return meta

    # Generic fallback: still deterministic, but provider stays unknown.
    iso_match = _GENERIC_DATE_ISO_RE.search(name)
    compact_match = _GENERIC_DATE_COMPACT_RE.search(name)
    if iso_match and _iso_from_iso(iso_match.group(1)):
        meta.race_date = _iso_from_iso(iso_match.group(1))
        meta.filename_date_status = "PARSED_FROM_FILENAME"
    elif compact_match and _iso_from_compact(compact_match.group(1)):
        meta.race_date = _iso_from_compact(compact_match.group(1))
        meta.filename_date_status = "PARSED_FROM_FILENAME"
    race_match = _GENERIC_RACE_RE.search(name)
    if race_match:
        meta.race_number = int(race_match.group("race"))
    if _RESULT_NAME_RE.search(name) or _RESULT_DIR_RE.search(rel_dir):
        meta.name_class_hint = CLASS_RESULT
    return meta


def _post_time(head_text: str) -> tuple[str | None, str]:
    match = _DK_POST_RE.search(head_text)
    if match:
        return f"{match.group(1)} {match.group(2)}", "WALL_CLOCK_NO_DATE_NO_TZ"
    return None, "ABSENT"


# --------------------------------------------------------------------------- #
# Classification                                                               #
# --------------------------------------------------------------------------- #
def _classify(extension: str, signature: str, name_meta: _NameMeta, rel_dir: str) -> str:
    if extension not in _SUPPORTED_EXTENSIONS:
        return CLASS_UNSUPPORTED

    result_signalled = (
        name_meta.name_class_hint == CLASS_RESULT
        or _RESULT_DIR_RE.search(rel_dir) is not None
    )

    if signature == "SIMD_XML":
        return CLASS_PRE_RACE
    if signature == "DK_MARKDOWN":
        return CLASS_RESULT if result_signalled else CLASS_PRE_RACE
    if signature == "PDF":
        if name_meta.provider == "equibase_results_pdf" or result_signalled:
            return CLASS_RESULT
        if name_meta.provider == "draftkings_pdf":
            return CLASS_PRE_RACE
        return CLASS_UNKNOWN
    if signature == "ZIP_OOXML":
        if name_meta.provider == "draftkings_workbook":
            return CLASS_PRE_RACE
        return CLASS_UNKNOWN
    if signature in ("XML",):
        return CLASS_PRE_RACE if name_meta.provider == "equibase_simd_xml" else CLASS_UNKNOWN

    # Plain text / markdown that does not read as a DK card.
    if name_meta.name_class_hint == CLASS_PRE_RACE and name_meta.provider:
        return CLASS_PRE_RACE
    if result_signalled:
        return CLASS_RESULT
    return CLASS_UNKNOWN


def _candidate_key(track_code: str | None, race_date: str | None, race_number: int | None) -> str | None:
    if track_code and race_date and race_number is not None:
        return f"{track_code}|{race_date}|R{race_number}"
    return None


# --------------------------------------------------------------------------- #
# Inventory                                                                    #
# --------------------------------------------------------------------------- #
def _inventory_one(path: Path, root: Path) -> CandidateArtifact:
    extension = path.suffix.lower()
    rel = path.relative_to(root).as_posix()
    rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
    name = path.name

    name_meta = _parse_filename(name, rel)
    notes = list(name_meta.notes)

    if extension in _SUPPORTED_EXTENSIONS:
        head = _header_bytes(path)
        signature = _header_signature(head, extension)
        head_text = head.decode("utf-8", errors="ignore")
    else:
        signature = "UNSUPPORTED_EXTENSION"
        head_text = ""

    classification = _classify(extension, signature, name_meta, rel)

    track_code = track_name = None
    if name_meta.track_token:
        resolution = resolve_track(track_code=name_meta.track_token)
        if resolution["track_code"] is None:
            resolution = resolve_track(track_name=name_meta.track_token)
        track_code = resolution["track_code"] or name_meta.track_token.upper()
        track_name = resolution["track_name_canonical"]
        if resolution["track_code"] is None:
            notes.append(f"track token '{name_meta.track_token}' not in canonical registry")

    if track_code is None and signature == "DK_MARKDOWN":
        first_line = next((ln.strip() for ln in head_text.splitlines() if ln.strip()), "")
        resolution = resolve_track(track_name=first_line)
        if resolution["track_code"]:
            track_code = resolution["track_code"]
            track_name = resolution["track_name_canonical"]

    race_number = name_meta.race_number
    if race_number is None and signature == "DK_MARKDOWN":
        header_race = re.search(r"(?m)^\s*RACE\s+(\d+)\s*$", head_text, re.I)
        if header_race:
            race_number = int(header_race.group(1))

    post_raw = post_status = None
    if signature == "DK_MARKDOWN":
        post_raw, post_status = _post_time(head_text)
    else:
        post_status = "ABSENT"

    key = _candidate_key(track_code, name_meta.race_date, race_number)
    if classification in (CLASS_PRE_RACE, CLASS_RESULT) and key is None:
        notes.append("insufficient identity (track/date/race) for an exact candidate key")

    return CandidateArtifact(
        path=path.resolve().as_posix(),
        root=root.resolve().as_posix(),
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
        extension=extension,
        header_signature=signature,
        classification=classification,
        provider_candidate=name_meta.provider,
        track_code=track_code,
        track_name=track_name,
        track_token_raw=name_meta.track_token,
        race_date=name_meta.race_date,
        filename_date_status=name_meta.filename_date_status,
        race_number=race_number,
        scheduled_post_time_raw=post_raw,
        scheduled_post_time_status=post_status or "ABSENT",
        source_as_of_status="REQUIRES_MANIFEST_EVIDENCE",
        candidate_key=key,
        recon_label=RECON_LABEL,
        notes=tuple(notes),
    )


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        if path.is_file():
            yield path


# --------------------------------------------------------------------------- #
# Pairing + missing-evidence enumeration                                       #
# --------------------------------------------------------------------------- #
def _build_pairs(inventory: tuple[CandidateArtifact, ...]) -> list[CandidatePair]:
    groups: dict[str, dict[str, list[CandidateArtifact]]] = {}
    for item in inventory:
        if item.candidate_key is None:
            continue
        if item.classification not in (CLASS_PRE_RACE, CLASS_RESULT):
            continue
        bucket = groups.setdefault(item.candidate_key, {"pre": [], "res": []})
        bucket["pre" if item.classification == CLASS_PRE_RACE else "res"].append(item)

    pairs: list[CandidatePair] = []
    for key in sorted(groups):
        bucket = groups[key]
        pre = sorted(bucket["pre"], key=lambda c: c.path)
        res = sorted(bucket["res"], key=lambda c: c.path)
        if pre and res:
            status = "DETERMINISTIC_PAIR"
        elif pre:
            status = "CARD_ONLY"
        else:
            status = "RESULT_ONLY"
        sample = (pre or res)[0]
        pairs.append(
            CandidatePair(
                candidate_key=key,
                track_code=sample.track_code,
                race_date=sample.race_date,
                race_number=sample.race_number,
                pre_race_paths=tuple(c.path for c in pre),
                result_paths=tuple(c.path for c in res),
                pair_status=status,
                recon_label=RECON_LABEL,
            )
        )
    return pairs


def manifest_required_fields() -> list[str]:
    """Return the manifest schema's required field names (read-only)."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return list(schema.get("required", []))


def _missing_evidence_for_pair(pair: CandidatePair) -> list[MissingEvidenceItem]:
    has_track = pair.track_code is not None
    has_date = pair.race_date is not None
    has_race = pair.race_number is not None
    has_result = bool(pair.result_paths)

    field_status: dict[str, tuple[str, str]] = {
        "artifact_path_or_uri": (_AVAILABLE, "local candidate card file is retained"),
        "sha256": (_AVAILABLE, "computed read-only during reconnaissance"),
        "source_provider": (_AVAILABLE, "provider candidate inferred from filename/header"),
        "parser_version": (_REQUIRES_PARSE, "no deterministic parser has been run against this artifact"),
        "source_as_of_timestamp": (_REQUIRES_MANIFEST, "provider/HTTP/operator-attested capture time with timezone"),
        "source_as_of_tier": (_REQUIRES_MANIFEST, "PROVEN or OPERATOR_ATTESTED must be declared, not inferred"),
        "source_as_of_provenance": (_REQUIRES_MANIFEST, "capture-time provenance must be declared"),
        "target_track_code": (
            (_AVAILABLE, "resolved from filename token") if has_track
            else (_REQUIRES_MANIFEST, "track identity not resolvable from filename")
        ),
        "target_race_date": (
            (_FILENAME_DERIVED, "date parsed from filename only; not pre-race proof") if has_date
            else (_REQUIRES_MANIFEST, "race date not present in filename")
        ),
        "target_race_number": (
            (_AVAILABLE, "parsed from filename/header") if has_race
            else (_REQUIRES_MANIFEST, "race number absent (full-card artifact)")
        ),
        "target_scheduled_post_timestamp": (_REQUIRES_MANIFEST, "timezone-qualified post time; wall-clock string is insufficient"),
        "target_surface": (_REQUIRES_PARSE, "surface must be confirmed by a deterministic parse"),
        "target_distance_furlongs": (_REQUIRES_PARSE, "distance must be confirmed by a deterministic parse"),
        "expected_active_starter_count": (_REQUIRES_PARSE, "non-scratched field must be enumerated by a parse"),
        "observed_active_starter_count": (_REQUIRES_PARSE, "non-scratched field must be enumerated by a parse"),
        "identity_reconciliation_status": (_REQUIRES_PARSE, "every active starter must reconcile to a stable identity"),
        "field_completeness_status": (_REQUIRES_PARSE, "complete starter fields must be verified by a parse"),
        "feature_vector_status": (_REQUIRES_PARSE, "complete pre-race feature vectors must be verified by a parse"),
        "raw_artifact_retained": (_AVAILABLE, "raw bytes present locally and hashed read-only"),
        "pre_race_fields_only": (_REQUIRES_PARSE, "post-race-content absence must be verified by a parse"),
        "outcome_reference": (
            (_AVAILABLE, "a separately retained result candidate is present for this key") if has_result
            else (_REQUIRES_RESULT, "no separately retained result/outcome artifact found for this key")
        ),
        "outcome_provenance_status": (_REQUIRES_MANIFEST, "result provenance (PROVEN/OPERATOR_ATTESTED) must be declared"),
    }

    ordered = manifest_required_fields()
    for extra in field_status:
        if extra not in ordered:
            ordered.append(extra)

    return [
        MissingEvidenceItem(
            candidate_key=pair.candidate_key,
            schema_field=field,
            status=field_status[field][0],
            detail=field_status[field][1],
        )
        for field in ordered
        if field in field_status
    ]


def _pair_is_contract_complete(items: list[MissingEvidenceItem]) -> bool:
    return not any(item.status in _BLOCKING_STATUSES for item in items)


# --------------------------------------------------------------------------- #
# Public entrypoints                                                           #
# --------------------------------------------------------------------------- #
def scan_roots(roots: Iterable[str | Path]) -> ReconReport:
    """Inventory candidate files under ``roots`` without modifying anything."""
    resolved: list[Path] = []
    seen: set[str] = set()
    for raw in roots:
        path = Path(raw)
        if not path.exists() or not path.is_dir():
            raise ReconRootError(f"scan root is not an existing local directory: {raw}")
        key = path.resolve().as_posix()
        if key not in seen:
            seen.add(key)
            resolved.append(path)
    if not resolved:
        raise ReconRootError("at least one --root is required")

    inventory: list[CandidateArtifact] = []
    for root in resolved:
        for file_path in _iter_files(root):
            inventory.append(_inventory_one(file_path, root))
    inventory.sort(key=lambda c: (c.candidate_key or "~", c.classification, c.path))
    inventory_tuple = tuple(inventory)

    pairs = _build_pairs(inventory_tuple)

    missing: list[MissingEvidenceItem] = []
    contract_complete = 0
    for pair in pairs:
        items = _missing_evidence_for_pair(pair)
        missing.extend(items)
        if _pair_is_contract_complete(items):
            contract_complete += 1

    cards = [c for c in inventory_tuple if c.classification == CLASS_PRE_RACE]
    results = [c for c in inventory_tuple if c.classification == CLASS_RESULT]
    summary = {
        "recon_version": RECON_VERSION,
        "recon_label": RECON_LABEL,
        "roots": [r.resolve().as_posix() for r in resolved],
        "file_count": len(inventory_tuple),
        "candidate_card_count": len(cards),
        "candidate_result_count": len(results),
        "unknown_candidate_count": sum(1 for c in inventory_tuple if c.classification == CLASS_UNKNOWN),
        "unsupported_count": sum(1 for c in inventory_tuple if c.classification == CLASS_UNSUPPORTED),
        "candidate_key_count": len({c.candidate_key for c in inventory_tuple if c.candidate_key}),
        "deterministic_pair_count": sum(1 for p in pairs if p.pair_status == "DETERMINISTIC_PAIR"),
        "card_only_key_count": sum(1 for p in pairs if p.pair_status == "CARD_ONLY"),
        "result_only_key_count": sum(1 for p in pairs if p.pair_status == "RESULT_ONLY"),
        "contract_complete_candidate_count": contract_complete,
        "classification_counts": {
            cls: sum(1 for c in inventory_tuple if c.classification == cls)
            for cls in (CLASS_PRE_RACE, CLASS_RESULT, CLASS_UNKNOWN, CLASS_UNSUPPORTED)
        },
        "provider_candidate_counts": _counts(c.provider_candidate for c in inventory_tuple),
    }

    return ReconReport(
        roots=tuple(r.resolve().as_posix() for r in resolved),
        recon_version=RECON_VERSION,
        inventory=inventory_tuple,
        pairs=tuple(pairs),
        missing_evidence=tuple(missing),
        summary=summary,
    )


def _counts(values: Iterable[str | None]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        label = value or "unknown"
        out[label] = out.get(label, 0) + 1
    return dict(sorted(out.items()))


_INVENTORY_COLUMNS = [
    "candidate_key", "classification", "provider_candidate", "track_code", "track_name",
    "track_token_raw", "race_date", "filename_date_status", "race_number",
    "scheduled_post_time_raw", "scheduled_post_time_status", "source_as_of_status",
    "header_signature", "extension", "size_bytes", "sha256", "recon_label",
    "root", "path", "notes",
]
_PAIRS_COLUMNS = [
    "candidate_key", "pair_status", "track_code", "race_date", "race_number",
    "pre_race_card_count", "result_count", "pre_race_paths", "result_paths", "recon_label",
]
_MISSING_COLUMNS = ["candidate_key", "schema_field", "status", "detail"]


def _write_csv(path: Path, columns: list[str], rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_recon_artifacts(report: ReconReport, output_dir: str | Path) -> dict[str, str]:
    """Write the four deterministic reconnaissance artifacts under ``output_dir``."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    inventory_csv = out / "historical_source_recon_inventory.csv"
    pairs_csv = out / "historical_source_recon_pairs.csv"
    missing_csv = out / "historical_source_recon_missing_evidence.csv"
    summary_json = out / "historical_source_recon_summary.json"

    _write_csv(
        inventory_csv,
        _INVENTORY_COLUMNS,
        (
            {
                "candidate_key": c.candidate_key or "",
                "classification": c.classification,
                "provider_candidate": c.provider_candidate or "",
                "track_code": c.track_code or "",
                "track_name": c.track_name or "",
                "track_token_raw": c.track_token_raw or "",
                "race_date": c.race_date or "",
                "filename_date_status": c.filename_date_status,
                "race_number": "" if c.race_number is None else c.race_number,
                "scheduled_post_time_raw": c.scheduled_post_time_raw or "",
                "scheduled_post_time_status": c.scheduled_post_time_status,
                "source_as_of_status": c.source_as_of_status,
                "header_signature": c.header_signature,
                "extension": c.extension,
                "size_bytes": c.size_bytes,
                "sha256": c.sha256,
                "recon_label": c.recon_label,
                "root": c.root,
                "path": c.path,
                "notes": " | ".join(c.notes),
            }
            for c in report.inventory
        ),
    )

    _write_csv(
        pairs_csv,
        _PAIRS_COLUMNS,
        (
            {
                "candidate_key": p.candidate_key,
                "pair_status": p.pair_status,
                "track_code": p.track_code or "",
                "race_date": p.race_date or "",
                "race_number": "" if p.race_number is None else p.race_number,
                "pre_race_card_count": len(p.pre_race_paths),
                "result_count": len(p.result_paths),
                "pre_race_paths": " | ".join(p.pre_race_paths),
                "result_paths": " | ".join(p.result_paths),
                "recon_label": p.recon_label,
            }
            for p in report.pairs
        ),
    )

    _write_csv(
        missing_csv,
        _MISSING_COLUMNS,
        (
            {
                "candidate_key": m.candidate_key,
                "schema_field": m.schema_field,
                "status": m.status,
                "detail": m.detail,
            }
            for m in report.missing_evidence
        ),
    )

    summary_json.write_text(
        json.dumps(report.summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return {
        "inventory_csv": inventory_csv.resolve().as_posix(),
        "pairs_csv": pairs_csv.resolve().as_posix(),
        "missing_evidence_csv": missing_csv.resolve().as_posix(),
        "summary_json": summary_json.resolve().as_posix(),
    }
