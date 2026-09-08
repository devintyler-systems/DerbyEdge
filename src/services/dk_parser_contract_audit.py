"""Read-only contract-audit regression for hardened DraftKings markdown parsing.

Measures pre-race *evidence coverage* on DK markdown fixtures after the runner
retention fix -- detected / active / scratched starters, runner identity
completeness, ALL RACES / WORKOUTS coverage -- and records the existing
read-only validator's verdict.

Strictly read-only:

* it parses fixtures with the current parser and calls
  :func:`validate_draftkings_markdown_card` (a pure, DB-free validator);
* it never persists canonical data, never opens or writes SQLite, never mutates
  the feature builder, and never changes readiness policy;
* feature-vector completeness is only computable after persistence + feature
  build, so it is reported as ``NOT_AVAILABLE_READ_ONLY``.

Every emitted row is labelled ``AUDIT_ONLY_READ_ONLY``.
"""
from __future__ import annotations

import csv
import dataclasses
import json
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.ingest.draftkings_markdown import (
    DraftKingsMarkdownCard,
    Entry,
    parse_draftkings_markdown,
    validate_draftkings_markdown_card,
)

AUDIT_LABEL = "AUDIT_ONLY_READ_ONLY"
AUDIT_VERSION = "1.0"
FEATURE_VECTOR_AUDIT_STATUS = "NOT_AVAILABLE_READ_ONLY"

# Minimum runner-identity field set for completeness scoring.  These map onto
# existing parser output; nothing here renames parser fields.
IDENTITY_FIELDS: tuple[str, ...] = (
    "post_position",
    "horse_name",
    "age",
    "sex",
    "color",
    "jockey_name",
    "trainer_name",
    "sire",
    "dam",
    "breeder",
    "owner",
)

_FALLBACK_AS_OF = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@dataclasses.dataclass(frozen=True)
class RunnerAudit:
    fixture_name: str
    track_name: str | None
    race_number: int | None
    runner_order: int
    post_position: int | None
    program_number: str | None
    horse_name: str | None
    scratched_flag: bool
    starter_status: str
    has_all_races_section: bool
    has_workouts_section: bool
    runner_retained_without_history: bool
    runner_parse_warning_count: int
    identity_fields_present_count: int
    identity_fields_expected_count: int
    identity_completeness_ratio: float
    missing_identity_fields: tuple[str, ...]
    scratch_basis: str
    status: str


@dataclasses.dataclass(frozen=True)
class CardAudit:
    fixture_name: str
    fixture_path: str
    source_sha256: str
    track_name: str | None
    race_number: int | None
    as_of: str
    detected_runner_count: int
    active_runner_count: int
    scratched_runner_count: int
    retained_without_history_count: int
    runners_with_all_races_count: int
    runners_with_workouts_count: int
    pct_with_all_races_section: float
    pct_with_workouts_section: float
    parser_warning_count: int
    card_parser_warning_count: int
    identity_completeness_ratio_mean: float
    past_performance_row_count: int
    workout_row_count: int
    # Optional read-only validator impact
    validator_invoked: bool
    scoring_ready_under_current_rules: bool | None
    validator_missing_fields_count: int
    validator_missing_fields: tuple[str, ...]
    validator_warning_count: int
    feature_vector_audit_status: str
    feature_vector_complete_runner_count: int | None
    feature_vector_incomplete_runner_count: int | None
    status: str
    runners: tuple[RunnerAudit, ...]

    def card_row(self) -> dict[str, Any]:
        row = {k: v for k, v in dataclasses.asdict(self).items() if k != "runners"}
        row["validator_missing_fields"] = " | ".join(self.validator_missing_fields)
        return row


@dataclasses.dataclass(frozen=True)
class DkParserContractAudit:
    cards: tuple[CardAudit, ...]
    summary: dict[str, Any]


# --------------------------------------------------------------------------- #
# Metric helpers                                                               #
# --------------------------------------------------------------------------- #
def _as_of_for(card_as_of_source: Path, override: datetime | None) -> datetime:
    if override is not None:
        if override.tzinfo is None:
            raise ValueError("as_of override must be timezone-aware")
        return override
    from src.ingest.draftkings_markdown import _parse_filename_date  # local, read-only helper

    parsed = _parse_filename_date(card_as_of_source.name)
    if parsed is not None:
        return datetime.combine(parsed, time(12, 0), tzinfo=timezone.utc)
    return _FALLBACK_AS_OF


def _identity_values(entry: Entry) -> dict[str, Any]:
    profile = entry.horse_profile
    return {
        "post_position": entry.post_position,
        "horse_name": entry.horse_name,
        "age": profile.age,
        "sex": profile.sex,
        "color": profile.color,
        "jockey_name": entry.jockey,
        "trainer_name": entry.trainer,
        "sire": profile.sire,
        "dam": profile.dam,
        "breeder": entry.breeder or profile.breeder,
        "owner": entry.owner,
    }


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _round(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _runner_audit(fixture_name: str, card: DraftKingsMarkdownCard, order: int, entry: Entry) -> RunnerAudit:
    values = _identity_values(entry)
    present = {name for name, value in values.items() if _present(value)}
    missing = tuple(name for name in IDENTITY_FIELDS if name not in present)
    present_count = len(IDENTITY_FIELDS) - len(missing)

    scratched = bool(getattr(entry, "is_scratched", False))
    raw_ml = (entry.raw_morning_line or entry.morning_line or "").strip()
    if scratched:
        scratch_basis = "parser is_scratched flag (odds/header token == SCR)"
        starter_status = "SCRATCHED"
    else:
        scratch_basis = f"active: morning_line={raw_ml!r}"
        starter_status = "ACTIVE"

    return RunnerAudit(
        fixture_name=fixture_name,
        track_name=card.race.track,
        race_number=card.race.race_number,
        runner_order=order,
        post_position=entry.post_position,
        program_number=entry.program_number,
        horse_name=entry.horse_name,
        scratched_flag=scratched,
        starter_status=starter_status,
        has_all_races_section=bool(getattr(entry, "has_all_races_section", False)),
        has_workouts_section=bool(getattr(entry, "has_workouts_section", False)),
        runner_retained_without_history=bool(getattr(entry, "runner_retained_without_history", False)),
        runner_parse_warning_count=len(getattr(entry, "runner_parse_warnings", []) or []),
        identity_fields_present_count=present_count,
        identity_fields_expected_count=len(IDENTITY_FIELDS),
        identity_completeness_ratio=_round(present_count, len(IDENTITY_FIELDS)),
        missing_identity_fields=missing,
        scratch_basis=scratch_basis,
        status=AUDIT_LABEL,
    )


def audit_fixture(path: str | Path, *, as_of: datetime | None = None) -> CardAudit:
    """Audit a single DK markdown fixture (read-only)."""
    fixture = Path(path)
    if not fixture.is_file():
        raise FileNotFoundError(f"DK markdown fixture not found: {fixture}")

    resolved_as_of = _as_of_for(fixture, as_of)
    card = parse_draftkings_markdown(fixture, as_of=resolved_as_of)
    runners = tuple(
        _runner_audit(fixture.name, card, order, entry)
        for order, entry in enumerate(card.entries, start=1)
    )

    detected = len(runners)
    scratched = sum(1 for r in runners if r.scratched_flag)
    with_all_races = sum(1 for r in runners if r.has_all_races_section)
    with_workouts = sum(1 for r in runners if r.has_workouts_section)
    identity_mean = round(
        sum(r.identity_completeness_ratio for r in runners) / detected, 6
    ) if detected else 0.0

    # Read-only validator impact (pure; no DB).
    validation = validate_draftkings_markdown_card(card)
    missing_fields = tuple(dict.fromkeys(validation.errors))

    return CardAudit(
        fixture_name=fixture.name,
        fixture_path=fixture.resolve().as_posix(),
        source_sha256=card.source_sha256,
        track_name=card.race.track,
        race_number=card.race.race_number,
        as_of=resolved_as_of.isoformat(),
        detected_runner_count=detected,
        active_runner_count=detected - scratched,
        scratched_runner_count=scratched,
        retained_without_history_count=sum(1 for r in runners if r.runner_retained_without_history),
        runners_with_all_races_count=with_all_races,
        runners_with_workouts_count=with_workouts,
        pct_with_all_races_section=_round(with_all_races, detected),
        pct_with_workouts_section=_round(with_workouts, detected),
        parser_warning_count=sum(r.runner_parse_warning_count for r in runners),
        card_parser_warning_count=len(card.parser_warnings or []),
        identity_completeness_ratio_mean=identity_mean,
        past_performance_row_count=len(card.past_performances),
        workout_row_count=len(card.workouts),
        validator_invoked=True,
        scoring_ready_under_current_rules=bool(validation.passed),
        validator_missing_fields_count=len(missing_fields),
        validator_missing_fields=missing_fields,
        validator_warning_count=len(validation.warnings),
        feature_vector_audit_status=FEATURE_VECTOR_AUDIT_STATUS,
        feature_vector_complete_runner_count=None,
        feature_vector_incomplete_runner_count=None,
        status=AUDIT_LABEL,
        runners=runners,
    )


def audit_fixtures(paths: Iterable[str | Path], *, as_of: datetime | None = None) -> DkParserContractAudit:
    cards = tuple(
        sorted((audit_fixture(p, as_of=as_of) for p in paths), key=lambda c: c.fixture_name)
    )
    totals = {
        "detected_runner_count": sum(c.detected_runner_count for c in cards),
        "active_runner_count": sum(c.active_runner_count for c in cards),
        "scratched_runner_count": sum(c.scratched_runner_count for c in cards),
        "retained_without_history_count": sum(c.retained_without_history_count for c in cards),
        "runners_with_all_races_count": sum(c.runners_with_all_races_count for c in cards),
        "runners_with_workouts_count": sum(c.runners_with_workouts_count for c in cards),
        "parser_warning_count": sum(c.parser_warning_count for c in cards),
        "card_parser_warning_count": sum(c.card_parser_warning_count for c in cards),
    }
    total_detected = totals["detected_runner_count"] or 0
    summary = {
        "status": AUDIT_LABEL,
        "audit_version": AUDIT_VERSION,
        "readiness_policy_changed": False,
        "feature_vector_audit_status": FEATURE_VECTOR_AUDIT_STATUS,
        "card_count": len(cards),
        "totals": totals,
        "overall_pct_with_all_races_section": _round(totals["runners_with_all_races_count"], total_detected),
        "overall_pct_with_workouts_section": _round(totals["runners_with_workouts_count"], total_detected),
        "cards": [
            {
                "fixture_name": c.fixture_name,
                "track_name": c.track_name,
                "race_number": c.race_number,
                "source_sha256": c.source_sha256,
                "detected_runner_count": c.detected_runner_count,
                "active_runner_count": c.active_runner_count,
                "scratched_runner_count": c.scratched_runner_count,
                "retained_without_history_count": c.retained_without_history_count,
                "pct_with_all_races_section": c.pct_with_all_races_section,
                "pct_with_workouts_section": c.pct_with_workouts_section,
                "identity_completeness_ratio_mean": c.identity_completeness_ratio_mean,
                "parser_warning_count": c.parser_warning_count,
                "scoring_ready_under_current_rules": c.scoring_ready_under_current_rules,
                "validator_missing_fields_count": c.validator_missing_fields_count,
            }
            for c in cards
        ],
    }
    return DkParserContractAudit(cards=cards, summary=summary)


# --------------------------------------------------------------------------- #
# Deterministic artifacts                                                      #
# --------------------------------------------------------------------------- #
_CARD_COLUMNS = [
    "fixture_name", "fixture_path", "source_sha256", "track_name", "race_number", "as_of",
    "detected_runner_count", "active_runner_count", "scratched_runner_count",
    "retained_without_history_count", "runners_with_all_races_count", "runners_with_workouts_count",
    "pct_with_all_races_section", "pct_with_workouts_section", "parser_warning_count",
    "card_parser_warning_count", "identity_completeness_ratio_mean",
    "past_performance_row_count", "workout_row_count",
    "validator_invoked", "scoring_ready_under_current_rules", "validator_missing_fields_count",
    "validator_missing_fields", "validator_warning_count", "feature_vector_audit_status",
    "feature_vector_complete_runner_count", "feature_vector_incomplete_runner_count", "status",
]
_RUNNER_COLUMNS = [
    "fixture_name", "track_name", "race_number", "runner_order", "post_position", "program_number",
    "horse_name", "scratched_flag", "starter_status", "has_all_races_section", "has_workouts_section",
    "runner_retained_without_history", "runner_parse_warning_count", "identity_fields_present_count",
    "identity_fields_expected_count", "identity_completeness_ratio", "missing_identity_fields",
    "scratch_basis", "status",
]


def _write_csv(path: Path, columns: list[str], rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _report_markdown(audit: DkParserContractAudit) -> str:
    lines: list[str] = []
    lines.append("# DK markdown parser contract audit")
    lines.append("")
    lines.append("## 1. Scope")
    lines.append("")
    lines.append(
        "Read-only regression measuring pre-race evidence coverage produced by the "
        "current DraftKings markdown parser after the runner-retention hardening. It "
        "parses fixtures and calls the existing pure validator "
        "`validate_draftkings_markdown_card`. It performs no persistence, opens no "
        "SQLite, builds no features, and changes no readiness, scoring, calibration, "
        "fair-odds, market, or wager logic."
    )
    lines.append("")
    lines.append("## 2. Fixtures audited")
    lines.append("")
    for card in audit.cards:
        lines.append(f"- `{card.fixture_name}` — sha256 `{card.source_sha256[:16]}…` (as_of `{card.as_of}`)")
    lines.append("")
    lines.append("## 3. Card-level table")
    lines.append("")
    lines.append(
        "| fixture | track | race | detected | active | scratched | retained_no_history "
        "| pct_all_races | pct_workouts | parser_warnings | scoring_ready_current_rules |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for c in audit.cards:
        ready = (
            "NOT_AVAILABLE_READ_ONLY"
            if c.scoring_ready_under_current_rules is None
            else str(c.scoring_ready_under_current_rules)
        )
        lines.append(
            f"| {c.fixture_name} | {c.track_name or '?'} | {c.race_number if c.race_number is not None else '?'} "
            f"| {c.detected_runner_count} | {c.active_runner_count} | {c.scratched_runner_count} "
            f"| {c.retained_without_history_count} | {_fmt_pct(c.pct_with_all_races_section)} "
            f"| {_fmt_pct(c.pct_with_workouts_section)} | {c.parser_warning_count} | {ready} |"
        )
    lines.append("")
    lines.append("## 4. Runner identity completeness notes")
    lines.append("")
    lines.append(
        f"Identity completeness scores each retained runner against {len(IDENTITY_FIELDS)} "
        f"fields: {', '.join(IDENTITY_FIELDS)}. Ratio = present / expected, bounded [0, 1]."
    )
    for c in audit.cards:
        low = [r for r in c.runners if r.identity_completeness_ratio < 1.0]
        lines.append(
            f"- `{c.fixture_name}`: mean ratio {c.identity_completeness_ratio_mean:.3f}; "
            f"{len(c.runners) - len(low)}/{len(c.runners)} runners fully complete."
        )
        for r in low:
            lines.append(
                f"  - {r.horse_name or '?'} (order {r.runner_order}, {r.starter_status}): "
                f"{r.identity_fields_present_count}/{r.identity_fields_expected_count} — "
                f"missing {', '.join(r.missing_identity_fields) or 'none'}"
            )
    lines.append("")
    lines.append("## 5. Validator / feature-audit impact notes")
    lines.append("")
    lines.append(
        "`validate_draftkings_markdown_card` is invoked read-only (its verdict is "
        "reported, its logic is unchanged). `scoring_ready_current_rules` is that "
        "verdict under today's rules — no card is made newly eligible."
    )
    for c in audit.cards:
        lines.append(
            f"- `{c.fixture_name}`: validator_invoked={c.validator_invoked}, "
            f"scoring_ready_under_current_rules={c.scoring_ready_under_current_rules}, "
            f"missing_fields={c.validator_missing_fields_count}"
        )
        for field in c.validator_missing_fields:
            lines.append(f"  - {field}")
    lines.append("")
    lines.append(
        f"Feature-vector completeness requires persistence + feature build, so it is "
        f"`{FEATURE_VECTOR_AUDIT_STATUS}` here (`feature_vector_complete_runner_count` / "
        f"`feature_vector_incomplete_runner_count` are null)."
    )
    lines.append("")
    lines.append("## 6. Readiness gates")
    lines.append("")
    lines.append(
        "Readiness gates were **not changed**. This audit does not modify "
        "`validate_draftkings_markdown_card`, `require_scoring_ready`, "
        "`persist_validated_draftkings_markdown`, `markdown_card_score_readiness`, the "
        "feature builder, scoring, calibration, fair-odds, market, or wager logic, and "
        "adds no readiness exceptions."
    )
    lines.append("")
    lines.append("## 7. Row labelling")
    lines.append("")
    lines.append(f"All card and runner rows in every artifact are `{AUDIT_LABEL}`.")
    lines.append("")
    return "\n".join(lines)


def write_audit_artifacts(audit: DkParserContractAudit, output_dir: str | Path) -> dict[str, str]:
    """Write the four deterministic audit artifacts under ``output_dir``."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cards_csv = out / "dk_parser_contract_audit_cards.csv"
    runners_csv = out / "dk_parser_contract_audit_runners.csv"
    summary_json = out / "dk_parser_contract_audit_summary.json"
    report_md = out / "dk_parser_contract_audit_report.md"

    _write_csv(cards_csv, _CARD_COLUMNS, (c.card_row() for c in audit.cards))
    _write_csv(
        runners_csv,
        _RUNNER_COLUMNS,
        (
            {
                **{k: v for k, v in dataclasses.asdict(r).items() if k != "missing_identity_fields"},
                "missing_identity_fields": " | ".join(r.missing_identity_fields),
            }
            for c in audit.cards
            for r in c.runners
        ),
    )
    summary_json.write_text(
        json.dumps(audit.summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report_md.write_text(_report_markdown(audit), encoding="utf-8")

    return {
        "cards_csv": cards_csv.resolve().as_posix(),
        "runners_csv": runners_csv.resolve().as_posix(),
        "summary_json": summary_json.resolve().as_posix(),
        "report_md": report_md.resolve().as_posix(),
    }
