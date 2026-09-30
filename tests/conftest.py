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


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: needs a running test hub (scripts/test-hub.sh); opt-in "
        "with ROOK_IT=1 or -m integration, skipped otherwise")
