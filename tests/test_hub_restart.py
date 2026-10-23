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


@pytest.mark.asyncio
async def test_the_settings_api_restarts_for_the_operator_only(monkeypatch):
    from rook.hub.settings_web import routes
    app = Starlette(routes=routes(lambda: SimpleNamespace(), _Accounts()))
    api, body = "/settings/account-api", {"csrf": "k", "action": "restart"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(api, json=body, headers={"Cookie": "rook_account=m1"})
        assert r.status_code == 403
        r = await c.post(api, json=body, headers={"Cookie": "rook_account=op"})
        assert r.status_code == 400 and "can't restart itself" in r.json()["error"]
        stopped = []
        restart.on_restart(lambda: stopped.append(1))
        monkeypatch.setattr(restart, "supervised", lambda: "a test reason")
        r = await c.post(api, json=body, headers={"Cookie": "rook_account=op"})
        assert r.status_code == 400 and r.json()["error"] == "Not restarted: a test reason."
        assert not restart.requested()
        monkeypatch.setattr(restart, "supervised", lambda: None)
        r = await c.post(api, json=body, headers={"Cookie": "rook_account=op"})
        assert r.status_code == 200 and r.json()["ok"] and restart.requested()
    await asyncio.sleep(0.2)
    assert stopped == [1]


@pytest.mark.asyncio
async def test_a_separate_dashboard_restarts_after_the_mcp_side_agrees(monkeypatch):
    from rook.remote.knowledge_web import SettingsWeb
    seen = []

    async def upstream(request):
        data = await request.json()
        seen.append(data["action"])
        return web.json_response({"ok": True} if data["action"] == "restart" else {"error": "no"},
                                 status=200 if data["action"] == "restart" else 400)
    up = web.Application()
    up.router.add_route("*", "/settings/account-api", upstream)
    user = {"id": "op", "username": "op", "admin": True, "csrf": "k"}
    account = SimpleNamespace(require=lambda r: user, csrf=lambda r, d, u: None)
    stopped = []
    restart.on_restart(lambda: stopped.append(1))
    async with TestServer(up) as ts:
        sw = SettingsWeb(account)
        sw.url = str(ts.make_url("/settings/account-api"))
        app = web.Application()
        sw.install(app)
        async with TestClient(TestServer(app)) as client:
            # The dashboard can't come back: refused before the MCP side hears of it.
            monkeypatch.setattr(restart, "supervised", lambda: "not under systemd")
            r = await client.post("/account/settings/api", json={"csrf": "k", "action": "restart"})
            assert r.status == 400 and "the dashboard" in (await r.json())["error"] and seen == []
            monkeypatch.setattr(restart, "supervised", lambda: None)
            # Other actions pass through untouched.
            r = await client.post("/account/settings/api", json={"csrf": "k", "action": "set"})
            assert r.status == 400 and not restart.requested()
            r = await client.post("/account/settings/api", json={"csrf": "k", "action": "restart"})
            assert r.status == 200 and restart.requested()
            # A non-operator's request is the MCP side's to refuse; nothing restarts here.
            restart._requested = False
            user["admin"] = False
            await client.post("/account/settings/api", json={"csrf": "k", "action": "restart"})
            assert not restart.requested()
    assert seen == ["set", "restart", "restart"]
    await asyncio.sleep(0.2)
    assert stopped == [1]


def test_the_settings_page_has_a_restart_button_and_waits_for_the_hub():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "rook/web/settings-area.js").read_text()
    assert "if(admin)bar.append(btn('Restart','st-restart',confirmRestart" in js
    assert "post({action:'restart'})" in js and "location.reload()" in js
