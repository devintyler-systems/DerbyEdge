"""
Regression tests: 1/ST BET detection and runner parsing.

Four layout fixtures cover real-world 1/ST BET export layouts:

  Layout A — spaced (odds inline, no dash):
      "1 PP1 HORSE J: Jockey T: Trainer ML 9/2 {pp_recap}"

  Layout B — spaced with dash separator:
      "1 PP1 HORSE J: Jockey T: Trainer - ML 8"

  Layout C — compact pdfplumber output (no spaces, no colons):
      "1PP1HORSEJ Jockey T Trainer-ML 8"

  Layout LIVE — line-preserving real production pdfplumber output:
      "HORSE NAME LIVE_ODDS"  (ALL CAPS + trailing odds)
      "N"                     (bare program number)
      "J: Jockey Name ML ml_odds"
      "PP{N}"
      "T: Trainer Name"
      "RECENT 5 WINS ..."

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
# Layout LIVE — real production pdfplumber output (Horseshoe Indianapolis R5)
#
# Block order (confirmed from live PDF):
#   HORSE NAME LIVE_ODDS  ← ALL CAPS + trailing odds token (live board odds)
#   N                     ← bare program number
#   J: Jockey Name ML ml  ← ML on same J: line
#   PP{N}                 ← optional label; skip
#   T: Trainer Name
#   RECENT 5 WINS ...     ← stats noise; ignored
#
# Live odds ≠ ML for PP1 and PP2 — verifies parser uses J:-line ML, not header odds.
# ===========================================================================

_IND_R5_LIVE_TEXT = """\
5/7/26, 4:38 PM 1/ST BET - The Easy & Smart Way to Bet the Races
HORSESHOE INDIANAPOLIS R 5
1:14 PM 7 Horses ALW $38,000 1M Dirt / Fast
FINAL
REPLAY
YOTOWIN 9
1
J: Evin A. Roman ML 8
PP1
T: Rogelio Labra
RECENT 5 WINS 1 TOP 3 3 More Info
INNISFREE LASS 8
2
J: Alberto Burgos ML 9/2
PP2
T: John Haran
RECENT 5 WINS 0 TOP 3 1 More Info
TAP BONNET 5/2
3
J: Jake Saez ML 5/2
PP3
T: Eduardo Caramori
RECENT 5 More Info
AMAZINGNESS 6
4
J: Chris Landeros ML 6
PP4
T: Ron Alfano
RECENT 5 More Info
WHAT ABOUT NOW 4
5
J: Silvio Amador ML 4
PP5
T: Elaine Labadie
RECENT 5 More Info
KABOOM 10
6
J: Alex Becerra ML 10
PP6
T: Kathy Ritvo
RECENT 5 More Info
I MADE IT 7/2
7
J: Xavier Perez ML 7/2
PP7
T: Wayne Catalano
RECENT 5 More Info
"""


class TestLayoutMultiline:
    """Tests _parse_race_runners_1stbet_multiline() against the real production layout."""

    def _run(self):
        warnings: list[str] = []
        runners = _parse_race_runners_1stbet_multiline(
            _IND_R5_LIVE_TEXT.splitlines(), warnings
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
        assert by_pp[7]["jockey"] == "Xavier Perez"

    def test_pp1_trainer(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[1]["trainer"] == "Rogelio Labra", f"Got: {by_pp[1]['trainer']!r}"

    def test_pp3_trainer(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[3]["trainer"] == "Eduardo Caramori"

    def test_ml_from_j_line_not_live_odds(self):
        """ML must come from J: line, not the live-odds token on the horse header."""
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        # PP1 YOTOWIN: live odds=9 → "9/1", J: ML 8 → "8/1" — must use J: value
        assert by_pp[1]["morning_line"] == "8/1", (
            f"Expected '8/1' from J: ML 8, got {by_pp[1]['morning_line']!r} "
            "(parser may be using live-odds token '9' instead)"
        )
        # PP2 INNISFREE LASS: live odds=8 → "8/1", J: ML 9/2 — must use J: value
        assert by_pp[2]["morning_line"] == "9/2", (
            f"Expected '9/2' from J: ML 9/2, got {by_pp[2]['morning_line']!r} "
            "(parser may be using live-odds token '8' instead)"
        )

    def test_integer_ml_normalised(self):
        """Bare integer ML (e.g. '8') normalises to 'N/1' form."""
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[1]["morning_line"] == "8/1",  f"PP1 ML: {by_pp[1]['morning_line']!r}"
        assert by_pp[4]["morning_line"] == "6/1",  f"PP4 ML: {by_pp[4]['morning_line']!r}"
        assert by_pp[5]["morning_line"] == "4/1",  f"PP5 ML: {by_pp[5]['morning_line']!r}"
        assert by_pp[6]["morning_line"] == "10/1", f"PP6 ML: {by_pp[6]['morning_line']!r}"

    def test_fractional_ml_preserved(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[2]["morning_line"] == "9/2", f"PP2 ML: {by_pp[2]['morning_line']!r}"
        assert by_pp[3]["morning_line"] == "5/2", f"PP3 ML: {by_pp[3]['morning_line']!r}"
        assert by_pp[7]["morning_line"] == "7/2", f"PP7 ML: {by_pp[7]['morning_line']!r}"

    def test_all_runners_have_required_fields(self):
        runners, _ = self._run()
        required = {"horse_name", "jockey", "trainer", "morning_line", "post_position"}
        for r in runners:
            missing = required - r.keys()
            assert not missing, f"{r.get('horse_name')} missing: {missing}"

    def test_all_required_fields_populated(self):
        """Every runner must have non-None, non-empty jockey, trainer, and ML."""
        runners, _ = self._run()
        for r in runners:
            assert r.get("jockey"),       f"Missing jockey:  {r.get('horse_name')}"
            assert r.get("trainer"),      f"Missing trainer: {r.get('horse_name')}"
            assert r.get("morning_line"), f"Missing ML:      {r.get('horse_name')}"

    def test_noise_not_treated_as_runner(self):
        """Page headers, URLs, and page counters must not become runner names."""
        runners, _ = self._run()
        names = {r["horse_name"] for r in runners}
        assert "1/St Bet" not in names
        assert not any("1stbet" in n.lower() for n in names)
        assert "Final" not in names
        assert "Replay" not in names

    def test_no_horse_name_with_j_or_t_prefix(self):
        """J: / T: lines must not leak into horse_name."""
        runners, _ = self._run()
        for r in runners:
            name = r["horse_name"]
            assert not name.startswith("J:"), f"Horse name starts with J:: {name!r}"
            assert not name.startswith("T:"), f"Horse name starts with T:: {name!r}"

    def test_recent_not_in_names(self):
        """RECENT stat lines must not appear in horse names."""
        runners, _ = self._run()
        for r in runners:
            assert "Recent" not in r["horse_name"], (
                f"RECENT leaked into horse name: {r['horse_name']!r}"
            )

    def test_horseshoe_header_not_a_runner(self):
        """'HORSESHOE INDIANAPOLIS R 5' must be rejected (next line is race-info, not bare int)."""
        runners, _ = self._run()
        names = {r["horse_name"] for r in runners}
        assert not any("Horseshoe" in n for n in names), (
            f"Track header accepted as runner: {names}"
        )


# ===========================================================================
# Top-level test: live-layout PDF through parse_race_pdf() (the UI entry point)
# ===========================================================================

class TestTopLevelMultilineRacePdf:
    """parse_race_pdf() with the verified Horseshoe live-layout pdfplumber text."""

    def _parse(self):
        with patch("src.services.pdf_ingest._extract_text",
                   return_value=_IND_R5_LIVE_TEXT):
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

    def test_primary_runners_seven(self):
        """runners_primary (multiline horse-header parser) must find all 7."""
        result = self._parse()
        assert len(result["runners_primary"]) == 7, (
            f"Multiline primary must find 7, got {len(result['runners_primary'])}: "
            f"{[r.get('horse_name') for r in result['runners_primary']]}"
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
        assert r7["jockey"] == "Xavier Perez"
        assert r7["trainer"] == "Wayne Catalano"
        assert r7["morning_line"] == "7/2"

    def test_multiline_parse_won_selection(self):
        """runners_primary (multiline) must be canonical for this line-preserving layout."""
        result = self._parse()
        assert len(result["runners_primary"]) == 7, (
            f"Multiline should find 7, got {len(result['runners_primary'])}"
        )

    def test_no_duplicate_post_positions(self):
        result = self._parse()
        pps = [r["post_position"] for r in result["runners"]]
        assert len(pps) == len(set(pps)), f"Duplicate PPs: {pps}"

    def test_all_runners_have_jockey(self):
        result = self._parse()
        for r in result["runners"]:
            assert r.get("jockey"), f"Missing jockey for {r.get('horse_name')}"

    def test_all_runners_have_trainer(self):
        result = self._parse()
        for r in result["runners"]:
            assert r.get("trainer"), f"Missing trainer for {r.get('horse_name')}"

    def test_all_runners_have_morning_line(self):
        result = self._parse()
        for r in result["runners"]:
            assert r.get("morning_line"), f"Missing morning_line for {r.get('horse_name')}"


# ===========================================================================
# Live-layout regression — exact real raw_text shape from parse_race_pdf()
#
# Verifies the specific assertions requested:
#   is_1stbet == True
#   runners_primary == 7   (horse-header multiline parser)
#   runners_fallback can be 0  (compact parser finds nothing in this layout)
#   canonical runners == 7
#   each runner: horse_name, jockey, trainer, morning_line
# ===========================================================================

_IND_R5_LIVE_RAW_TEXT = """\
5/7/26, 4:38 PM 1/ST BET - The Easy & Smart Way to Bet the Races
HORSESHOE INDIANAPOLIS R 5
1:14 PM 7 Horses ALW $38,000 1M Dirt / Fast
FINAL
REPLAY
YOTOWIN 9
1
J: Evin A. Roman ML 8
PP1
T: Rogelio Labra
RECENT 5 WINS 1 TOP 3 3 More Info
INNISFREE LASS 8
2
J: Alberto Burgos ML 9/2
PP2
T: John Haran
RECENT 5 WINS 0 TOP 3 1 More Info
TAP BONNET 5/2
3
J: Jake Saez ML 5/2
PP3
T: Eduardo Caramori
RECENT 5 More Info
AMAZINGNESS 6
4
J: Chris Landeros ML 6
PP4
T: Ron Alfano
RECENT 5 More Info
WHAT ABOUT NOW 4
5
J: Silvio Amador ML 4
PP5
T: Elaine Labadie
RECENT 5 More Info
KABOOM 10
6
J: Alex Becerra ML 10
PP6
T: Kathy Ritvo
RECENT 5 More Info
I MADE IT 7/2
7
J: Xavier Perez ML 7/2
PP7
T: Wayne Catalano
RECENT 5 More Info
"""


class TestLiveLayoutRegression:
    """Regression for the real production pdfplumber layout (horse-header → bare int → J:/PP/T)."""

    def _parse(self):
        with patch("src.services.pdf_ingest._extract_text",
                   return_value=_IND_R5_LIVE_RAW_TEXT):
            return parse_race_pdf(b"fake-pdf-bytes")

    def test_is_1stbet_true(self):
        result = self._parse()
        assert result["is_1stbet"] is True, "Live PDF must be detected as 1/ST BET"

    def test_runners_primary_seven(self):
        result = self._parse()
        assert len(result["runners_primary"]) == 7, (
            f"Horse-header multiline parser must yield 7, "
            f"got {len(result['runners_primary'])}: "
            f"{[r.get('horse_name') for r in result['runners_primary']]}"
        )

    def test_runners_fallback_zero_or_fewer(self):
        """Compact PP-anchor parser should find 0 runners in this line-preserving layout."""
        result = self._parse()
        # Compact parser is expected to return 0; if it finds some, that is not a failure
        # but the primary parser must dominate (already verified by runners_primary == 7).
        assert len(result["runners_fallback"]) < len(result["runners_primary"]), (
            "Compact fallback must not outpace the primary parser for live-layout PDFs"
        )

    def test_canonical_runners_seven(self):
        result = self._parse()
        assert len(result["runners"]) == 7, (
            f"Expected 7 canonical runners, got {len(result['runners'])}: "
            f"{[r.get('horse_name') for r in result['runners']]}"
        )

    def test_post_positions_one_through_seven(self):
        result = self._parse()
        pps = sorted(r["post_position"] for r in result["runners"])
        assert pps == [1, 2, 3, 4, 5, 6, 7], f"Expected [1..7], got {pps}"

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

    def test_all_runners_have_morning_line(self):
        result = self._parse()
        for r in result["runners"]:
            assert r.get("morning_line"), f"Missing morning_line for {r.get('horse_name')}"

    def test_pp1_complete(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        r1 = by_pp[1]
        assert r1["horse_name"] == "Yotowin"
        assert r1["jockey"] == "Evin A. Roman"
        assert r1["trainer"] == "Rogelio Labra"
        assert r1["morning_line"] == "8/1", (
            f"ML should be '8/1' from J:-line ML 8, got {r1['morning_line']!r}"
        )

    def test_pp2_complete(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        r2 = by_pp[2]
        assert r2["horse_name"] == "Innisfree Lass"
        assert r2["jockey"] == "Alberto Burgos"
        assert r2["trainer"] == "John Haran"
        assert r2["morning_line"] == "9/2", (
            f"ML should be '9/2' from J:-line ML 9/2, got {r2['morning_line']!r}"
        )

    def test_pp7_complete(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        r7 = by_pp[7]
        assert r7["horse_name"] == "I Made It"
        assert r7["jockey"] == "Xavier Perez"
        assert r7["trainer"] == "Wayne Catalano"
        assert r7["morning_line"] == "7/2"

    def test_ml_source_j_line_not_live_odds(self):
        """Critical: ML from J: line must win over live-odds token on horse header."""
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        # YOTOWIN: live_odds=9 (→ 9/1 if used), J: ML=8 (→ 8/1)
        assert by_pp[1]["morning_line"] == "8/1", (
            f"YOTOWIN ML: expected '8/1' from J: line, got {by_pp[1]['morning_line']!r}"
        )
        # INNISFREE LASS: live_odds=8 (→ 8/1 if used), J: ML=9/2
        assert by_pp[2]["morning_line"] == "9/2", (
            f"INNISFREE LASS ML: expected '9/2' from J: line, got {by_pp[2]['morning_line']!r}"
        )

    def test_no_duplicate_post_positions(self):
        result = self._parse()
        pps = [r["post_position"] for r in result["runners"]]
        assert len(pps) == len(set(pps)), f"Duplicate PPs: {pps}"

    def test_horse_names_title_cased(self):
        result = self._parse()
        names = {r["horse_name"] for r in result["runners"]}
        assert "YOTOWIN" not in names, "Horse names must be title-cased, not ALL CAPS"
        assert "Yotowin" in names
        assert "Innisfree Lass" in names
        assert "I Made It" in names


# ===========================================================================
# Race 8 regression — mix of active runners (no trailing token) and
# scratched runners (SCR token).  Exposes the bug where _candidate_horse
# rejected any line without a trailing odds/SCR token, silently discarding
# all 8 active runners and returning only the 2 scratches.
#
# Active runners:  SCRIPTED LOVE, BEAUTIFUL ESTER, PINK PICTURE, MIDNIGHT BLUE,
#                  SUMMER BREEZE, GOLDEN ARROW, STAR DANCER, WILD FIRE  (8)
# Scratched:       WHAT YOU WISHED SCR (PP4), ONCE MORE SCR (PP9)       (2)
# Total:           10
# ===========================================================================

_IND_R8_LIVE_TEXT = """\
5/7/26, 5:02 PM 1/ST BET - The Easy & Smart Way to Bet the Races
HORSESHOE INDIANAPOLIS R 8
2:15 PM 10 Horses MCL $12,500 1M Dirt / Fast
FINAL
REPLAY
SCRIPTED LOVE
1
J: Luis Quinonez ML 5/2
PP1
T: Brian Lynch
RECENT 5 WINS 1 TOP 3 2 More Info
BEAUTIFUL ESTER
2
J: Maria Harrington ML 3/1
PP2
T: Adam Kitchingman
RECENT 5 More Info
PINK PICTURE
3
J: Josue Perez ML 8
PP3
T: Patricia Farro
RECENT 5 More Info
WHAT YOU WISHED SCR
4
J: Chris Landeros ML 6
PP4
T: Ron Alfano
RECENT 5 More Info
MIDNIGHT BLUE
5
J: Silvio Amador ML 4
PP5
T: Elaine Labadie
RECENT 5 More Info
SUMMER BREEZE
6
J: Alex Becerra ML 10
PP6
T: Kathy Ritvo
RECENT 5 More Info
GOLDEN ARROW
7
J: Xavier Perez ML 15
PP7
T: Wayne Catalano
RECENT 5 More Info
STAR DANCER
8
J: Evin A. Roman ML 5
PP8
T: Rogelio Labra
RECENT 5 More Info
ONCE MORE SCR
9
J: Alberto Burgos ML 20
PP9
T: John Haran
RECENT 5 More Info
WILD FIRE
10
J: Jake Saez ML 7
PP10
T: Eduardo Caramori
RECENT 5 More Info
"""

_R8_ACTIVE_NAMES = {
    "Scripted Love", "Beautiful Ester", "Pink Picture", "Midnight Blue",
    "Summer Breeze", "Golden Arrow", "Star Dancer", "Wild Fire",
}
_R8_SCRATCHED_NAMES = {"What You Wished", "Once More"}
_R8_ALL_NAMES = _R8_ACTIVE_NAMES | _R8_SCRATCHED_NAMES


class TestLiveLayoutRace8Direct:
    """Direct test of _parse_race_runners_1stbet_multiline with Race 8 fixture.

    Critical regression: active runners have NO trailing token on the horse-name
    line.  Previous parser rejected them, returning only the 2 scratched runners.
    """

    def _run(self):
        warnings: list[str] = []
        runners = _parse_race_runners_1stbet_multiline(
            _IND_R8_LIVE_TEXT.splitlines(), warnings
        )
        return runners, warnings

    def test_total_runner_count(self):
        runners, _ = self._run()
        assert len(runners) == 10, (
            f"Expected 10 runners (8 active + 2 scratched), "
            f"got {len(runners)}: {[r['horse_name'] for r in runners]}"
        )

    def test_active_runner_count(self):
        runners, _ = self._run()
        active = [r for r in runners if not r["is_scratched"]]
        assert len(active) == 8, (
            f"Expected 8 active runners, got {len(active)}: "
            f"{[r['horse_name'] for r in active]}"
        )

    def test_scratched_runner_count(self):
        runners, _ = self._run()
        scratched = [r for r in runners if r["is_scratched"]]
        assert len(scratched) == 2, (
            f"Expected 2 scratched, got {len(scratched)}: "
            f"{[r['horse_name'] for r in scratched]}"
        )

    def test_all_names_found(self):
        runners, _ = self._run()
        names = {r["horse_name"] for r in runners}
        assert names == _R8_ALL_NAMES, (
            f"Missing: {_R8_ALL_NAMES - names}  Extra: {names - _R8_ALL_NAMES}"
        )

    def test_active_names_correct(self):
        runners, _ = self._run()
        active_names = {r["horse_name"] for r in runners if not r["is_scratched"]}
        assert active_names == _R8_ACTIVE_NAMES, (
            f"Active name mismatch — missing: {_R8_ACTIVE_NAMES - active_names}"
        )

    def test_scratched_names_correct(self):
        runners, _ = self._run()
        scr_names = {r["horse_name"] for r in runners if r["is_scratched"]}
        assert scr_names == _R8_SCRATCHED_NAMES, (
            f"Scratched name mismatch: {scr_names}"
        )

    def test_post_positions_one_through_ten(self):
        runners, _ = self._run()
        pps = sorted(r["post_position"] for r in runners)
        assert pps == list(range(1, 11)), f"Expected [1..10], got {pps}"

    def test_scratched_at_pp4_and_pp9(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[4]["is_scratched"] is True,  "PP4 must be scratched"
        assert by_pp[9]["is_scratched"] is True,  "PP9 must be scratched"
        assert by_pp[4]["horse_name"] == "What You Wished"
        assert by_pp[9]["horse_name"] == "Once More"

    def test_active_runners_not_scratched(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        for pp in [1, 2, 3, 5, 6, 7, 8, 10]:
            assert by_pp[pp]["is_scratched"] is False, (
                f"PP{pp} must not be scratched"
            )

    def test_pp1_fields(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        r1 = by_pp[1]
        assert r1["horse_name"] == "Scripted Love"
        assert r1["jockey"] == "Luis Quinonez"
        assert r1["trainer"] == "Brian Lynch"
        assert r1["morning_line"] == "5/2"
        assert r1["is_scratched"] is False

    def test_pp2_fields(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        r2 = by_pp[2]
        assert r2["horse_name"] == "Beautiful Ester"
        assert r2["jockey"] == "Maria Harrington"
        assert r2["trainer"] == "Adam Kitchingman"
        assert r2["morning_line"] == "3/1"

    def test_pp10_fields(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        r10 = by_pp[10]
        assert r10["horse_name"] == "Wild Fire"
        assert r10["jockey"] == "Jake Saez"
        assert r10["trainer"] == "Eduardo Caramori"
        assert r10["morning_line"] == "7/1"

    def test_all_active_have_jockey_trainer_ml(self):
        runners, _ = self._run()
        for r in runners:
            if r["is_scratched"]:
                continue
            assert r.get("jockey"),       f"Missing jockey:  {r['horse_name']}"
            assert r.get("trainer"),      f"Missing trainer: {r['horse_name']}"
            assert r.get("morning_line"), f"Missing ML:      {r['horse_name']}"

    def test_no_duplicate_pp(self):
        runners, _ = self._run()
        pps = [r["post_position"] for r in runners]
        assert len(pps) == len(set(pps)), f"Duplicate PPs: {pps}"

    def test_horse_names_title_cased(self):
        runners, _ = self._run()
        for r in runners:
            assert r["horse_name"] != r["horse_name"].upper(), (
                f"Horse name not title-cased: {r['horse_name']!r}"
            )


class TestLiveLayoutRace8Pipeline:
    """End-to-end parse_race_pdf() with Race 8 fixture — the full production path."""

    def _parse(self):
        with patch("src.services.pdf_ingest._extract_text",
                   return_value=_IND_R8_LIVE_TEXT):
            return parse_race_pdf(b"fake-pdf-bytes")

    def test_ok(self):
        result = self._parse()
        assert result["ok"] is True, f"ok=False: {result.get('error')}"

    def test_is_1stbet_true(self):
        result = self._parse()
        assert result["is_1stbet"] is True

    def test_canonical_runners_ten(self):
        result = self._parse()
        assert len(result["runners"]) == 10, (
            f"Expected 10 canonical runners, got {len(result['runners'])}: "
            f"{[r.get('horse_name') for r in result['runners']]}"
        )

    def test_primary_runners_ten(self):
        result = self._parse()
        assert len(result["runners_primary"]) == 10, (
            f"Primary must find 10, got {len(result['runners_primary'])}"
        )

    def test_all_names_found(self):
        result = self._parse()
        names = {r["horse_name"] for r in result["runners"]}
        assert names == _R8_ALL_NAMES, (
            f"Missing: {_R8_ALL_NAMES - names}  Extra: {names - _R8_ALL_NAMES}"
        )

    def test_pp4_scratched(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        assert by_pp[4]["is_scratched"] is True
        assert by_pp[4]["horse_name"] == "What You Wished"

    def test_pp9_scratched(self):
        result = self._parse()
        by_pp = {r["post_position"]: r for r in result["runners"]}
        assert by_pp[9]["is_scratched"] is True
        assert by_pp[9]["horse_name"] == "Once More"

    def test_race_number(self):
        result = self._parse()
        assert result["race_number"] == 8, f"race_number: {result['race_number']!r}"

    def test_field_size(self):
        result = self._parse()
        assert result["field_size"] == 10, f"field_size: {result['field_size']!r}"


# ===========================================================================
# Trailing-dash stripping regression
#
# pdfplumber can merge a standalone "-" separator line into the preceding
# horse-name line when the two sit at very close y-coordinates, producing
# e.g. "SCRIPTED LOVE-" instead of "SCRIPTED LOVE".  The hyphen is legal
# in the ALL-CAPS name regex ([A-Z0-9'\\s\\-\\.]+), so without stripping
# the horse name comes out as "Scripted Love-".
#
# This fixture simulates that exact pdfplumber output (trailing "-" fused to
# horse names, and a T:/−/ML layout where the dash also appears standalone).
# After the fix, all horse_name values must be clean (no trailing hyphens).
# ===========================================================================

_TRAILING_DASH_TEXT = """\
5/7/26, 5:02 PM 1/ST BET - The Easy & Smart Way to Bet the Races
HORSESHOE INDIANAPOLIS R 8
2:15 PM 3 Horses MCL $12,500 6F Dirt / Fast
FINAL
REPLAY
SCRIPTED LOVE-
1
J: Luis Quinonez
PP1
T: Paul Mcentee
-
ML 10
BEAUTIFUL ESTER-
2
J: Maria Harrington
PP2
T: Adam Kitchingman
-
ML 5/2
PINK PICTURE-
3
J: Josue Perez
PP3
T: Patricia Farro
-
ML 3/1
"""


class TestTrailingDashStripping:
    """Regression: pdfplumber-merged trailing dash must be stripped from horse names."""

    def _run(self):
        warnings: list[str] = []
        runners = _parse_race_runners_1stbet_multiline(
            _TRAILING_DASH_TEXT.splitlines(), warnings
        )
        return runners, warnings

    def test_runner_count(self):
        runners, _ = self._run()
        assert len(runners) == 3, (
            f"Expected 3, got {len(runners)}: {[r['horse_name'] for r in runners]}"
        )

    def test_no_trailing_dash_in_names(self):
        """Core regression: horse names must not end with a hyphen."""
        runners, _ = self._run()
        for r in runners:
            assert not r["horse_name"].endswith("-"), (
                f"Trailing dash in horse_name: {r['horse_name']!r} (pp={r['post_position']})"
            )

    def test_pp1_horse_name(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[1]["horse_name"] == "Scripted Love", (
            f"Expected 'Scripted Love', got {by_pp[1]['horse_name']!r}"
        )

    def test_pp2_horse_name(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[2]["horse_name"] == "Beautiful Ester", (
            f"Expected 'Beautiful Ester', got {by_pp[2]['horse_name']!r}"
        )

    def test_pp3_horse_name(self):
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[3]["horse_name"] == "Pink Picture", (
            f"Expected 'Pink Picture', got {by_pp[3]['horse_name']!r}"
        )

    def test_pp1_trainer(self):
        """T:/−/ML layout: trainer must be extracted correctly."""
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[1]["trainer"] == "Paul Mcentee", (
            f"Got: {by_pp[1]['trainer']!r}"
        )

    def test_ml_from_standalone_line(self):
        """ML on its own line (after T: and −) must still be extracted."""
        runners, _ = self._run()
        by_pp = {r["post_position"]: r for r in runners}
        assert by_pp[1]["morning_line"] == "10/1", (
            f"PP1 ML: {by_pp[1]['morning_line']!r}"
        )
        assert by_pp[2]["morning_line"] == "5/2", (
            f"PP2 ML: {by_pp[2]['morning_line']!r}"
        )
        assert by_pp[3]["morning_line"] == "3/1", (
            f"PP3 ML: {by_pp[3]['morning_line']!r}"
        )
