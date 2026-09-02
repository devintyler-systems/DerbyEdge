"""Tests for distance parsing across all ingestion paths.

Covers the "4 1/2F" bug: the substring "2F" was matched before "4 1/2F"
because the generic digit+F regex runs left-to-right and finds "2F" first.
"""
import pytest
from src.utils.distance_parser import parse_furlongs
from src.services.race_card_builder import parse_distance_yards
from src.services.pdf_ingest import _extract_distance


# ── parse_furlongs (shared util) ──────────────────────────────────────────────

class TestParseFurlongs:
    def test_fractional_with_trailing_condition(self):
        assert parse_furlongs("4 1/2F Dirt / Fast") == 4.5

    def test_fractional_decimal_notation(self):
        assert parse_furlongs("4.5F Dirt/Fast") == 4.5

    def test_fractional_no_trailing(self):
        assert parse_furlongs("4 1/2F") == 4.5

    def test_unicode_half(self):
        assert parse_furlongs("4½F") == 4.5

    def test_six_furlongs(self):
        assert parse_furlongs("6F") == 6.0

    def test_six_furlongs_with_surface(self):
        assert parse_furlongs("6F Dirt") == 6.0

    def test_decimal_furlongs_string(self):
        assert parse_furlongs("6.5 Furlongs") == 6.5

    def test_one_mile(self):
        assert parse_furlongs("1M") == 8.0

    def test_one_mile_spelled(self):
        assert parse_furlongs("1 Mile") == 8.0

    def test_fractional_miles(self):
        assert parse_furlongs("1 1/16 Miles") == pytest.approx(8.5)

    def test_returns_none_for_empty(self):
        assert parse_furlongs("") is None

    def test_returns_none_for_none(self):
        assert parse_furlongs(None) is None

    def test_returns_none_for_garbage(self):
        assert parse_furlongs("scratch") is None


# ── parse_distance_yards (race_card_builder) ──────────────────────────────────

class TestParseDistanceYards:
    def test_four_and_half_furlongs_fraction(self):
        assert parse_distance_yards("4 1/2F") == 990        # 4.5 * 220

    def test_four_and_half_furlongs_with_surface(self):
        assert parse_distance_yards("4 1/2F Dirt / Fast") == 990

    def test_four_point_five_furlongs(self):
        assert parse_distance_yards("4.5F") == 990

    def test_unicode_half_furlongs(self):
        assert parse_distance_yards("4½F") == 990

    def test_six_furlongs(self):
        assert parse_distance_yards("6f") == 1320

    def test_six_furlongs_normalized(self):
        assert parse_distance_yards("6 Furlongs") == 1320

    def test_one_mile(self):
        assert parse_distance_yards("1 Mile") == 1760

    def test_one_and_sixteenth_miles(self):
        assert parse_distance_yards("1 1/16 Miles") == 1870  # 8.5 * 220


# ── _extract_distance (pdf_ingest text normalizer) ────────────────────────────

class TestExtractDistance:
    def test_four_and_half_fraction_notation(self):
        assert _extract_distance("4 1/2F Dirt / Fast") == "4.5 Furlongs"

    def test_four_and_half_unicode(self):
        assert _extract_distance("4½F Dirt / Fast") == "4.5 Furlongs"

    def test_four_point_five(self):
        assert _extract_distance("4.5F Dirt/Fast") == "4.5 Furlongs"

    def test_six_furlongs(self):
        assert _extract_distance("6F Dirt") == "6 Furlongs"

    def test_does_not_extract_2f_from_4_half(self):
        result = _extract_distance("4 1/2F Dirt / Fast")
        assert result != "2 Furlongs", "Bug: regex matched '2F' inside '4 1/2F'"

    def test_ct_r9_race_card_string(self):
        # Charles Town R9 2026-05-07 — source text seen in the race card
        result = _extract_distance("4 1/2F Dirt / Fast")
        assert result == "4.5 Furlongs"


# ── UI display integration (format_race_hint) ─────────────────────────────────

class TestUIDisplay:
    def test_ct_r9_displays_correct_distance(self):
        from src.services.race_display import format_race_hint
        race = {
            "track_abbrev":      "CT",
            "race_number":       9,
            "card_date":         "2026-05-07",
            "surface":           "dirt",
            "distance_furlongs": 4.5,
        }
        hint = format_race_hint(race)
        assert "4.5f" in hint
        assert "2.0f" not in hint
