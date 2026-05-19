"""
DerbyEdge — Policy layer
src/models/policy.py

Lightweight segment-based operational defaults for tier and chaos selection.
Config-driven and fully reversible; does not affect model training.
"""
from __future__ import annotations

FIELD_SIZE_BUCKET_LABELS: list[str] = [
    "Full (13+)",
    "Large (10-12)",
    "Medium (7-9)",
    "Small (<=6)",
]


def bucket_field_size(field_size: int | None) -> str:
    """Map a live field size to the canonical bucket label."""
    if field_size is None:
        return "Unknown"
    try:
        n = int(field_size)
    except (ValueError, TypeError):
        return "Unknown"
    if n >= 13:
        return "Full (13+)"
    if n >= 10:
        return "Large (10-12)"
    if n >= 7:
        return "Medium (7-9)"
    return "Small (<=6)"


def normalize_dist_category(dist_category: str | None) -> str:
    """Return 'route', 'sprint', or the raw lowercase value."""
    if dist_category is None:
        return "unknown"
    val = str(dist_category).strip().lower()
    if val in ("route", "routes"):
        return "route"
    if val in ("sprint", "sprints"):
        return "sprint"
    return val


def normalize_surface(surface: str | None) -> str:
    """Return 'D', 'T', or the original uppercase token."""
    if surface is None:
        return "Unknown"
    val = str(surface).strip().upper()
    if val in ("D", "DIRT"):
        return "D"
    if val in ("T", "TURF", "GRASS"):
        return "T"
    return val


# ---------------------------------------------------------------------------
# Tier override rules — keyed by (surface, dist_category, field_bucket).
# Only high-confidence dirt sprint segments are active.
# ---------------------------------------------------------------------------
TIER_OVERRIDE_RULES: dict[tuple[str, str, str], str] = {
    ("D", "sprint", "Small (<=6)"):   "enriched_proxy",
    ("D", "sprint", "Medium (7-9)"):  "enriched_proxy",
    ("D", "sprint", "Large (10-12)"): "enriched_proxy",
}

# ---------------------------------------------------------------------------
# Chaos defaults — keyed by (surface, dist_category, field_bucket).
# One active rule: dirt sprint small-field -> chaos off.
# ---------------------------------------------------------------------------
CHAOS_DEFAULTS: dict[tuple[str, str, str], bool] = {
    ("D", "sprint", "Small (<=6)"): False,
    # TODO: ("T", "route",  "Large (10-12)"): True,  # activate when turf sample >= 10
    # TODO: ("T", "sprint", "Full (13+)"):    True,  # activate when turf sprint sample >= 10
}


def choose_tier(
    surface: str | None,
    dist_category: str | None,
    field_size: int | None,
    default_tier: str = "enriched_proxy",
) -> tuple[str, str]:
    """Return (chosen_tier, policy_reason).

    Looks up (normalized_surface, normalized_dist, field_bucket) in
    TIER_OVERRIDE_RULES.  Falls back to default_tier when no rule matches.
    """
    surf   = normalize_surface(surface)
    dist   = normalize_dist_category(dist_category)
    bucket = bucket_field_size(field_size)
    key = (surf, dist, bucket)
    if key in TIER_OVERRIDE_RULES:
        return TIER_OVERRIDE_RULES[key], f"segment_override:{surf}/{dist}/{bucket}"
    return default_tier, "default_tier"


def default_chaos(
    surface: str | None,
    dist_category: str | None,
    field_size: int | None,
    default_value: bool = False,
) -> tuple[bool, str]:
    """Return (chaos_default, policy_reason).

    Looks up (normalized_surface, normalized_dist, field_bucket) in
    CHAOS_DEFAULTS.  Falls back to default_value when no rule matches.
    """
    surf   = normalize_surface(surface)
    dist   = normalize_dist_category(dist_category)
    bucket = bucket_field_size(field_size)
    key = (surf, dist, bucket)
    if key in CHAOS_DEFAULTS:
        return CHAOS_DEFAULTS[key], f"segment_override:{surf}/{dist}/{bucket}"
    return default_value, "default_chaos"
