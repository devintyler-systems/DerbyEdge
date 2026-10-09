"""The CSV templates the app offers for download live in samples/, which is mostly gitignored: they must be tracked."""
from __future__ import annotations

import csv
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "src" / "app" / "app.py").read_text(encoding="utf-8")


def _referenced() -> set[str]:
    return set(re.findall(r'ROOT\s*/\s*"samples"\s*/\s*"([^"]+)"', APP))


def test_every_samples_file_the_app_reads_exists():
    names = _referenced()
    assert {"new_race_odds_template.csv", "results_import_template.csv"} <= names
    missing = sorted(n for n in names if not (ROOT / "samples" / n).is_file())
    assert not missing, f"app.py reads samples/{missing} but the files are missing (gitignored or deleted?)"


def test_the_templates_are_tracked_by_git_not_just_present_locally():
    for name in _referenced():
        ignored = subprocess.run(["git", "check-ignore", "-q", f"samples/{name}"], cwd=ROOT).returncode == 0
        assert not ignored, f"samples/{name} is gitignored, so a fresh clone would not have it"


def test_templates_have_the_columns_the_importers_document():
    with open(ROOT / "samples" / "new_race_odds_template.csv", newline="", encoding="utf-8") as f:
        odds = next(csv.reader(f))
    with open(ROOT / "samples" / "results_import_template.csv", newline="", encoding="utf-8") as f:
        results = next(csv.reader(f))
    assert {"track_code", "race_date", "race_number", "horse_name", "morning_line", "is_scratched"} <= set(odds)
    assert {"race_date", "track_code", "race_number", "horse_name", "finish_position", "scratched"} <= set(results)
