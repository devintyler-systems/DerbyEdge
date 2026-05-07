"""
Regression tests: 1/ST BET fallback PP-anchor runner parser.

Three fixtures cover the real-world 1/ST BET export layouts:

  Layout A — spaced (odds inline, no dash):
      "1 PP1 HORSE J: Jockey T: Trainer ML 9/2 {pp_recap}"

  Layout B — spaced with dash separator:
      "1 PP1 HORSE J: Jockey T: Trainer - ML 8"

  Layout C — compact pdfplumber output (no spaces, no colons):
      "1PP1HORSEJ Jockey T Trainer-ML 8"
"""
from __future__ import annotations

import re

import pytest

from src.services.pdf_ingest import _parse_race_runners_1stbet_fallback


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
