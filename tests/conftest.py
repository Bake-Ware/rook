"""Make `telesthete` (the sibling reference-lib checkout) importable in tests,
matching how build_band_worker.py / stage_worker.py vendor it into bundles."""

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TELESTHETE = os.path.join(os.path.dirname(_REPO), "telesthete")

for p in (_REPO, _TELESTHETE):
    if p not in sys.path:
        sys.path.insert(0, p)


import pytest


@pytest.fixture(autouse=True)
def _isolated_hub_keys(tmp_path, monkeypatch):
    """Never let a test read or create the real hub signing keys (the root
    OTA key and the permissions op key beside it)."""
    if "ROOK_UPDATE_KEY" not in os.environ:
        monkeypatch.setenv("ROOK_UPDATE_KEY", str(tmp_path / "keys" / "update-signing-key"))
    # A hub node started by a test runs the updates plugin: keep it off
    # GitHub and out of the real data directory.
    if "ROOK_UPDATES_DIR" not in os.environ:
        monkeypatch.setenv("ROOK_UPDATES_DIR", str(tmp_path / "updates"))
    if "ROOK_UPDATES_MODE" not in os.environ:
        monkeypatch.setenv("ROOK_UPDATES_MODE", "off")
    # updates.check ignores the mode: point it at a closed local port too.
    if "ROOK_UPDATES_API" not in os.environ:
        monkeypatch.setenv("ROOK_UPDATES_API", "http://127.0.0.1:9")


@pytest.fixture(autouse=True)
def _fresh_login_throttle():
    """Test clients all come from 127.0.0.1: one test's failed logins must
    not lock the next one out (rook.core.throttle)."""
    from rook.core import throttle
    throttle.shared().clear()
    yield
    throttle.shared().clear()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: needs a running test hub (scripts/test-hub.sh); opt-in "
        "with ROOK_IT=1 or -m integration, skipped otherwise")
