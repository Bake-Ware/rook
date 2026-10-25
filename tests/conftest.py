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


@pytest.fixture(autouse=True)
def _approvals_off_unless_asked(request, monkeypatch):
    """Approval holds (rook/hub/approvals.py) are on by default and make an
    agent's admin or destructive call wait up to 45 s for a person. Tests
    that are not about them run with them off, as the hub did before; tests
    marked ``approvals`` (tests/test_approvals.py) get the shipped default."""
    if request.node.get_closest_marker("approvals") is None:
        monkeypatch.setenv("ROOK_APPROVALS", "0")
    else:
        monkeypatch.delenv("ROOK_APPROVALS", raising=False)
    # Advanced security (rook/hub/plugins/security) is off by default; tests of
    # the guardrails it carries are marked ``advanced``.
    if request.node.get_closest_marker("advanced") is None:
        monkeypatch.delenv("ROOK_SECURITY_ADVANCED", raising=False)
    else:
        monkeypatch.setenv("ROOK_SECURITY_ADVANCED", "1")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "approvals: run with approval holds on (the shipped default); every other test "
        "runs with ROOK_APPROVALS=0")
    config.addinivalue_line(
        "markers",
        "advanced: run with the advanced security plugin on (blanket holds, guarded trust, "
        "votes, client relays, the shell read-only guess)")
    config.addinivalue_line(
        "markers",
        "integration: needs a running test hub (scripts/test-hub.sh); opt-in "
        "with ROOK_IT=1 or -m integration, skipped otherwise")
