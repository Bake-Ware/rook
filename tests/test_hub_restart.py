"""Settings > Restart: the hub process stops and its service manager starts it
again (rook/core/restart.py), through the MCP side's settings API and, when the
dashboard is its own process, the dashboard's proxy too."""
import asyncio
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette

from rook.core import restart


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(restart, "_stoppers", [])
    monkeypatch.setattr(restart, "_requested", False)
    monkeypatch.setattr(restart, "DELAY", 0.05)
    monkeypatch.delenv("ROOK_RESTART_SUPERVISED", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)


def _systemctl(monkeypatch, policy, seen):
    def run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(stdout=policy + "\n", returncode=0)
    monkeypatch.setattr(subprocess, "run", run)


def test_only_a_supervisor_that_restarts_it_counts(monkeypatch):
    assert "not run by systemd" in restart.supervised()
    monkeypatch.setenv("ROOK_RESTART_SUPERVISED", "1")       # vouched for (Docker, runit)
    assert restart.supervised() is None
    monkeypatch.delenv("ROOK_RESTART_SUPERVISED")
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setattr(restart, "_unit", lambda: ("rook-hub.service", False))
    seen = []
    for policy in ("always", "on-failure"):
        _systemctl(monkeypatch, policy, seen)
        assert restart.supervised() is None
    assert seen[0] == ["systemctl", "show", "rook-hub.service", "--property=Restart", "--value"]
    _systemctl(monkeypatch, "no", seen)
    assert "Restart=no" in restart.supervised()
    monkeypatch.setattr(restart, "_unit", lambda: ("rook-hub.service", True))
    _systemctl(monkeypatch, "on-failure", seen)
    assert restart.supervised() is None and seen[-1][:2] == ["systemctl", "--user"]
    monkeypatch.setattr(restart, "_unit", lambda: None)
    assert "which systemd unit" in restart.supervised()


@pytest.mark.asyncio
async def test_a_request_stops_the_process_after_the_reply():
    with pytest.raises(RuntimeError):
        restart.request()
    stopped = []
    unregister = restart.on_restart(lambda: stopped.append(1))
    assert restart.available() and restart.exit_code() == 0
    restart.request("human:op")
    assert stopped == [] and restart.requested()            # the reply goes out first
    await asyncio.sleep(0.2)
    assert stopped == [1] and restart.exit_code() == restart.EXIT_CODE
    unregister()
    assert not restart.available()


class _Accounts:
    def session(self, token):
        return {"id": token, "username": token, "admin": token == "op", "csrf": "k"} if token else None


