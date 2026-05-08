"""Shared helpers for race identity labels and lifecycle status."""
from __future__ import annotations


def format_race_label(race: dict) -> str:
    """Canonical race identity label.

    Returns 'CT R5 · 2026-05-07 · Allowance'.
    Falls back to 'CT · 2026-05-07 · Allowance · ID {card_id}' when
    race_number is absent, so the label is always unique.

    Accepts any dict with the standard race_cards / load_race_index keys.
    Secondary keys (track_code, race_date, race_type) are accepted as
    aliases for the primary names so this works on both selector dicts and
    outcomes-query dicts.
    """
    track = race.get("track_abbrev") or race.get("track_code") or "?"
    rnum  = race.get("race_number")
    date  = race.get("card_date") or race.get("race_date") or "?"
    name  = (
        race.get("stakes_name")
        or race.get("race_class")
        or race.get("race_type")
        or "Race"
    )
    if rnum:
        return f"{track} R{rnum} · {date} · {name}"
    card_id = race.get("card_id") or "?"
    return f"{track} · {date} · {name} · ID {card_id}"


def format_race_hint(race: dict) -> str:
    """Secondary hint line: '6.0f dirt · Field 8'.

    Returns an empty string when no relevant fields are present.
    """
    dist  = race.get("distance_furlongs") or race.get("distance_f")
    surf  = race.get("surface") or race.get("surface_code") or ""
    field = race.get("field_size")
    parts: list[str] = []
    if dist:
        parts.append(f"{dist}f {surf}".strip())
    if field:
        parts.append(f"Field {field}")
    return " · ".join(parts)


# ── Lifecycle status ───────────────────────────────────────────────────────────

_STATUS_LABELS: dict[str, str] = {
    "unscored":         "Not scored",
    "scored_no_result": "Scored · Results pending",
    "calibrated":       "Scored · Results ingested",
}

_STATUS_ICONS: dict[str, str] = {
    "unscored":         "⬜",
    "scored_no_result": "🟡",
    "calibrated":       "🟢",
}


def get_race_workflow_status(race: dict) -> str:
    """Derive lifecycle status from has_score_run / has_results flags.

    Returns one of: 'unscored', 'scored_no_result', 'calibrated'.
    These flags are populated by load_race_index via LEFT JOINs on
    score_runs and race_results.
    """
    if race.get("has_score_run") and race.get("has_results"):
        return "calibrated"
    if race.get("has_score_run"):
        return "scored_no_result"
    return "unscored"


def format_status_badge(race: dict) -> str:
    """Short human-readable status with icon, e.g. '🟡 Scored · Results pending'."""
    status = get_race_workflow_status(race)
    return f"{_STATUS_ICONS[status]} {_STATUS_LABELS[status]}"
