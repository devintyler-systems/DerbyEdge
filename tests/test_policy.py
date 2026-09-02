"""Smoke tests for src.models.policy."""
from __future__ import annotations

import pytest

from src.models.policy import bucket_field_size, choose_tier, default_chaos


# ---------------------------------------------------------------------------
# bucket_field_size
# ---------------------------------------------------------------------------

def test_bucket_small():
    assert bucket_field_size(5) == "Small (<=6)"


def test_bucket_medium():
    assert bucket_field_size(8) == "Medium (7-9)"


def test_bucket_large():
    assert bucket_field_size(11) == "Large (10-12)"


def test_bucket_full():
    assert bucket_field_size(14) == "Full (13+)"


def test_bucket_boundaries():
    assert bucket_field_size(6)  == "Small (<=6)"
    assert bucket_field_size(7)  == "Medium (7-9)"
    assert bucket_field_size(9)  == "Medium (7-9)"
    assert bucket_field_size(10) == "Large (10-12)"
    assert bucket_field_size(12) == "Large (10-12)"
    assert bucket_field_size(13) == "Full (13+)"


def test_bucket_none():
    assert bucket_field_size(None) == "Unknown"


# ---------------------------------------------------------------------------
# choose_tier
# ---------------------------------------------------------------------------

def test_choose_tier_dirt_sprint_small_override():
    tier, reason = choose_tier("D", "Sprint", 6)
    assert tier == "enriched_proxy"
    assert "segment_override" in reason


def test_choose_tier_dirt_sprint_medium_override():
    tier, reason = choose_tier("dirt", "sprint", 8)
    assert tier == "enriched_proxy"
    assert "segment_override" in reason


def test_choose_tier_dirt_sprint_large_override():
    tier, reason = choose_tier("D", "sprint", 11)
    assert tier == "enriched_proxy"
    assert "segment_override" in reason


def test_choose_tier_turf_route_default():
    tier, reason = choose_tier("T", "Route", 10)
    assert tier == "enriched_proxy"
    assert reason == "default_tier"


def test_choose_tier_unknown_surface_default():
    tier, reason = choose_tier(None, "sprint", 5)
    assert reason == "default_tier"


# ---------------------------------------------------------------------------
# default_chaos
# ---------------------------------------------------------------------------

def test_chaos_dirt_sprint_small_override():
    chaos, reason = default_chaos("D", "Sprint", 6)
    assert chaos is False
    assert "segment_override" in reason


def test_chaos_turf_route_default():
    chaos, reason = default_chaos("T", "Route", 10)
    assert chaos is False
    assert reason == "default_chaos"


def test_chaos_dirt_sprint_medium_default():
    chaos, reason = default_chaos("D", "sprint", 8)
    assert chaos is False
    assert reason == "default_chaos"
