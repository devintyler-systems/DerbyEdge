"""
Regression tests: 1/ST BET detection and runner parsing.

Three layout fixtures cover real-world 1/ST BET export layouts:

  Layout A — spaced (odds inline, no dash):
      "1 PP1 HORSE J: Jockey T: Trainer ML 9/2 {pp_recap}"

  Layout B — spaced with dash separator:
      "1 PP1 HORSE J: Jockey T: Trainer - ML 8"

  Layout C — compact pdfplumber output (no spaces, no colons):
      "1PP1HORSEJ Jockey T Trainer-ML 8"

Top-level tests exercise parse_race_pdf() end-to-end with _extract_text mocked.
"""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from src.services.pdf_ingest import (
    _is_1stbet,
    _parse_race_runners_1stbet_fallback,
    _parse_race_runners_1stbet_multiline,
    parse_race_pdf,
)


# ---------------------------------------------------------------------------
# Layout A fixture — original IND R5 CLM format with RECENT PP recap
# ---------------------------------------------------------------------------
_IND_R5_CLM_TEXT = """\
5/7/26, 12:33 PM 1/ST BET - The Easy & Smart Way to Bet the Races
1/4
Horseshoe Indianapolis R 5
3:45 PM 7 Horses CLM $12,500 6F Dirt / Fast
1 PP1 YOTOWIN J: Luis Quinonez T: Brian Lynch ML 9/2 RECENT 5 (4-1-1-2) 12Apr26 7 PEN 6f
2 PP2 INNISFREE LASS J: Maria Harrington T: Adam Kitchingman ML 5/1 RECENT 5 (0-0-0-5) 5Apr26 1 IND 6f
3 PP3 TAP BONNET J: Josue Perez T: Patricia Farro ML 3/1
4 PP4 AMAZINGNESS J: Chris Landeros T: Ron Alfano ML 6/1
5 PP5 WHAT ABOUT NOW J: Silvio Amador T: Elaine Labadie ML 15/1
6 PP6 KABOOM J: Alex Becerra T: Kathy Ritvo ML 4/1
7 PP7 I MADE IT J: Xavier Perez T: Wayne Catalano ML 8/1
https://legacy.1stbet.com
2/4
"""

# ---------------------------------------------------------------------------
# Layout B fixture — ALW format with dash separator and bare integer ML
# ---------------------------------------------------------------------------
_IND_R5_ALW_TEXT = """\
5/7/26, 12:33 PM 1/ST BET - The Easy & Smart Way to Bet the Races
https://legacy.1stbet.com 1/4
Horseshoe Indianapolis R 5
1:21 PM 7 Horses ALW $38,000 1M Dirt / Fast
1 PP1 YOTOWIN J: Evin A. Roman T: Rogelio Labra - ML 8
2 PP2 INNISFREE LASS J: Alberto Burgos T: John Haran - ML 9/2
3 PP3 TAP BONNET J: Jake Saez T: Eduardo Caramori - ML 5/2
4 PP4 AMAZINGNESS J: Chris Landeros T: Ron Alfano - ML 6
5 PP5 WHAT ABOUT NOW J: Silvio Amador T: Elaine Labadie - ML 4
6 PP6 KABOOM J: Alex Becerra T: Kathy Ritvo - ML 10
7 PP7 I MADE IT J: Xavier Perez T: Wayne Catalano - ML 7/2
https://legacy.1stbet.com 2/4
"""

_EXPECTED_NAMES = {
    "Yotowin",
    "Innisfree Lass",
    "Tap Bonnet",
    "Amazingness",
    "What About Now",
    "Kaboom",
    "I Made It",
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run(text: str) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    runners = _parse_race_runners_1stbet_fallback(text, warnings)
    return runners, warnings


# ===========================================================================
# Layout A tests (original format)
# ===========================================================================

class TestLayoutA:
    def test_runner_count(self):
        runners, _ = _run(_IND_R5_CLM_TEXT)
        assert len(runners) == 7, (
            f"Expected 7, got {len(runners)}: {[r['horse_name'] for r in runners]}"
        )

    def test_runner_names(self):
        runners, _ = _run(_IND_R5_CLM_TEXT)
        assert {r["horse_name"] for r in runners} == _EXPECTED_NAMES

    def test_post_positions_sequential(self):
        runners, _ = _run(_IND_R5_CLM_TEXT)
        assert [r["post_position"] for r in runners] == [1, 2, 3, 4, 5, 6, 7]

    def test_fallback_warning_emitted(self):
        _, warnings = _run(_IND_R5_CLM_TEXT)
        assert any("fallback PP-anchor" in w for w in warnings)

    def test_horse_name_no_recap_pollution(self):
        runners, _ = _run(_IND_R5_CLM_TEXT)
        for r in runners:
            assert "RECENT" not in r["horse_name"]
            assert "Apr" not in r["horse_name"]

    def test_pp_recap_stored(self):
        runners, _ = _run(_IND_R5_CLM_TEXT)
        by_name = {r["horse_name"]: r for r in runners}
        assert by_name["Yotowin"].get("pp_recap"), "Yotowin should have pp_recap"
        assert by_name["Innisfree Lass"].get("pp_recap"), "Innisfree Lass should have pp_recap"
        for name in ("Tap Bonnet", "Amazingness", "What About Now", "Kaboom", "I Made It"):
            assert not by_name[name].get("pp_recap"), f"{name} should have no pp_recap"

    def test_pp_recap_content(self):
        runners, _ = _run(_IND_R5_CLM_TEXT)
        yotowin = next(r for r in runners if r["horse_name"] == "Yotowin")
        assert "RECENT" in yotowin["pp_recap"]
        assert "PEN" in yotowin["pp_recap"]


# ===========================================================================
# Layout B tests (new ALW format with dash separator and integer ML)
# ===========================================================================

class TestLayoutB:
    def test_runner_count(self):
        runners, _ = _run(_IND_R5_ALW_TEXT)
        assert len(runners) == 7, (
            f"Expected 7, got {len(runners)}: {[r['horse_name'] for r in runners]}"
        )

    def test_runner_names(self):
        runners, _ = _run(_IND_R5_ALW_TEXT)
        assert {r["horse_name"] for r in runners} == _EXPECTED_NAMES

    def test_post_positions_sequential(self):
        runners, _ = _run(_IND_R5_ALW_TEXT)
        assert [r["post_position"] for r in runners] == [1, 2, 3, 4, 5, 6, 7]

    def test_fallback_warning_emitted(self):
        _, warnings = _run(_IND_R5_ALW_TEXT)
        assert any("fallback PP-anchor" in w for w in warnings)

    def test_no_runner_failure_warning(self):
        """The 'runners (0 found)' condition must not be reached."""
        runners, _ = _run(_IND_R5_ALW_TEXT)
        assert len(runners) > 0, "Expected >0 runners for Layout B fixture"

    def test_trainer_no_trailing_dash(self):
        """Dash separator before ML must not appear in trainer names."""
        runners, _ = _run(_IND_R5_ALW_TEXT)
        for r in runners:
            t = r.get("trainer") or ""
            assert not t.endswith("-"), f"Trainer has trailing dash: {t!r} ({r['horse_name']})"
            assert not t.endswith("- "), f"Trainer has trailing '- ': {t!r}"

    def test_integer_ml_parsed(self):
        """Bare integer ML (e.g. '8') must normalise to 'N/1' form."""
        runners, _ = _run(_IND_R5_ALW_TEXT)
        by_name = {r["horse_name"]: r for r in runners}
        # PP1 YOTOWIN ML 8 → "8/1"
        assert by_name["Yotowin"]["ml"] == "8/1", (
            f"Expected '8/1', got {by_name['Yotowin']['ml']!r}"
        )
        # PP7 I MADE IT ML 7/2 → "7/2" (fractional preserved)
        assert by_name["I Made It"]["ml"] == "7/2", (
            f"Expected '7/2', got {by_name['I Made It']['ml']!r}"
        )

    def test_fractional_ml_preserved(self):
        runners, _ = _run(_IND_R5_ALW_TEXT)
        by_name = {r["horse_name"]: r for r in runners}
        assert by_name["Innisfree Lass"]["ml"] == "9/2"
        assert by_name["Tap Bonnet"]["ml"] == "5/2"

    def test_no_pp_recap_for_layout_b(self):
        """Layout B runners have no RECENT history bloc — pp_recap should be absent."""
        runners, _ = _run(_IND_R5_ALW_TEXT)
        for r in runners:
            recap = r.get("pp_recap") or ""
            assert "RECENT" not in recap, f"Unexpected RECENT in pp_recap for {r['horse_name']}"

    def test_noise_stripped_from_names(self):
        runners, _ = _run(_IND_R5_ALW_TEXT)
        for r in runners:
            name = r["horse_name"]
            assert "1stbet" not in name.lower(), f"URL in name: {name!r}"
            assert not re.match(r'^\d+/\d+$', name), f"Page counter as name: {name!r}"


# ===========================================================================
# Shared / cross-layout tests
# ===========================================================================

class TestShared:
    def test_all_caps_title_cased_layout_a(self):
        runners, _ = _run(_IND_R5_CLM_TEXT)
        names = {r["horse_name"] for r in runners}
        assert "Yotowin" in names
        assert "What About Now" in names

    def test_all_caps_title_cased_layout_b(self):
        runners, _ = _run(_IND_R5_ALW_TEXT)
        names = {r["horse_name"] for r in runners}
        assert "Yotowin" in names
        assert "What About Now" in names

    def test_mixed_case_preserved(self):
        text = (
            "1 PP1 YOTOWIN J: Jockey1 T: Trainer1 ML 5/2\n"
            "2 PP2 Innisfree Lass J: Jockey2 T: Trainer2 ML 3/1\n"
        )
        runners, _ = _run(text)
        names = {r["horse_name"] for r in runners}
        assert "Yotowin" in names,        "ALL-CAPS name should be title-cased"
        assert "Innisfree Lass" in names, "Mixed-case name should be preserved"

    def test_no_runners_when_no_pp_anchors(self):
        runners, _ = _run("Some race text with no post-position anchors at all.")
        assert runners == []

    def test_ml_key_present_on_all_runners_layout_a(self):
        runners, _ = _run(_IND_R5_CLM_TEXT)
        for r in runners:
            assert "ml" in r, f"Missing 'ml' key: {r}"

    def test_ml_key_present_on_all_runners_layout_b(self):
        runners, _ = _run(_IND_R5_ALW_TEXT)
        for r in runners:
            assert "ml" in r, f"Missing 'ml' key: {r}"

    def test_required_keys_present_layout_a(self):
        runners, _ = _run(_IND_R5_CLM_TEXT)
        required = {"horse_name", "trainer", "jockey", "ml", "post_position"}
        for r in runners:
            assert not (required - r.keys()), (
                f"Runner {r.get('horse_name')} missing: {required - r.keys()}"
            )

    def test_required_keys_present_layout_b(self):
        runners, _ = _run(_IND_R5_ALW_TEXT)
        required = {"horse_name", "trainer", "jockey", "ml", "post_position"}
        for r in runners:
            assert not (required - r.keys()), (
                f"Runner {r.get('horse_name')} missing: {required - r.keys()}"
            )


# ===========================================================================
# Layout C fixture — compact pdfplumber output (real normalized token stream)
# Mirrors the actual extracted text shape: no spaces, no colons after J/T,
# dash immediately before ML, integer or fractional odds.
# ===========================================================================

_IND_R5_COMPACT_TEXT = (
    "HORSESHOE INDIANAPOLISR 5"
    "121 PM7 HorsesALW38,0001MDirt Fast"
    "1PP1YOTOWINJ Evin A. Roman T Rogelio Labra-ML 8"
    "2PP2INNISFREE LASSJ Alberto Burgos T John Haran-ML 9"
    "3PP3TAP BONNETJ Marcelino Pedroza, Jr. T Anthony J. Granitz-ML 5"
    "4PP4AMAZINGNESSJ Joseph D. Ramos T Anthony J. Granitz-ML 6"
    "5PP5WHAT ABOUT NOWJ Mitchell Murrill T Tim Eggleston-ML 4"
    "6PP6KABOOMJ Irving Moncada T Michael E. Lauer-ML 10"
    "7PP7I MADE ITJ Jose Ramos Gutierrez T Stephen V. Fosdick-ML 7"
)


class TestLayoutC:
    def test_runner_count(self):
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        assert len(runners) == 7, (
            f"Expected 7, got {len(runners)}: {[r['horse_name'] for r in runners]}"
        )

    def test_runner_names(self):
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        assert {r["horse_name"] for r in runners} == _EXPECTED_NAMES

    def test_post_positions_sequential(self):
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        assert [r["post_position"] for r in runners] == [1, 2, 3, 4, 5, 6, 7]

    def test_no_duplicate_pp(self):
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        pps = [r["post_position"] for r in runners]
        assert len(pps) == len(set(pps)), f"Duplicate PPs: {pps}"

    def test_jockey_populated(self):
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        by_name = {r["horse_name"]: r for r in runners}
        assert by_name["Yotowin"]["jockey"] == "Evin A. Roman", (
            f"Got: {by_name['Yotowin']['jockey']!r}"
        )
        assert by_name["Innisfree Lass"]["jockey"] == "Alberto Burgos"

    def test_trainer_populated(self):
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        by_name = {r["horse_name"]: r for r in runners}
        assert by_name["Yotowin"]["trainer"] == "Rogelio Labra"
        assert by_name["Innisfree Lass"]["trainer"] == "John Haran"

    def test_trainer_no_trailing_dash(self):
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        for r in runners:
            t = r.get("trainer") or ""
            assert not t.rstrip().endswith("-"), f"Trailing dash in trainer: {t!r}"

    def test_ml_parsed(self):
        """Bare integer ML normalises to N/1; fractional passes through."""
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        by_name = {r["horse_name"]: r for r in runners}
        # ML 8 → "8/1"
        assert by_name["Yotowin"]["ml"] == "8/1", f"Got {by_name['Yotowin']['ml']!r}"
        # ML 9 → "9/1"
        assert by_name["Innisfree Lass"]["ml"] == "9/1"
        # ML 7 → "7/1"
        assert by_name["I Made It"]["ml"] == "7/1"

    def test_ml_key_present(self):
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        for r in runners:
            assert "ml" in r

    def test_required_keys_present(self):
        runners, _ = _run(_IND_R5_COMPACT_TEXT)
        required = {"horse_name", "trainer", "jockey", "ml", "post_position"}
        for r in runners:
            assert not (required - r.keys())


# ===========================================================================
# _is_1stbet detection tests
# Verifies all brand/URL/structural forms are detected; generic PDFs are not.
# ===========================================================================

class TestIsOneStBetDetection:
    def test_standard_brand_text(self):
        assert _is_1stbet("5/7/26, 1:21 PM 1/ST BET - The Easy & Smart Way") is True

    def test_compact_no_slash(self):
        assert _is_1stbet("1ST BET race card IND R5") is True

    def test_compact_no_space_no_slash(self):
        assert _is_1stbet("1STBET race card IND R5") is True

    def test_url_in_footer(self):
        assert _is_1stbet("https://legacy.1stbet.com\n1/4") is True

    def test_bare_url(self):
        assert _is_1stbet("1stbet.com race view") is True

    def test_structural_heuristic_compact_lines(self):
        # No brand text; detected via ≥3 compact NNPPnn tokens on separate lines
        text = (
            "HORSESHOE INDIANAPOLISR 5121 PM7 HorsesALW38,0001MDirt Fast\n"
            "1PP1YOTOWINJ Evin A. Roman T Rogelio Labra-ML 8\n"
            "2PP2INNISFREE LASSJ Alberto Burgos T John Haran-ML 9\n"
            "3PP3TAP BONNETJ Marcelino Pedroza, Jr. T Anthony J. Granitz-ML 5\n"
        )
        assert _is_1stbet(text) is True

    def test_structural_heuristic_needs_three(self):
        # Only 2 compact tokens — below threshold, not detected
        text = "1PP1HORSE A J: Jockey T: Trainer ML 5\n2PP2HORSE B J: Jockey T: Trainer ML 3"
        assert _is_1stbet(text) is False

    def test_generic_equibase_not_detected(self):
        text = (
            "Equibase Company LLC - Official Chart\n"
            "Last Raced Pgm Horse Name\n"
            "1. SECRETARIAT        J. Smith / T. Jones  4-1\n"
            "2. AFFIRMED           J. Brown / T. White  5-2\n"
            "3. SEATTLE SLEW       J. Green / T. Black  3-1\n"
        )
        assert _is_1stbet(text) is False

    def test_brand_beyond_300_chars_still_detected(self):
        # Brand text after the old 300-char cutoff — new limit is 1000 chars
        padding = "A" * 350
        text = padding + " 1/ST BET race data"
        assert _is_1stbet(text) is True


# ===========================================================================
# Top-level parse_race_pdf regression test — compact format, full pipeline
#
# Mocks _extract_text so we can drive parse_race_pdf() with the exact text
# pdfplumber emits for the IND R5 ALW compact PDF (user-confirmed source truth).
# Asserts the full flow: detection → primary (0) → fallback (7) → canonical (7).
# ===========================================================================

# Exact lines extracted by pdfplumber for the compact 1/ST BET PDF.
# Each runner is one compact line; no "1/ST BET" brand text present,
# so _is_1stbet() MUST fire via the structural heuristic.
_IND_R5_COMPACT_RAW_TEXT = (
    "HORSESHOE INDIANAPOLISR 5121 PM7 HorsesALW38,0001MDirt Fast\n"
    "1PP1YOTOWINJ Evin A. Roman T Rogelio Labra-ML 8\n"
    "2PP2INNISFREE LASSJ Alberto Burgos T John Haran-ML 9\n"
    "3PP3TAP BONNETJ Marcelino Pedroza, Jr. T Anthony J. Granitz-ML 5\n"
    "4PP4AMAZINGNESSJ Joseph D. Ramos T Anthony J. Granitz-ML 6\n"
    "5PP5WHAT ABOUT NOWJ Mitchell Murrill T Tim Eggleston-ML 4\n"
    "6PP6KABOOMJ Irving Moncada T Michael E. Lauer-ML 10\n"
    "7PP7I MADE ITJ Jose Ramos Gutierrez T Stephen V. Fosdick-ML 7\n"
)


class TestTopLevelParseRacePdf:
    """Exercises parse_race_pdf() end-to-end — the exact function app.py calls."""

    def _parse(self):
        with patch("src.services.pdf_ingest._extract_text",
                   return_value=_IND_R5_COMPACT_RAW_TEXT):
            return parse_race_pdf(b"fake-pdf-bytes")

    def test_ok(self):
        result = self._parse()
        assert result["ok"] is True, f"parse_race_pdf returned ok=False: {result.get('error')}"

    def test_is_1stbet_true(self):
        result = self._parse()
        assert result["is_1stbet"] is True, "Compact PDF must be detected as 1/ST BET"

    def test_primary_runners_zero(self):
        result = self._parse()
        assert len(result["runners_primary"]) == 0, (
            "Primary parser must find 0 runners in compact format — "
            f"got {len(result['runners_primary'])}"
        )

    def test_fallback_runners_seven(self):
        result = self._parse()
        assert len(result["runners_fallback"]) == 7, (
            f"Fallback must find 7 runners, got {len(result['runners_fallback'])}: "
            f"{[r['horse_name'] for r in result['runners_fallback']]}"
        )

    def test_canonical_runners_seven(self):
        result = self._parse()
        assert len(result["runners"]) == 7, (
            f"Canonical runners must be 7, got {len(result['runners'])}: "
            f"{[r.get('horse_name') for r in result['runners']]}"
        )

    def test_post_positions_one_through_seven(self):
        result = self._parse()
        pps = sorted(r["post_position"] for r in result["runners"])
        assert pps == [1, 2, 3, 4, 5, 6, 7], f"Expected [1..7], got {pps}"

    def test_horse_names(self):
        result = self._parse()
        names = {r["horse_name"] for r in result["runners"]}
        assert names == _EXPECTED_NAMES, f"Name mismatch: {names}"

    def test_pp1_jockey(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        assert by_pp[1]["jockey"] == "Evin A. Roman", (
            f"PP1 jockey: {by_pp[1]['jockey']!r}"
        )

    def test_pp1_trainer(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        assert by_pp[1]["trainer"] == "Rogelio Labra", (
            f"PP1 trainer: {by_pp[1]['trainer']!r}"
        )

    def test_pp1_ml(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        ml = by_pp[1].get("ml") or by_pp[1].get("morning_line")
        assert ml == "8/1", f"PP1 ML: {ml!r}"

    def test_all_runners_have_horse_name(self):
        result = self._parse()
        for r in result["runners"]:
            assert r.get("horse_name"), f"Missing horse_name: {r}"

    def test_all_runners_have_jockey(self):
        result = self._parse()
        for r in result["runners"]:
            assert r.get("jockey"), f"Missing jockey for {r.get('horse_name')}"

    def test_all_runners_have_trainer(self):
        result = self._parse()
        for r in result["runners"]:
            assert r.get("trainer"), f"Missing trainer for {r.get('horse_name')}"

    def test_all_runners_have_ml(self):
        result = self._parse()
        for r in result["runners"]:
            ml = r.get("ml") or r.get("morning_line")
            assert ml, f"Missing ML for {r.get('horse_name')}"

    def test_no_duplicate_post_positions(self):
        result = self._parse()
        pps = [r["post_position"] for r in result["runners"]]
        assert len(pps) == len(set(pps)), f"Duplicate PPs: {pps}"


# ===========================================================================
# Multiline block layout — exact Horseshoe Indianapolis R5 pdfplumber shape
# Each runner is a vertical block: N / PP{N} / HORSE / J: / T: / - / ML odds
# ===========================================================================

_IND_R5_MULTILINE_TEXT = """\
5/7/26, 1:21 PM 1/ST BET - The Easy & Smart Way to Bet the Races
https://legacy.1stbet.com 1/4
HORSESHOE INDIANAPOLIS R 5
1:21 PM 7 Horses ALW $38,000 1M Dirt / Fast
1
PP1
YOTOWIN
J: Evin A. Roman
T: Rogelio Labra
-
ML 8
2
PP2
INNISFREE LASS
J: Alberto Burgos
T: John Haran
-
ML 9/2
3
PP3
TAP BONNET
J: Marcelino Pedroza, Jr.
T: Anthony J. Granitz
-
ML 5
4
PP4
AMAZINGNESS
J: Joseph D. Ramos
T: Anthony J. Granitz
-
ML 6
5
PP5
WHAT ABOUT NOW
J: Mitchell Murrill
T: Tim Eggleston
-
ML 4
6
PP6
KABOOM
J: Irving Moncada
T: Michael E. Lauer
-
ML 10
7
PP7
I MADE IT
J: Jose Ramos Gutierrez
T: Stephen V. Fosdick
-
ML 7/2
"""


class TestLayoutMultiline:
    """Tests _parse_race_runners_1stbet_multiline() directly."""

    def _run(self):
        warnings: list[str] = []
        runners = _parse_race_runners_1stbet_multiline(
            _IND_R5_MULTILINE_TEXT.splitlines(), warnings
        )
        return runners, warnings

    def test_runner_count(self):
        runners, _ = self._run()
        assert len(runners) == 7, (
            f"Expected 7, got {len(runners)}: {[r['horse_name'] for r in runners]}"
        )

    def test_runner_names(self):
        runners, _ = self._run()
        assert {r["horse_name"] for r in runners} == _EXPECTED_NAMES

    def test_post_positions_sequential(self):
        runners, _ = self._run()
        assert [r["post_position"] for r in runners] == [1, 2, 3, 4, 5, 6, 7]

    def test_no_duplicate_pp(self):
        runners, _ = self._run()
        pps = [r["post_position"] for r in runners]
        assert len(pps) == len(set(pps)), f"Duplicate PPs: {pps}"

    def test_pp1_jockey(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[1]["jockey"] == "Evin A. Roman", f"Got: {by_pp[1]['jockey']!r}"

    def test_pp2_jockey(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[2]["jockey"] == "Alberto Burgos"

    def test_pp7_jockey(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[7]["jockey"] == "Jose Ramos Gutierrez"

    def test_pp1_trainer(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[1]["trainer"] == "Rogelio Labra", f"Got: {by_pp[1]['trainer']!r}"

    def test_pp3_trainer(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[3]["trainer"] == "Anthony J. Granitz"

    def test_integer_ml_normalised(self):
        """Bare integer ML (e.g. '8') normalises to 'N/1' form."""
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[1]["morning_line"] == "8/1", f"PP1 ML: {by_pp[1]['morning_line']!r}"
        assert by_pp[3]["morning_line"] == "5/1"
        assert by_pp[4]["morning_line"] == "6/1"
        assert by_pp[5]["morning_line"] == "4/1"
        assert by_pp[6]["morning_line"] == "10/1"

    def test_fractional_ml_preserved(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[2]["morning_line"] == "9/2", f"PP2 ML: {by_pp[2]['morning_line']!r}"
        assert by_pp[7]["morning_line"] == "7/2", f"PP7 ML: {by_pp[7]['morning_line']!r}"

    def test_all_runners_have_required_fields(self):
        runners, _ = self._run()
        required = {"horse_name", "jockey", "trainer", "morning_line", "post_position"}
        for r in runners:
            missing = required - r.keys()
            assert not missing, f"{r.get('horse_name')} missing: {missing}"

    def test_noise_not_treated_as_runner(self):
        """Page headers, URLs, and page counters must not become runner names."""
        runners, _ = self._run()
        names = {r["horse_name"] for r in runners}
        assert "1/St Bet" not in names
        assert not any("1stbet" in n.lower() for n in names)

    def test_no_horse_name_with_j_or_t_prefix(self):
        """J: / T: lines must not leak into horse_name."""
        runners, _ = self._run()
        for r in runners:
            name = r["horse_name"]
            assert not name.startswith("J:"), f"Horse name starts with J:: {name!r}"
            assert not name.startswith("T:"), f"Horse name starts with T:: {name!r}"


# ===========================================================================
# Top-level test: multiline PDF through parse_race_pdf() (the UI entry point)
# ===========================================================================

class TestTopLevelMultilineRacePdf:
    """parse_race_pdf() with the verified Horseshoe multiline pdfplumber text."""

    def _parse(self):
        with patch("src.services.pdf_ingest._extract_text",
                   return_value=_IND_R5_MULTILINE_TEXT):
            return parse_race_pdf(b"fake-pdf-bytes")

    def test_ok(self):
        result = self._parse()
        assert result["ok"] is True, f"ok=False: {result.get('error')}"

    def test_is_1stbet_true(self):
        result = self._parse()
        assert result["is_1stbet"] is True

    def test_canonical_runners_seven(self):
        result = self._parse()
        assert len(result["runners"]) == 7, (
            f"Expected 7 canonical runners, got {len(result['runners'])}: "
            f"{[r.get('horse_name') for r in result['runners']]}"
        )

    def test_post_positions_one_through_seven(self):
        result = self._parse()
        pps = sorted(r["post_position"] for r in result["runners"])
        assert pps == [1, 2, 3, 4, 5, 6, 7]

    def test_horse_names(self):
        result = self._parse()
        names = {r["horse_name"] for r in result["runners"]}
        assert names == _EXPECTED_NAMES, f"Name mismatch: {names}"

    def test_race_number(self):
        result = self._parse()
        assert result["race_number"] == 5, f"race_number: {result['race_number']!r}"

    def test_race_date(self):
        result = self._parse()
        assert result["race_date"] == "2026-05-07", f"race_date: {result['race_date']!r}"

    def test_distance(self):
        result = self._parse()
        assert result["distance_text"] == "1 Mile", f"distance: {result['distance_text']!r}"

    def test_purse(self):
        result = self._parse()
        assert result["purse_usd"] == 38000, f"purse: {result['purse_usd']!r}"

    def test_race_type_allowance(self):
        result = self._parse()
        assert result["race_type"] == "Allowance", f"race_type: {result['race_type']!r}"

    def test_pp1_fields(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        r1 = by_pp[1]
        assert r1["horse_name"] == "Yotowin"
        assert r1["jockey"] == "Evin A. Roman"
        assert r1["trainer"] == "Rogelio Labra"
        assert r1["morning_line"] == "8/1"

    def test_pp7_fields(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        r7 = by_pp[7]
        assert r7["horse_name"] == "I Made It"
        assert r7["jockey"] == "Jose Ramos Gutierrez"
        assert r7["trainer"] == "Stephen V. Fosdick"
        assert r7["morning_line"] == "7/2"

    def test_multiline_parse_won_selection(self):
        """runners_primary (multiline) must be the canonical source for this layout."""
        result = self._parse()
        assert len(result["runners_primary"]) == 7, (
            f"Multiline should find 7, got {len(result['runners_primary'])}"
        )

    def test_no_duplicate_post_positions(self):
        result = self._parse()
        pps = [r["post_position"] for r in result["runners"]]
        assert len(pps) == len(set(pps)), f"Duplicate PPs: {pps}"
