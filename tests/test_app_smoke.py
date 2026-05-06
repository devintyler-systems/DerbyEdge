"""Streamlit app smoke test. Verifies the app renders without exceptions."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
APP = ROOT / "app" / "streamlit_app.py"

# Skip cleanly if streamlit isn't installed in this env or the model file is missing.
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


@pytest.mark.skipif(not _have_model(),
                    reason="No trained model present; run train_baseline first")
def test_app_renders_without_exceptions():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert len(at.exception) == 0, [e.value for e in at.exception]
    # Verify expected UI parts exist
    assert any("Edge Sheet" in t.value for t in at.title)
    assert any("Edge sheet" in s.value for s in at.subheader)
    assert len(at.dataframe) >= 1


@pytest.mark.skipif(not _have_model(),
                    reason="No trained model present; run train_baseline first")
def test_app_chaos_toggle_works():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    toggles = at.toggle
    assert len(toggles) >= 1
    toggles[0].set_value(True).run()
    assert len(at.exception) == 0, [e.value for e in at.exception]
