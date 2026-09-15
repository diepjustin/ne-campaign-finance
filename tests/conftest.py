"""Pytest config: make scripts/ importable as plain modules, no package."""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _no_real_legacy_csv(monkeypatch, tmp_path):
    """build_site.py's LEGACY_CONTRIBUTIONS defaults to the real
    data/processed/contributions_legacy.csv -- 253,553 rows on this machine.
    Without this, any build_search_index() test that doesn't set its own
    legacy path would silently read the whole real file every run: slow, and
    a real violation of "tests run with no network and no data/". A test that
    actually needs legacy rows overrides this with its own monkeypatch.setattr
    after this fixture runs, in the same test function's shared monkeypatch.
    """
    try:
        import build_site
    except ImportError:
        return
    monkeypatch.setattr(build_site, "LEGACY_CONTRIBUTIONS", tmp_path / "no-such-legacy.csv")
