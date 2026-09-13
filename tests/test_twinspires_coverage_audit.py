"""The coverage report is diagnostic-only and cannot change runtime validity."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "db" / "derbyedge.db"


def test_card_73_coverage_audit_is_db_read_only_and_remains_invalid():
    if not DB.is_file():
        pytest.skip("requires local production db/derbyedge.db, not present in CI")
    before = hashlib.sha256(DB.read_bytes()).hexdigest()
    output_dir = ROOT / "output" / "acceptance"
    completed = subprocess.run([sys.executable, "scripts/audit_source_coverage.py", "--card-id", "73", "--source-provider", "twinspires", "--output-dir", str(output_dir)], cwd=ROOT, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert hashlib.sha256(DB.read_bytes()).hexdigest() == before
    summary = json.loads((output_dir / "twinspires_card_73_summary.json").read_text(encoding="utf-8"))
    assert summary["read_only"] is True
    assert summary["score_valid"] is False
    assert summary["fair_odds_valid"] is False
    assert summary["market_comparison_valid"] is False
    assert summary["wager_valid"] is False
