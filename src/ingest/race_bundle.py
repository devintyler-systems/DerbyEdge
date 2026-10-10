"""One-file race bundle: DK Advanced + TwinSpires + DK Basic pasted together.

The three tabs are told apart by their header rows, in any order:

* DK Advanced  - header contains ``Sire / Dam``
* TwinSpires   - header contains ``STYLE``
* DK Basic     - header contains ``MED/WT/EQP``

An optional fourth block, the TwinSpires ``RACE STATS`` panel (starts at a line reading exactly ``RACE STATS``), is
captured as-is and stored beside the race; it never feeds scoring.  The bundle passes without it, and a present but
malformed block is a hard failure that names the problem.

Track, race number and post time come from the DK Advanced header; the date from
the upload filename (as for single-file DK uploads); the capture time is the
moment the app received the file.  Nothing needs to be typed into the file.

The three sections are cross-checked per program number.  Any disagreement on
horse, morning line, scratch status or connections is a hard failure with the
conflicting values named; nothing is silently overwritten.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from src.derbyedge.odds_math import morningline_to_decimal
from src.derbyedge.tracks import resolve_track
from src.ingest.draftkings_basic_grid import BasicGrid, BasicGridError, parse_basic_grid
from src.ingest.draftkings_markdown import (
    DraftKingsMarkdownCard, ValidationResult, parse_draftkings_markdown,
    validate_draftkings_markdown_card,
)
from src.ingest.race_time import post_time_to_utc
from src.ingest.twinspires_markdown import TwinSpiresMarkdownCard, parse_twinspires_markdown
from src.ingest.twinspires_race_stats import HEADING as RACE_STATS_HEADING
from src.ingest.twinspires_race_stats import RaceStats, RaceStatsError, identity_mismatches, parse_race_stats
from src.utils.horse_norm import normalize_horse_name

SECTION_KINDS = ("dk_advanced", "twinspires", "dk_basic")      # the three tabs a bundle requires
OPTIONAL_KINDS = ("race_stats",)                                # captured when present, never required
_RACE_LINE = re.compile(r"^RACE\s+\d+$", re.I)


@dataclasses.dataclass
class Reconciliation:
    conflicts: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    weights_merged: int = 0
    programs_checked: int = 0


@dataclasses.dataclass
class RaceBundle:
    card: DraftKingsMarkdownCard
    validation: ValidationResult
    basic: BasicGrid | None
    twinspires: TwinSpiresMarkdownCard | None
    twinspires_text: str
    reconciliation: Reconciliation
    sections: dict[str, tuple[int, int]]
    bundle_sha256: str
    captured_at: datetime
    post_utc: str | None
    post_source: str
    late_capture: bool
    race_stats: RaceStats | None = None


def _normalize_newlines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _header_kind(lines: list[str], at: int) -> str | None:
    window = [l.strip().lower() for l in lines[at:at + 40] if l.strip()][:12]
    if "sire / dam" in window:
        return "dk_advanced"
    if "med/wt/eqp" in window:
        return "dk_basic"
    if "style" in window and "run" in window:
        return "twinspires"
    return None


def locate_sections(text: str) -> tuple[dict[str, tuple[int, int]], list[str], list[str]]:
    """Return ``({kind: (first_line, end_line)}, errors, lines)`` over normalized lines."""
    lines = _normalize_newlines(text)
    starts: dict[str, list[int]] = {k: [] for k in SECTION_KINDS + OPTIONAL_KINDS}
    for i, line in enumerate(lines):
        if line.strip() == RACE_STATS_HEADING:
            starts["race_stats"].append(i)
            continue
        if line.strip() != "#":
            continue
        kind = _header_kind(lines, i)
        if kind == "dk_advanced":
            race_at = next((j for j in range(i, -1, -1) if _RACE_LINE.match(lines[j].strip())), None)
            if race_at is None:
                continue
            first = next((j for j in range(race_at - 1, -1, -1) if lines[j].strip()), None)
            if first is None:
                continue
            starts[kind].append(first)
        elif kind:
            starts[kind].append(i)
    errors: list[str] = []
    for kind, found in starts.items():
        if len(found) > 1:
            errors.append(f"bundle contains {len(found)} {kind} sections; exactly one is allowed")
    firsts = sorted((found[0], kind) for kind, found in starts.items() if found)
    sections: dict[str, tuple[int, int]] = {}
    for n, (first, kind) in enumerate(firsts):
        end = firsts[n + 1][0] if n + 1 < len(firsts) else len(lines)
        sections[kind] = (first, end)
    return sections, errors, lines


def is_race_bundle(text: str) -> bool:
    """True when the text holds a DK Advanced section plus at least one other tab."""
    sections, _errors, _lines = locate_sections(text)
    return "dk_advanced" in sections and sum(1 for k in SECTION_KINDS if k in sections) >= 2


def _person_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lstrip("*").casefold()).strip()


def _ml_equal(a: str | None, b: str | None) -> bool:
    if (a or "").strip() == (b or "").strip():
        return True
    da, db = morningline_to_decimal(a), morningline_to_decimal(b)
    return da is not None and db is not None and abs(da - db) < 1e-9


def reconcile_bundle(
    card: DraftKingsMarkdownCard, basic: BasicGrid | None, ts: TwinSpiresMarkdownCard | None,
) -> Reconciliation:
    out = Reconciliation()
    dk = {(e.program_number or "").upper(): e for e in card.entries}

    def conflict(program: str, field: str, dk_value: Any, source: str, other: Any) -> None:
        out.conflicts.append({"program": program, "field": field, "draftkings_advanced": dk_value,
                              "source": source, "value": other})
        out.errors.append(
            f"CONFLICT program {program} {field}: DK Advanced={dk_value!r} vs {source}={other!r}"
        )

    def program_sets(source: str, programs: set[str]) -> None:
        for p in sorted(set(dk) - programs):
            out.errors.append(f"program {p} is in DK Advanced but missing from {source}")
        for p in sorted(programs - set(dk)):
            out.errors.append(f"program {p} is in {source} but missing from DK Advanced")

    if basic is not None:
        rows = {r.program_number.upper(): r for r in basic.rows}
        program_sets("DK Basic", set(rows))
        for p, r in rows.items():
            e = dk.get(p)
            if e is None:
                continue
            out.programs_checked += 1
            if normalize_horse_name(e.horse_name or "") != normalize_horse_name(r.horse_name):
                conflict(p, "horse", e.horse_name, "DK Basic", r.horse_name)
            if not _ml_equal(e.morning_line, r.morning_line):
                conflict(p, "morning_line", e.morning_line, "DK Basic", r.morning_line)
            if r.scratched != (not e.is_active):
                conflict(p, "scratched", not e.is_active, "DK Basic", r.scratched)
            if _person_key(e.jockey) != _person_key(r.jockey):
                conflict(p, "jockey", e.jockey, "DK Basic", r.jockey)
            if _person_key(e.trainer) != _person_key(r.trainer):
                conflict(p, "trainer", e.trainer, "DK Basic", r.trainer)
            if e.weight is None and r.weight is not None:
                e.weight = r.weight
                out.weights_merged += 1
            elif e.weight is not None and r.weight is not None and e.weight != r.weight:
                conflict(p, "weight", e.weight, "DK Basic", r.weight)
    if ts is not None:
        recs = {r.program_number.upper(): r for r in ts.records}
        recs.update({r.program_number.upper(): r for r in ts.scratched})
        program_sets("TwinSpires", set(recs))
        for p, r in recs.items():
            e = dk.get(p)
            if e is None:
                continue
            if normalize_horse_name(e.horse_name or "") != normalize_horse_name(r.horse_name):
                conflict(p, "horse", e.horse_name, "TwinSpires", r.horse_name)
            if r.morning_line is not None and not _ml_equal(e.morning_line, r.morning_line):
                conflict(p, "morning_line", e.morning_line, "TwinSpires", r.morning_line)
            if r.scratched != (not e.is_active):
                conflict(p, "scratched", not e.is_active, "TwinSpires", r.scratched)
        out.errors.extend(f"TwinSpires: {m}" for m in ts.parser_errors)
        out.warnings.extend(f"TwinSpires: {m}" for m in ts.parser_warnings)
    active = sum(1 for e in card.entries if e.is_active)
    if basic is not None and active != sum(1 for r in basic.rows if not r.scratched):
        out.errors.append("active runner count differs between DK Advanced and DK Basic")
    if ts is not None and active != len(ts.records):
        out.errors.append(
            f"active runner count differs between DK Advanced ({active}) and TwinSpires ({len(ts.records)})"
        )
    out.errors = list(dict.fromkeys(out.errors))
    return out


def parse_race_bundle(
    text: str, *, source_path: str, as_of: datetime, captured_at: datetime | None = None,
    required: tuple[str, ...] = SECTION_KINDS,
) -> RaceBundle:
    captured_at = captured_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        raise ValueError("captured_at must be timezone-aware")
    sections, section_errors, lines = locate_sections(text)
    errors = list(section_errors)
    for kind in required:
        if kind not in sections:
            errors.append(f"bundle is missing the {kind} section")
    if "dk_advanced" not in sections:
        raise ValueError("; ".join(errors) or "bundle has no DK Advanced section")

    def body(kind: str) -> str:
        first, end = sections[kind]
        return "\n".join(lines[first:end]) + "\n"

    card = parse_draftkings_markdown(body("dk_advanced"), source_path=source_path, as_of=as_of)
    basic: BasicGrid | None = None
    if "dk_basic" in sections:
        try:
            basic = parse_basic_grid(body("dk_basic"))
        except BasicGridError as exc:
            errors.append(f"DK Basic: {exc}")
    race_stats: RaceStats | None = None
    if "race_stats" in sections:
        try:
            race_stats = parse_race_stats(body("race_stats"))
        except RaceStatsError as exc:
            errors.append(f"RACE STATS block is malformed (remove the block to import without it): {exc}")
    ts_text = body("twinspires") if "twinspires" in sections else ""
    ts = (
        parse_twinspires_markdown(
            ts_text.encode("utf-8"), source_filename=source_path, declared_as_of=captured_at.isoformat(),
        ) if ts_text else None
    )
    recon = reconcile_bundle(card, basic, ts)   # merges Basic weights before validation
    race = card.race
    track_code = resolve_track(track_name=race.track or "").get("track_code")
    post_utc, post_source = post_time_to_utc(
        race.race_date, race.scheduled_post_time or race.post_time_display, track_code, captured_at=captured_at,
    )
    late = bool(post_utc) and captured_at >= datetime.fromisoformat(post_utc)
    if race_stats is not None:
        wrong = identity_mismatches(race_stats, track_code=track_code, distance=race.distance, surface=race.surface)
        if wrong:
            errors.append("RACE STATS block looks like a different race than the DK card (" + "; ".join(wrong) +
                          "); paste the block for this race, or remove it")
    validation = validate_draftkings_markdown_card(card)
    validation.errors = list(dict.fromkeys(validation.errors + errors + recon.errors))
    validation.warnings = list(dict.fromkeys(validation.warnings + recon.warnings))
    if post_utc is None:
        validation.warnings.append(f"post time unavailable ({post_source}); pre-post gates will not pass")
    if late:
        validation.warnings.append(
            f"LATE_CAPTURE: received {captured_at.isoformat()} at/after post {post_utc}; "
            "not eligible for live market/pace features"
        )
    validation.passed = not validation.errors
    return RaceBundle(
        card=card, validation=validation, basic=basic, twinspires=ts, twinspires_text=ts_text,
        reconciliation=recon, sections=sections,
        bundle_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        captured_at=captured_at, post_utc=post_utc, post_source=post_source, late_capture=late,
        race_stats=race_stats,
    )
