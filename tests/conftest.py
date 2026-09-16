"""Pytest config: make scripts/ importable as plain modules, no package."""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _no_real_legacy_csv(monkeypatch, tmp_path):
    """build_site.py's LEGACY_CONTRIBUTIONS/EXPENDITURES/LEGACY_EXPENDITURES
    default to real files -- 253,553 / 88,905 / 125,809 rows on this machine.
    Without this, any test that doesn't set its own path would silently read
    the whole real file every run: slow, and a real violation of "tests run
    with no network and no data/" (this bit once already -- a live pytest run
    went from under a second to hanging past four minutes). A test that
    actually needs real-shaped rows overrides one of these paths with its own
    monkeypatch.setattr after this fixture runs, in the same test function's
    shared monkeypatch.
    """
    try:
        import build_site
    except ImportError:
        return
    for attr in (
        "LEGACY_CONTRIBUTIONS", "EXPENDITURES", "LEGACY_EXPENDITURES",
        "LEGACY_FINANCIAL_INTERESTS",
    ):
        if hasattr(build_site, attr):
            monkeypatch.setattr(build_site, attr, tmp_path / f"no-such-{attr.lower()}.csv")
