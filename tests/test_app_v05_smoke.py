"""v0.5 smoke tests — Kelly column, bet-tag filter, CSV export, calibration plot."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
APP = ROOT / "app" / "streamlit_app.py"

streamlit = pytest.importorskip("streamlit", reason="streamlit not installed")


@pytest.fixture(autouse=True)
def _cwd():
    prev = os.getcwd()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "src"))
    yield
    os.chdir(prev)


def _have_model() -> bool:
    return (ROOT / "models" / "baseline_v0.3.pkl").exists() or \
           (ROOT / "models" / "baseline_v0.2.pkl").exists()


@pytest.mark.skipif(not _have_model(), reason="No trained model present")
def test_kelly_controls_render():
    """Bankroll number_input + Kelly cap slider exist in the sidebar."""
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert len(at.exception) == 0, [e.value for e in at.exception]

    # number_input for bankroll
    bankroll_inputs = [n for n in at.number_input
                       if "Bankroll" in (n.label or "")]
    assert len(bankroll_inputs) == 1
    assert bankroll_inputs[0].value == 1000

    # slider for max-Kelly cap
    kelly_sliders = [s for s in at.slider
                     if "Kelly" in (s.label or "")]
    assert len(kelly_sliders) >= 1


@pytest.mark.skipif(not _have_model(), reason="No trained model present")
def test_bet_tag_filter_present():
    """Multiselect filter exists with STRONG+BET defaults."""
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert len(at.exception) == 0, [e.value for e in at.exception]

    multi = [m for m in at.multiselect if "tag" in (m.label or "").lower()]
    assert len(multi) == 1
    assert set(multi[0].value) == {"STRONG", "BET"}


@pytest.mark.skipif(not _have_model(), reason="No trained model present")
def test_csv_export_present_in_source():
    """App source wires st.download_button for CSV export.

    Streamlit 1.57's AppTest does not introspect download_button as a
    first-class element, so we assert on the source + verify the app
    still runs without exception.
    """
    src = APP.read_text(encoding="utf-8")
    assert "st.download_button(" in src
    assert "text/csv" in src

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert len(at.exception) == 0, [e.value for e in at.exception]


@pytest.mark.skipif(
    not (ROOT / "data" / "processed" / "oof_predictions.parquet").exists(),
    reason="oof_predictions.parquet missing; rerun train_baseline.py",
)
def test_oof_predictions_schema():
    """OOF parquet has the columns the calibration plot needs."""
    import pandas as pd
    df = pd.read_parquet(ROOT / "data" / "processed" / "oof_predictions.parquet")
    assert "y_true" in df.columns
    assert "y_pred" in df.columns
    assert len(df) > 0
    assert df["y_pred"].between(0, 1).all()
    assert set(df["y_true"].unique()).issubset({0, 1})


def test_kelly_capped_to_user_max():
    """Kelly fraction respects custom cap, not just default 5%."""
    from derbyedge.odds_math import kelly_fraction
    # Strong edge that would normally produce >10% raw Kelly
    raw = kelly_fraction(0.50, 4.0, cap=1.0)  # uncapped
    assert raw > 0.10
    capped_5 = kelly_fraction(0.50, 4.0, cap=0.05)
    assert capped_5 == pytest.approx(0.05, abs=1e-6)
    capped_10 = kelly_fraction(0.50, 4.0, cap=0.10)
    assert capped_10 == pytest.approx(0.10, abs=1e-6)
    # Negative-edge bet returns 0
    assert kelly_fraction(0.10, 4.0, cap=0.05) == 0.0


def test_race_day_template_csv_parses():
    """Sample race-day CSV is parseable by the manual adapter."""
    from derbyedge.odds_ingest import adapter_manual_csv
    template = ROOT / "samples" / "race_day_template.csv"
    assert template.exists(), f"Missing {template}"
    recs = adapter_manual_csv(template, conn=None)
    assert len(recs) > 0
    # Mix of books present
    books = {r.book_id for r in recs}
    assert "fanduel" in books
    assert "morningline" in books
    # All have a race_id and program_number
    for r in recs:
        assert r.race_id
        assert r.program_number
