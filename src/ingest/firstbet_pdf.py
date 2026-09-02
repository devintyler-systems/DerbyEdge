"""Source-specific adapter for text-based 1/ST BET race PDFs.

The adapter owns extraction, parsing, validation, mode resolution, and audit
persistence.  It does not build model features and it never guesses a horse
join with fuzzy matching.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from src.derbyedge.tracks import resolve_track
from src.ingest.run_state import DataQuality, RunMode, resolve_run_mode
from src.utils.horse_norm import horse_key


SCHEMA_VERSION = "1.0"
REQUIRED_RACE_FIELDS = {
    "track_name",
    "race_number",
    "race_date",
    "class_family",
    "distance_furlongs",
    "surface",
    "going",
    "field_size_declared",
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_TRACK_DATE_RE = re.compile(
    rf"^(.+?)\s+(\d{{1,2}})\s+({_MONTH_PATTERN})\s+(\d{{4}})$",
    re.IGNORECASE,
)
_ENTRY_HEADER_RE = re.compile(r"^([A-Z][A-Z0-9'.()\- ]*[A-Z0-9)])\s+-\s*$")
_FINISH_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_FIELD_RE = re.compile(r"\b(\d{1,2})\s+Horses\b", re.IGNORECASE)
_ODDS_RE = re.compile(r"\((\d+(?:/\d+)?)\)")
_PURSE_RE = re.compile(r"\$([\d,]+)")
_DIST_RE = re.compile(r"\b((?:\d+\s+\d+/\d+|\d+(?:\.\d+)?)\s*[FM])\b", re.I)
_SURFACE_GOING_RE = re.compile(
    r"\b(Dirt|Inner\s*Turf|Turf|Synthetic|All[- ]?Weather)\s*/\s*([A-Za-z-]+)", re.I
)
_CLASS_RE = re.compile(
    r"\b(Maiden\s+Special\s+Wt\.?|Maiden\s+Special\s+Weight|"
    r"Maiden\s+Claiming|Allowance\s+Optional\s+Claiming|Optional\s+Claiming|"
    r"Starter\s+Allowance|Allowance|Non-Graded\s+Stakes|Graded|Stakes?|"
    r"Handicap|Claiming)\b",
    re.I,
)
_PAGE_NOISE_RE = re.compile(
    r"^(?:https?://|\d+/\d+$|\d{1,2}/\d{1,2}/\d{2},\s+\d|"
    r"1/ST BET|SARATOGA TB R\s+\d+$)", re.I
)
_RUN_STYLE_TERMS = re.compile(
    r"set pace|\bpace\b|\bled\b|quick lead|took over|dueled|vied|hooked|"
    r"pressed|challeng|rallied|ran on|up final|gained|belatedly",
    re.I,
)
_TRIP_TERMS = re.compile(
    r"bump|brushed|stumbled|broke out|veered|slow|\b[4-9]w\b|wide|outside|"
    r"dueled|vied|hooked|pressed|challeng|weakened|tired|yielded|faltered|empty",
    re.I,
)
_OFF_TRACK = {"good", "muddy", "sloppy", "heavy", "yielding", "soft", "wet-fast"}


class FirstBetParseError(ValueError):
    """Raised when extracted text is not a supported 1/ST race-detail page."""


def extract_text(pdf_bytes: bytes) -> str:
    """Extract text while retaining the source's line boundaries."""
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise FirstBetParseError("pdfplumber is required to parse 1/ST PDFs.") from exc

    parts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text(x_tolerance=2, y_tolerance=2)
                if page_text:
                    parts.append(page_text)
    except Exception as exc:
        raise FirstBetParseError(f"PDF read error: {exc}") from exc
    text = "\n".join(parts)
    if not text.strip():
        raise FirstBetParseError("No text found in PDF; image-only PDFs are unsupported.")
    return text


def parse_firstbet_text(
    text: str,
    *,
    filename: str,
    sha256: str,
    uploaded_at_utc: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse extracted 1/ST text into the normalized payload and audit."""
    if not re.search(r"1/ST\s+BET|(?:legacy\.)?1stbet\.com", text[:1500], re.I):
        raise FirstBetParseError("Source is not recognized as a 1/ST BET PDF.")

    lines = [line.strip() for line in text.splitlines()]
    race = _parse_race_header(lines)
    entries, entry_diagnostics = _parse_entries(lines)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "provider": "1stbet",
            "source_type": "pdf",
            "filename": Path(filename).name,
            "sha256": sha256,
            "uploaded_at_utc": uploaded_at_utc,
        },
        "race": race,
        "entries": entries,
    }
    audit = build_feature_audit(payload, entry_diagnostics, extracted_text_chars=len(text))
    return payload, audit


def parse_firstbet_pdf(
    pdf_bytes: bytes,
    *,
    filename: str,
    uploaded_at_utc: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    uploaded = uploaded_at_utc or _utc_now()
    return parse_firstbet_text(
        extract_text(pdf_bytes),
        filename=filename,
        sha256=sha256,
        uploaded_at_utc=uploaded,
    )


def ingest_firstbet_pdf(
    pdf_bytes: bytes,
    *,
    filename: str,
    runs_root: Path | str = Path("data/runs"),
    uploaded_at_utc: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Parse and persist both artifacts for every 1/ST upload attempt."""
    uploaded = uploaded_at_utc or _utc_now()
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    run_id = run_id or _new_run_id(uploaded, sha256)
    try:
        payload, audit = parse_firstbet_pdf(
            pdf_bytes, filename=filename, uploaded_at_utc=uploaded
        )
    except Exception as exc:
        payload = _empty_payload(filename, sha256, uploaded)
        audit = {
            "run_id": run_id,
            "run_mode": RunMode.BLOCKED.value,
            "ingest_run_mode": RunMode.BLOCKED.value,
            "source_provider": "1stbet",
            "field_size_declared": None,
            "field_size_declared_raw": None,
            "entries_parsed": 0,
            "active_entries": 0,
            "scratches": 0,
            "entries_with_pp_history": 0,
            "total_pp_starts_parsed": 0,
            "starter_match_rate": 0.0,
            "feature_coverage": _empty_coverage(),
            "blocking_errors": [str(exc)],
            "warnings": [],
            "diagnostics": {"source_sha256": sha256},
        }
        paths = persist_run_artifacts(run_id, payload, audit, runs_root=runs_root)
        return {
            "ok": False,
            "error": str(exc),
            "run_id": run_id,
            "payload": payload,
            "feature_audit": audit,
            "paths": paths,
        }

    audit["run_id"] = run_id
    paths = persist_run_artifacts(run_id, payload, audit, runs_root=runs_root)
    return {
        "ok": audit["run_mode"] != RunMode.BLOCKED.value,
        "error": "; ".join(audit["blocking_errors"]) or None,
        "run_id": run_id,
        "payload": payload,
        "feature_audit": audit,
        "paths": paths,
    }


def persist_run_artifacts(
    run_id: str,
    payload: Mapping[str, Any],
    audit: Mapping[str, Any],
    *,
    runs_root: Path | str = Path("data/runs"),
) -> dict[str, str]:
    run_dir = Path(runs_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    parsed_path = run_dir / "parsed_pp.json"
    audit_path = run_dir / "feature_audit.json"
    parsed_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    return {"parsed_pp": str(parsed_path), "feature_audit": str(audit_path)}


def bind_run_to_card(
    run_id: str,
    card_id: int,
    *,
    runs_root: Path | str = Path("data/runs"),
) -> dict[str, Any]:
    """Attach an immutable upload run to its DB race identity.

    The binding is deliberately separate from ``feature_audit.json`` so the
    ingest-time audit remains byte-for-byte immutable after persistence.
    """
    audit_path = Path(runs_root) / run_id / "feature_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    binding_path = audit_path.with_name("card_binding.json")
    binding = (
        json.loads(binding_path.read_text(encoding="utf-8"))
        if binding_path.exists() else {}
    )
    existing = binding.get("card_id")
    if existing is not None and int(existing) != int(card_id):
        raise ValueError(f"Run {run_id} is already bound to card_id={existing}.")
    binding = {"run_id": run_id, "card_id": int(card_id)}
    binding_path.write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")
    return {**audit, "card_id": int(card_id), "_binding_path": str(binding_path)}


def load_latest_card_audit(
    card_id: int,
    *,
    runs_root: Path | str = Path("data/runs"),
) -> dict[str, Any] | None:
    """Return the newest persisted upload audit explicitly bound to a card."""
    root = Path(runs_root)
    if not root.exists():
        return None
    matches: list[dict[str, Any]] = []
    for binding_path in root.glob("*/card_binding.json"):
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            if int(binding.get("card_id")) != int(card_id):
                continue
            path = binding_path.with_name("feature_audit.json")
            audit = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        audit["card_id"] = int(card_id)
        audit["_audit_path"] = str(path)
        audit["_binding_path"] = str(binding_path)
        matches.append(audit)
    # Read legacy bindings without rewriting their immutable audit. This keeps
    # existing PR fixtures/runs discoverable while all new bindings are split.
    for path in root.glob("*/feature_audit.json"):
        try:
            audit = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if audit.get("card_id") is not None and int(audit["card_id"]) == int(card_id):
            audit["_audit_path"] = str(path)
            matches.append(audit)
    if not matches:
        return None
    return max(matches, key=lambda item: str(item.get("run_id") or ""))


def to_legacy_race_result(
    payload: Mapping[str, Any], audit: Mapping[str, Any]
) -> dict[str, Any]:
    """Translate normalized data for the existing race-card DB writer."""
    race = payload.get("race") or {}
    runners = []
    for entry in payload.get("entries") or []:
        legacy_starts = []
        for pp in entry.get("past_performances") or []:
            pp_track = resolve_track(track_name=pp.get("track_name"), track_code=None)
            surface_code = {
                "dirt": "D", "turf": "T", "synthetic": "S", "all_weather": "AW"
            }.get(pp.get("surface"))
            legacy_starts.append({
                "race_date": pp.get("start_date"),
                "track_code": pp_track.get("track_code"),
                "finish_position": pp.get("finish_position"),
                "field_size": pp.get("field_size"),
                "odds_str": pp.get("odds_fractional"),
                "distance_text": (
                    f"{pp['distance_furlongs']:g}F"
                    if pp.get("distance_furlongs") is not None else None
                ),
                "surface": surface_code,
                "race_class": pp.get("class_family"),
                "purse": pp.get("purse_usd"),
                "notes": pp.get("trip_comment"),
            })
        runners.append({
            "horse_name": _display_name(entry.get("horse_raw") or ""),
            "horse_key": entry.get("horse_key"),
            "post_position": entry.get("post"),
            "program_number": str(entry.get("post")),
            "trainer": entry.get("trainer"),
            "jockey": entry.get("jockey"),
            "ml": entry.get("morning_line_text"),
            "morning_line": entry.get("morning_line_text"),
            "morning_line_decimal": entry.get("morning_line_decimal"),
            "morning_line_decimal_includes_stake": True,
            "past_performances": entry.get("past_performances") or [],
            "last_5": legacy_starts,
            "is_scratched": bool(entry.get("is_scratched")),
            "scratch_source": entry.get("scratch_source"),
        })
    return {
        "ok": audit.get("run_mode") != RunMode.BLOCKED.value,
        "error": "; ".join(audit.get("blocking_errors") or []) or None,
        "warnings": list(audit.get("warnings") or []),
        "track_code": race.get("track_code"),
        "track_name": race.get("track_name"),
        "race_date": race.get("race_date"),
        "race_number": race.get("race_number"),
        "post_time_local": race.get("post_time_local"),
        "distance_text": (
            f"{race['distance_furlongs']:g} Furlongs"
            if race.get("distance_furlongs") is not None else None
        ),
        "surface": race.get("surface"),
        "going": race.get("going"),
        "race_type": race.get("class_family"),
        "purse_usd": race.get("purse_usd"),
        "field_size": race.get("field_size_declared"),
        "runners": runners,
        "runners_count": len(runners),
        "is_1stbet": True,
        "parse_debug": audit.get("diagnostics") or {},
        "run_mode": audit.get("run_mode"),
    }


def build_feature_audit(
    payload: Mapping[str, Any],
    entry_diagnostics: Mapping[str, Any] | None = None,
    *,
    extracted_text_chars: int | None = None,
) -> dict[str, Any]:
    race = payload.get("race") or {}
    entries = list(payload.get("entries") or [])
    active_entries = [entry for entry in entries if not entry.get("is_scratched")]
    scratches = len(entries) - len(active_entries)
    entries_with_pp = sum(bool(e.get("past_performances")) for e in active_entries)
    total_pp = sum(len(e.get("past_performances") or []) for e in active_entries)
    metadata_complete = all(race.get(field) not in (None, "") for field in REQUIRED_RACE_FIELDS)
    blocking: list[str] = []
    invalid_entries = [
        str(e.get("post") or "?")
        for e in active_entries
        if not all(e.get(k) not in (None, "") for k in ("post", "horse_raw", "trainer", "jockey"))
        or e.get("morning_line_decimal") is None
    ]
    if invalid_entries:
        blocking.append("Entries missing horse, post, trainer, jockey, or morning line: " + ", ".join(invalid_entries))

    parsed = len(active_entries)
    match_rate = entries_with_pp / parsed if parsed else 0.0
    quality = DataQuality(
        entries_parsed=parsed,
        field_size_declared=race.get("field_size_declared"),
        entries_with_pp_history=entries_with_pp,
        starter_match_rate=match_rate,
        race_metadata_complete=metadata_complete,
        has_morning_lines=bool(active_entries) and all(
            e.get("morning_line_decimal") for e in active_entries
        ),
        has_live_odds=False,
        required_model_features_complete=False,
        blocking_errors=blocking,
        entries_scratched=scratches,
    )
    mode, reasons = resolve_run_mode(quality)
    coverage = _coverage(active_entries)
    warnings = [
        "No speed figures supplied by source.",
        "No fractional pace figures supplied by source.",
        "Morning line is not live market odds.",
    ]
    if coverage["off_track_evidence"] < 1.0:
        warnings.append("Muddy-track evidence is incomplete for some starters.")
    if mode in (RunMode.PP_PARSED_FEATURES_PENDING, RunMode.MODEL_READY_LIMITED):
        warnings.extend(reasons)

    diagnostics = dict(entry_diagnostics or {})
    diagnostics.update({
        "parsed_post_positions": [e.get("post") for e in entries],
        "active_post_positions": [e.get("post") for e in active_entries],
        "missing_post_positions": _missing_posts(race.get("field_size_declared"), entries),
        "horse_join_strategy": "strict_canonical_key",
    })
    if extracted_text_chars is not None:
        diagnostics["extracted_text_chars"] = extracted_text_chars

    return {
        "run_id": None,
        "run_mode": mode.value,
        "ingest_run_mode": mode.value,
        "source_provider": "1stbet",
        "field_size_declared": race.get("field_size_declared"),
        "field_size_declared_raw": race.get("field_size_declared"),
        "entries_parsed": len(entries),
        "active_entries": parsed,
        "scratches": scratches,
        "entries_with_pp_history": entries_with_pp,
        "total_pp_starts_parsed": total_pp,
        "starter_match_rate": round(match_rate, 4),
        "race_metadata_complete": metadata_complete,
        "has_morning_lines": quality.has_morning_lines,
        "feature_coverage": coverage,
        "blocking_errors": reasons if mode == RunMode.BLOCKED else [],
        "warnings": list(dict.fromkeys(warnings)),
        "diagnostics": diagnostics,
    }


def _parse_race_header(lines: list[str]) -> dict[str, Any]:
    race_date = None
    uploaded_header = next((line for line in lines[:5] if "1/ST BET" in line.upper()), "")
    date_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", uploaded_header)
    if date_match:
        year = int(date_match.group(3))
        year = year + 2000 if year < 100 else year
        race_date = f"{year:04d}-{int(date_match.group(1)):02d}-{int(date_match.group(2)):02d}"

    track_name = None
    race_number = None
    identity_idx = None
    for idx, line in enumerate(lines[:10]):
        match = re.match(r"^(.+?)\s+(?:TB\s+)?R\s+(\d{1,2})$", line, re.I)
        if match:
            track_name = re.sub(r"\s+TB$", "", match.group(1), flags=re.I).strip().title()
            race_number = int(match.group(2))
            identity_idx = idx
            break

    # The 1/ST PDF text extractor can emit the track and race as two lines:
    # ``SARATOGA TB`` / ``R 9``. Keep the compact form above as the primary
    # parser so existing cards retain their established behavior.
    if identity_idx is None:
        for idx in range(min(len(lines) - 1, 10)):
            track_match = re.match(r"^(.+?)(?:\s+TB)?$", lines[idx], re.I)
            race_match = re.match(r"^R\s+(\d{1,2})$", lines[idx + 1], re.I)
            if track_match and race_match:
                candidate = track_match.group(1).strip()
                if candidate and not re.search(r"1/ST|BET|\d{1,2}/\d{1,2}/\d", candidate, re.I):
                    track_name = candidate.title()
                    race_number = int(race_match.group(1))
                    identity_idx = idx + 1
                    break

    detail_lines = (
        lines[identity_idx + 1:min(identity_idx + 9, len(lines))]
        if identity_idx is not None else []
    )
    detail = detail_lines[0] if detail_lines else ""
    match = re.match(
        r"^(\d{1,2}:\d{2}\s*[AP]M)\s+(\d+)\s+Horses\s+([A-Z]{2,5})\s+"
        r"\$([\d,]+)\s+(.+?)\s+(Inner\s*Turf|Innerturf|Dirt|Turf|Synthetic|All[- ]?Weather)\s*/\s*([A-Za-z-]+)$",
        detail,
        re.I,
    )
    post_time = field_size = class_family = purse = distance = surface = going = None
    if match:
        post_time = _to_24_hour(match.group(1))
        field_size = int(match.group(2))
        class_family = match.group(3).upper()
        purse = int(match.group(4).replace(",", ""))
        distance = _distance_furlongs(match.group(5))
        surface = _surface(match.group(6))
        going = match.group(7).lower()
    elif detail_lines:
        # Stacked layout: time / field / class / purse / distance / surface / going.
        # Parse labels rather than relying on an exact line count so page chrome
        # between source fields cannot silently change the meaning of a card.
        detail_text = " ".join(detail_lines)
        time_match = re.search(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", detail_text, re.I)
        field_match = _FIELD_RE.search(detail_text)
        class_match = re.search(r"\bHorses\s+([A-Z]{2,5})\s+\$", detail_text, re.I)
        purse_match = _PURSE_RE.search(detail_text)
        distance_match = _DIST_RE.search(detail_text)
        surface_match = re.search(
            r"\b(Inner\s*Turf|Innerturf|Dirt|Turf|Synthetic|All[- ]?Weather)\b",
            detail_text,
            re.I,
        )
        if time_match:
            post_time = _to_24_hour(time_match.group(1))
        if field_match:
            field_size = int(field_match.group(1))
        if class_match:
            class_family = class_match.group(1).upper()
        if purse_match:
            purse = int(purse_match.group(1).replace(",", ""))
        if distance_match:
            distance = _distance_furlongs(distance_match.group(1))
        if surface_match:
            surface = _surface(surface_match.group(1))
            after_surface = detail_text[surface_match.end():]
            going_match = re.search(r"\b([A-Za-z-]+)\b", after_surface)
            if going_match:
                going = going_match.group(1).lower()

    resolved = resolve_track(track_name=track_name, track_code=None)
    return {
        "track_code": resolved.get("track_code"),
        "track_name": resolved.get("track_name_canonical") or track_name,
        "race_number": race_number,
        "race_date": race_date,
        "post_time_local": post_time,
        "class_family": class_family,
        "purse_usd": purse,
        "distance_furlongs": distance,
        "surface": surface,
        "going": going,
        "field_size_declared": field_size,
    }


def _parse_entries(lines: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run every supported source layout and retain the strongest parse.

    A 1/ST export can mix page furniture with one of several column orders.
    Choosing the first non-empty result made the parser brittle: an incidental
    match can hide the strategy that actually recovered the full field.
    """
    strategies = (
        ("compact", _parse_compact_entries),
        ("stacked", _parse_stacked_entries),
        ("name_aux_post_jockey_pp_trainer", _parse_name_aux_post_jockey_pp_trainer_entries),
    )
    candidates: list[tuple[tuple[int, int, int], str, list[dict[str, Any]], dict[str, Any]]] = []
    for strategy_name, parser in strategies:
        entries, diagnostics = parser(lines)
        unique_posts = len({entry.get("post") for entry in entries if entry.get("post") is not None})
        complete_profiles = sum(
            all(entry.get(field) not in (None, "") for field in (
                "horse_raw", "post", "jockey", "trainer", "morning_line_decimal"
            ))
            for entry in entries
        )
        candidates.append(((len(entries), unique_posts, complete_profiles), strategy_name, entries, diagnostics))

    score, strategy_name, entries, diagnostics = max(candidates, key=lambda candidate: candidate[0])
    diagnostics = dict(diagnostics)
    diagnostics["entry_parser_strategy"] = strategy_name
    diagnostics["entry_parser_candidates"] = {
        name: {
            "parsed_entries": candidate_score[0],
            "unique_posts": candidate_score[1],
            "complete_connection_ml_entries": candidate_score[2],
        }
        for candidate_score, name, _, _ in candidates
    }
    diagnostics["entry_parser_selection_score"] = {
        "parsed_entries": score[0],
        "unique_posts": score[1],
        "complete_connection_ml_entries": score[2],
    }
    return entries, diagnostics


def _parse_compact_entries(lines: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    anchors: list[tuple[int, str, int, int]] = []
    for idx, line in enumerate(lines):
        header = _ENTRY_HEADER_RE.match(line)
        if not header:
            continue
        post = None
        pp_idx = None
        for look in range(idx + 1, min(idx + 10, len(lines))):
            pp_match = re.fullmatch(r"PP\s*(\d{1,2})", lines[look], re.I)
            if pp_match:
                post = int(pp_match.group(1))
                pp_idx = look
                break
        if post is not None and pp_idx is not None:
            anchors.append((idx, header.group(1).strip(), post, pp_idx))

    entries: list[dict[str, Any]] = []
    duplicate_keys: list[str] = []
    seen_keys: set[str] = set()
    for pos, (start_idx, raw_name, post, pp_idx) in enumerate(anchors):
        end_idx = anchors[pos + 1][0] if pos + 1 < len(anchors) else len(lines)
        profile = lines[start_idx:pp_idx + 1]
        jockey_line = next((line for line in profile if re.match(r"^J:", line, re.I)), "")
        trainer_line = next((line for line in profile if re.match(r"^T:", line, re.I)), "")
        jockey_match = re.match(r"^J:\s*(.+?)\s+ML\s+([\d/-]+)$", jockey_line, re.I)
        if not jockey_match:
            jockey_match = re.match(r"^J:\s*(.+)$", jockey_line, re.I)
        trainer_match = re.match(r"^T:\s*(.+)$", trainer_line, re.I)
        ml_source = (
            jockey_match.group(2)
            if jockey_match and (jockey_match.lastindex or 0) >= 2
            else None
        )
        if ml_source is None:
            ml_line = next((line for line in profile if re.search(r"\bML\s+[\d/-]+", line, re.I)), "")
            ml_match = re.search(r"\bML\s+([\d/-]+)", ml_line, re.I)
            ml_source = ml_match.group(1) if ml_match else None
        ml_text, ml_decimal = _normalize_ml(ml_source)
        key = horse_key(raw_name)
        if key in seen_keys:
            duplicate_keys.append(key)
        seen_keys.add(key)
        entries.append({
            "post": post,
            "horse_raw": raw_name,
            "horse_key": key,
            "trainer": trainer_match.group(1).strip() if trainer_match else None,
            "jockey": jockey_match.group(1).strip() if jockey_match else None,
            "morning_line_source_text": ml_source,
            "morning_line_text": ml_text,
            "morning_line_decimal": ml_decimal,
            "is_scratched": False,
            "scratch_source": None,
            "past_performances": _parse_past_performances(lines[pp_idx + 1:end_idx]),
        })

    diagnostics = {
        "entry_anchor_count": len(anchors),
        "duplicate_horse_keys": duplicate_keys,
        "unmatched_entry_keys": [],
    }
    return sorted(entries, key=lambda entry: entry["post"]), diagnostics


def _parse_stacked_entries(lines: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse 1/ST's alternate post / PP / horse / connections stack.

    In this layout an auxiliary program number (or ``SCR``) is emitted after
    the trainer, and the morning line is emitted on its own line. Anchoring on
    the adjacent post and PP lines prevents PP-history text from becoming an
    entry merely because it contains a number.
    """
    anchors: list[tuple[int, int]] = []
    for idx in range(len(lines) - 2):
        post_match = re.fullmatch(r"(\d{1,2})", lines[idx])
        pp_match = re.fullmatch(r"PP\s*(\d{1,2})", lines[idx + 1], re.I)
        if post_match and pp_match and int(post_match.group(1)) == int(pp_match.group(1)):
            anchors.append((idx, int(post_match.group(1))))

    entries: list[dict[str, Any]] = []
    duplicate_keys: list[str] = []
    seen_keys: set[str] = set()
    for position, (start_idx, post) in enumerate(anchors):
        end_idx = anchors[position + 1][0] if position + 1 < len(anchors) else len(lines)
        profile = lines[start_idx + 2:end_idx]
        raw_name = profile[0].strip() if profile else ""
        jockey_line = next((line for line in profile if re.match(r"^J:", line, re.I)), "")
        trainer_line = next((line for line in profile if re.match(r"^T:", line, re.I)), "")
        jockey_match = re.match(r"^J:\s*(.+?)(?:\s+ML\s+[\d/-]+)?$", jockey_line, re.I)
        trainer_match = re.match(r"^T:\s*(.+)$", trainer_line, re.I)
        ml_line = next((line for line in profile if re.match(r"^ML\b", line, re.I)), "")
        ml_match = re.search(r"\bML\s*:?\s*([\d/-]+)", ml_line, re.I)
        if not ml_match:
            ml_match = re.search(r"\bML\s*:?\s*([\d/-]+)", jockey_line, re.I)
        ml_source = ml_match.group(1) if ml_match else None
        ml_text, ml_decimal = _normalize_ml(ml_source)
        is_scratched = any(re.fullmatch(r"SCR", line, re.I) for line in profile)
        key = horse_key(raw_name)
        if key in seen_keys:
            duplicate_keys.append(key)
        seen_keys.add(key)
        entries.append({
            "post": post,
            "horse_raw": raw_name,
            "horse_key": key,
            "trainer": trainer_match.group(1).strip() if trainer_match else None,
            "jockey": jockey_match.group(1).strip() if jockey_match else None,
            "morning_line_source_text": ml_source,
            "morning_line_text": ml_text,
            "morning_line_decimal": ml_decimal,
            "is_scratched": is_scratched,
            "scratch_source": "1stbet_pdf_scr" if is_scratched else None,
            "past_performances": _parse_past_performances(profile),
        })

    diagnostics = {
        "entry_anchor_count": len(anchors),
        "duplicate_horse_keys": duplicate_keys,
        "unmatched_entry_keys": [],
        "entry_parser_strategy": "stacked",
    }
    return sorted(entries, key=lambda entry: entry["post"]), diagnostics


def _parse_name_aux_post_jockey_pp_trainer_entries(
    lines: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse 1/ST's name / auxiliary / post / connections column order.

    pdfplumber can read the name column before the post and connection columns:
    ``LEXINGTON PIKE 27`` / ``1`` / ``J: ... ML 20`` / ``PP1`` / ``T: ...``.
    The complete five-line anchor is deliberately strict so a PP-history line
    cannot masquerade as a runner. A scratch is carried on the name line.
    """
    anchors: list[tuple[int, int, str, bool, re.Match[str], re.Match[str]]] = []
    for idx in range(len(lines) - 4):
        parsed_name = _name_and_source_scratch(lines[idx])
        post_match = re.fullmatch(r"(\d{1,2})", lines[idx + 1])
        jockey_match = re.fullmatch(r"J:\s*(.+?)\s+ML\s+([\d/-]+)", lines[idx + 2], re.I)
        pp_match = re.fullmatch(r"PP\s*(\d{1,2})", lines[idx + 3], re.I)
        trainer_match = re.fullmatch(r"T:\s*(.+)", lines[idx + 4], re.I)
        if not (parsed_name and post_match and jockey_match and pp_match and trainer_match):
            continue
        post = int(post_match.group(1))
        if post != int(pp_match.group(1)):
            continue
        raw_name, is_scratched = parsed_name
        anchors.append((idx, post, raw_name, is_scratched, jockey_match, trainer_match))

    entries: list[dict[str, Any]] = []
    duplicate_keys: list[str] = []
    seen_keys: set[str] = set()
    for position, (start_idx, post, raw_name, is_scratched, jockey_match, trainer_match) in enumerate(anchors):
        end_idx = anchors[position + 1][0] if position + 1 < len(anchors) else len(lines)
        pp_lines = lines[start_idx + 5:end_idx]
        ml_source = jockey_match.group(2)
        ml_text, ml_decimal = _normalize_ml(ml_source)
        key = horse_key(raw_name)
        if key in seen_keys:
            duplicate_keys.append(key)
        seen_keys.add(key)
        entries.append({
            "post": post,
            "horse_raw": raw_name,
            "horse_key": key,
            "trainer": trainer_match.group(1).strip(),
            "jockey": jockey_match.group(1).strip(),
            "morning_line_source_text": ml_source,
            "morning_line_text": ml_text,
            "morning_line_decimal": ml_decimal,
            "is_scratched": is_scratched,
            "scratch_source": "1stbet_pdf_scr" if is_scratched else None,
            "past_performances": _parse_past_performances(pp_lines),
        })

    diagnostics = {
        "entry_anchor_count": len(anchors),
        "duplicate_horse_keys": duplicate_keys,
        "unmatched_entry_keys": [],
    }
    return sorted(entries, key=lambda entry: entry["post"]), diagnostics


def _name_and_source_scratch(line: str) -> tuple[str, bool] | None:
    """Return a source horse name after removing only allowed trailing tokens."""
    candidate = line.strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9'.()\- /]*", candidate):
        return None
    scratch_match = re.fullmatch(r"(.+?)\s+SCR", candidate, re.I)
    if scratch_match:
        name = scratch_match.group(1).strip()
        return (name, True) if name else None
    # The source auxiliary is an integer. Do not strip fractional tokens such
    # as ``7/2`` because they are not the documented auxiliary value.
    aux_match = re.fullmatch(r"(.+?)\s+\d+", candidate)
    name = aux_match.group(1).strip() if aux_match else candidate
    return (name, False) if name else None


def _parse_past_performances(lines: list[str]) -> list[dict[str, Any]]:
    starts: list[dict[str, Any]] = []
    date_anchors = [(idx, _TRACK_DATE_RE.match(line)) for idx, line in enumerate(lines)]
    date_anchors = [(idx, match) for idx, match in date_anchors if match]
    for pos, (idx, match) in enumerate(date_anchors):
        end = date_anchors[pos + 1][0] if pos + 1 < len(date_anchors) else len(lines)
        block = [line for line in lines[idx + 1:end] if line and not _PAGE_NOISE_RE.match(line)]
        result_line = next((line for line in block if _FINISH_RE.match(line)), "")
        distance_line = next((line for line in block if _SURFACE_GOING_RE.search(line)), "")
        if not result_line:
            continue
        finish = _FINISH_RE.match(result_line)
        field = _FIELD_RE.search(result_line)
        odds = _ODDS_RE.search(result_line)
        class_match = _CLASS_RE.search(result_line)
        combined = " ".join(block)
        purse = _PURSE_RE.search(combined)
        distance = _DIST_RE.search(distance_line)
        surface_going = _SURFACE_GOING_RE.search(distance_line)
        comment_start = block.index(distance_line) + 1 if distance_line in block else len(block)
        comments = [
            line for line in block[comment_start:]
            if not _PAGE_NOISE_RE.match(line) and not _FINISH_RE.match(line)
        ]
        month = _MONTHS[match.group(3).lower()]
        starts.append({
            "start_date": f"{int(match.group(4)):04d}-{month:02d}-{int(match.group(2)):02d}",
            "track_name": match.group(1).strip().title(),
            "finish_position": int(finish.group(1)) if finish else None,
            "field_size": int(field.group(1)) if field else None,
            "class_family": class_match.group(1).rstrip(".").upper() if class_match else None,
            "purse_usd": int(purse.group(1).replace(",", "")) if purse else None,
            "distance_furlongs": _distance_furlongs(distance.group(1)) if distance else None,
            "surface": _surface(surface_going.group(1)) if surface_going else None,
            "going": surface_going.group(2).lower() if surface_going and surface_going.group(2) != "-" else None,
            "odds_fractional": odds.group(1) if odds else None,
            "trip_comment": " ".join(comments).strip() or None,
        })
    return starts


def _coverage(entries: list[Mapping[str, Any]]) -> dict[str, float]:
    count = len(entries)
    if not count:
        return _empty_coverage()

    def fraction(predicate) -> float:
        return round(sum(bool(predicate(entry)) for entry in entries) / count, 2)

    return {
        "recent_form": fraction(lambda e: e.get("past_performances")),
        "distance_surface_fit": fraction(lambda e: any(
            pp.get("distance_furlongs") is not None and pp.get("surface")
            for pp in e.get("past_performances") or []
        )),
        "run_style_proxy": fraction(lambda e: any(
            _RUN_STYLE_TERMS.search(pp.get("trip_comment") or "")
            for pp in e.get("past_performances") or []
        )),
        "trip_flags": fraction(lambda e: any(
            _TRIP_TERMS.search(pp.get("trip_comment") or "")
            for pp in e.get("past_performances") or []
        )),
        "off_track_evidence": fraction(lambda e: any(
            (pp.get("going") or "").lower() in _OFF_TRACK
            for pp in e.get("past_performances") or []
        )),
        "speed_figures": 0.0,
        "fractional_pace": 0.0,
        "workouts": 0.0,
        "live_odds": 0.0,
    }


def _empty_coverage() -> dict[str, float]:
    return {
        "recent_form": 0.0,
        "distance_surface_fit": 0.0,
        "run_style_proxy": 0.0,
        "trip_flags": 0.0,
        "off_track_evidence": 0.0,
        "speed_figures": 0.0,
        "fractional_pace": 0.0,
        "workouts": 0.0,
        "live_odds": 0.0,
    }


def _empty_payload(filename: str, sha256: str, uploaded: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "provider": "1stbet", "source_type": "pdf",
            "filename": Path(filename).name, "sha256": sha256,
            "uploaded_at_utc": uploaded,
        },
        "race": {field: None for field in sorted(REQUIRED_RACE_FIELDS)},
        "entries": [],
    }


def _normalize_ml(raw: str | None) -> tuple[str | None, float | None]:
    if not raw:
        return None, None
    raw = raw.strip()
    if raw.isdigit():
        numerator, denominator = int(raw), 1
    else:
        match = re.fullmatch(r"(\d+)\s*[-/]\s*(\d+)", raw)
        if not match or int(match.group(2)) == 0:
            return None, None
        numerator, denominator = int(match.group(1)), int(match.group(2))
    return f"{numerator}-{denominator}", round(numerator / denominator + 1.0, 3)


def _distance_furlongs(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.upper().replace(" ", "")
    unit = text[-1]
    value = text[:-1]
    if "/" in value:
        whole_match = re.fullmatch(r"(\d+)(\d+)/(\d+)", value)
        if not whole_match:
            return None
        number = int(whole_match.group(1)) + int(whole_match.group(2)) / int(whole_match.group(3))
    else:
        try:
            number = float(value)
        except ValueError:
            return None
    return round(number * 8 if unit == "M" else number, 3)


def _surface(raw: str | None) -> str | None:
    if not raw:
        return None
    normalized = raw.lower().replace(" ", "-")
    if normalized.replace("-", "") == "innerturf":
        return "turf"
    return "all_weather" if normalized == "all-weather" else normalized


def _to_24_hour(raw: str) -> str | None:
    try:
        return datetime.strptime(raw.strip().upper(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return None


def _display_name(raw: str) -> str:
    text = raw.title()
    return re.sub(r"\(([A-Za-z]{2,3})\)", lambda m: f"({m.group(1).upper()})", text)


def _missing_posts(field_size: int | None, entries: list[Mapping[str, Any]]) -> list[int]:
    if not field_size:
        return []
    present = {int(entry["post"]) for entry in entries if entry.get("post") is not None}
    return [post for post in range(1, int(field_size) + 1) if post not in present]


def _new_run_id(uploaded: str, sha256: str) -> str:
    stamp = re.sub(r"[^0-9]", "", uploaded)[:14]
    return f"{stamp}_{sha256[:12]}_{uuid4().hex[:8]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
