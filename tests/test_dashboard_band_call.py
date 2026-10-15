"""The dashboard's cap buttons run on the MCP side (rook/band_mcp/dashboard_call.py).

* ``POST /internal/band-call`` on the MCP app: internal token, a forwarded
  ``human:*`` principal only, the call made as that person, reply masked and
  journaled;
* the dashboard's ``/api/band/call`` forwards there, and falls back to a direct
  band call for workers (never for the hub's own caps) when the MCP side
  can't be reached.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette
from starlette.testclient import TestClient as StarletteClient

from rook.band_mcp import dashboard_call, secret_mask
from rook.remote import upstream
from rook.remote.bootstrap import CombinedServer

TOKEN = "t" * 64
PASSWORD = "test-password-1"
OWNER = json.dumps({"id": "human:dashboard", "role": "owner", "label": "dashboard"})


class _FakeClient:
    def __init__(self, reply=None, exc=None):
        self.workers = {"w1": {"name": "nas", "caps": ["shell.exec"]},
                        "hub1": {"name": "rook", "roles": ["is_hub"], "caps": ["updates.check"]}}
        self.calls = []
        self.reply = reply if reply is not None else {"ok": True, "result": "hello s3cr3t-value"}
        self.exc = exc

    async def call(self, **kw):
        from rook.hub.authz import current_principal
        self.calls.append({**kw, "context_principal": current_principal.get()})
        if self.exc:
            raise self.exc
        return dict(self.reply)


class _FakeJournal:
    def __init__(self):
        self.rows = []

    def record(self, **kw):
        self.rows.append(kw)
        return "j1"


class _Masker:
    def mask(self, obj):
        return json.loads(json.dumps(obj).replace("s3cr3t-value", "{{secret:demo}}"))


def _mcp(client, journal):
    return StarletteClient(Starlette(routes=[
        dashboard_call.route(TOKEN, lambda: client, lambda: journal)]))


def _post(http, body, token=TOKEN, principal=OWNER):
    headers = {"Authorization": f"Bearer {token}"}
    if principal is not None:
        headers["X-Rook-Principal"] = principal
    return http.post(dashboard_call.PATH, json=body, headers=headers)


def test_needs_the_internal_token_and_a_person():
    client, journal = _FakeClient(), _FakeJournal()
    http = _mcp(client, journal)
    assert _post(http, {"cap": "shell.exec"}, token="wrong").status_code == 401
    assert _post(http, {"cap": "shell.exec"}, principal=None).status_code == 403
    agent = json.dumps({"id": "token:agent_x", "role": "owner"})
    assert _post(http, {"cap": "shell.exec"}, principal=agent).status_code == 403
    system = json.dumps({"id": "system:hub", "role": "owner"})
    assert _post(http, {"cap": "shell.exec"}, principal=system).status_code == 403
    assert _post(http, {"args": {}}).status_code == 400
    assert client.calls == [] and journal.rows == []


def test_runs_as_the_person_by_name_masks_and_journals(monkeypatch):
    monkeypatch.setattr(secret_mask, "installed", lambda: _Masker())
    client, journal = _FakeClient(), _FakeJournal()
    r = _post(_mcp(client, journal), {"cap": "shell.exec", "target": "nas",
                                      "args": {"cmd": "echo hi"}, "timeout": 20,
                                      "identity": "dashboard"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "hello {{secret:demo}}" and body["journal_id"] == "j1"
    call = client.calls[0]
    assert call["target"] == "w1" and call["args"] == {"cmd": "echo hi"}
    assert call["principal"].id == "human:dashboard" and call["principal"].kind == "human"
    assert call["principal"].fail_open()  # an owner
    assert call["context_principal"].id == "human:dashboard"
    assert call["timeout"] == 20
    row = journal.rows[0]
    assert row["cap"] == "shell.exec" and row["worker"] == "nas" and row["identity"] == "dashboard"
    assert "s3cr3t-value" not in json.dumps(row["reply"])


def test_member_stays_a_member_and_timeouts_are_reported():
    client, journal = _FakeClient(exc=asyncio.TimeoutError()), _FakeJournal()
    member = json.dumps({"id": "human:u1", "role": "member", "label": "alex"})
    r = _post(_mcp(client, journal), {"cap": "shell.exec", "target": "w1"}, principal=member)
    assert r.status_code == 504 and r.json()["timeout"] is True
    assert not client.calls[0]["principal"].fail_open()
    assert journal.rows[0]["reply"]["timeout"] is True


# -- the dashboard side ----------------------------------------------------------

@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    token_file = tmp_path / "mask.token"
    token_file.write_text(TOKEN)
    monkeypatch.setenv("ROOK_MASK_TOKEN_FILE", str(token_file))
    from rook.remote import setup_store
    setup_store.save({"band_name": "b", "band_psk": "alpha-bravo-charlie-delta-echo",
                      "hub_public": "hub.example.com:443", "pyz_domain": "hub.example.com"})
    server = CombinedServer(web_user="owner", web_pass=PASSWORD)
    band = _FakeClient(reply={"ok": True, "result": "direct"})
    server._band = band
    yield server, band
    if server._chat:
        server._chat.close()


def _basic():
    return {"Authorization": "Basic " + base64.b64encode(f"owner:{PASSWORD}".encode()).decode()}


async def _call(server, body):
    async with TestClient(TestServer(server._app)) as http:
        r = await http.post("/api/band/call", json=body, headers=_basic())
        return r.status, await r.json()


@pytest.mark.asyncio
async def test_dashboard_forwards_to_the_mcp_side_as_the_person(dashboard, monkeypatch):
    server, band = dashboard
    seen = {}

    async def fake_request(method, url, json=None, timeout=None, headers=None):
        seen.update(method=method, url=url, json=json, headers=headers, timeout=timeout)
        return 200, {"ok": True, "result": "via mcp", "journal_id": "j9"}
    monkeypatch.setattr(upstream, "request_json", fake_request)
    status, reply = await _call(server, {"cap": "updates.check", "worker_id": "rook",
                                         "timeout": 20})
    assert status == 200 and reply["result"] == "via mcp"
    assert band.calls == []  # nothing went out on the band from the dashboard
    assert seen["url"].endswith("/internal/band-call")
    assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
    who = json.loads(seen["headers"]["X-Rook-Principal"])
    assert who["id"] == "human:dashboard" and who["role"] == "owner"
    assert seen["json"]["cap"] == "updates.check" and seen["json"]["target"] == "hub1"
    assert seen["timeout"] > 20


@pytest.mark.asyncio
async def test_unreachable_mcp_falls_back_for_workers_not_for_the_hub(dashboard, monkeypatch):
    server, band = dashboard

    async def down(*a, **kw):
        raise upstream.Unavailable("connection refused")
    monkeypatch.setattr(upstream, "request_json", down)
    status, reply = await _call(server, {"cap": "shell.exec", "worker_id": "nas",
                                         "args": {"cmd": "true"}})
    assert status == 200 and reply["result"] == "direct"
    assert band.calls[0]["target"] == "w1"
    status, reply = await _call(server, {"cap": "updates.check", "worker_id": "rook"})
    assert status == 503 and "MCP server isn't reachable" in reply["error"]
    assert len(band.calls) == 1


@pytest.mark.asyncio
async def test_older_mcp_without_the_route_falls_back(dashboard, monkeypatch):
    server, band = dashboard

    async def missing(*a, **kw):
        return 404, {"error": "not found"}
    monkeypatch.setattr(upstream, "request_json", missing)
    status, reply = await _call(server, {"cap": "shell.exec", "worker_id": "w1"})
    assert status == 200 and reply["result"] == "direct"


@pytest.mark.asyncio
async def test_slow_mcp_is_not_retried_over_the_band(dashboard, monkeypatch):
    # The MCP side was reached but didn't answer in time: the call may be
    # running there, so the dashboard must not send it a second time.
    server, band = dashboard
    monkeypatch.setattr(CombinedServer, "FORWARD_FAST_FAIL", -1.0)

    async def slow(*a, **kw):
        raise upstream.Unavailable("timed out")
    monkeypatch.setattr(upstream, "request_json", slow)
    status, reply = await _call(server, {"cap": "shell.exec", "worker_id": "w1"})
    assert status == 504 and "may still be running" in reply["error"]
    assert band.calls == []
