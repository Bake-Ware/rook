"""Fixtures for the Castling 0.5 end-to-end tests (harness.py has the
pieces; docs/castling-e2e.md says how to run them). Every test has a hard
timeout (SIGALRM, ``E2E_TIMEOUT`` seconds) and every process is killed at the
end whatever happened; process logs are printed when a test fails."""
from __future__ import annotations

import signal

import pytest

if not hasattr(signal, "SIGALRM"):        # Windows: no SIGALRM or process groups
    collect_ignore_glob = ["test_*.py"]
else:
    from harness import E2E_TIMEOUT, Band


@pytest.fixture(autouse=True)
def _hard_timeout():
    """A hung test fails instead of blocking the run."""
    def expire(*_):
        raise TimeoutError(f"e2e test took longer than {E2E_TIMEOUT}s")
    old = signal.signal(signal.SIGALRM, expire)
    signal.alarm(E2E_TIMEOUT)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _dump_logs(band: Band) -> str:
    out = []
    for p in band.procs:
        out.append(f"--- {p.name} (pid {p.pid}, rc {p.p.returncode}) ---")
        out.extend(p.lines[-60:])
    return "\n".join(out)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    band = getattr(item, "_e2e_band", None)
    if band is not None and rep.when == "call" and rep.failed:
        rep.sections.append(("e2e process logs", _dump_logs(band)))


@pytest.fixture
def band(request, tmp_path):
    """A running relay + hub + worker; the worker's caps are listed by the hub."""
    b = Band(tmp_path)
    request.node._e2e_band = b
    try:
        yield b.up()
    finally:
        b.close()


@pytest.fixture
def fresh(request, tmp_path):
    """A written but not started band (the test starts what it needs)."""
    b = Band(tmp_path)
    request.node._e2e_band = b
    b.write()
    try:
        yield b
    finally:
        b.close()


