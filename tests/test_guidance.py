"""Agent guidance: delivered at connect, in tool listings and on matching
rook_call replies; editable by the operator; never able to break a call."""
import json
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from rook.band_mcp import guidance as guidance_mod
from rook.band_mcp.guidance_web import routes
from rook.band_mcp.server import build_server

STATIC = "static-token-0123456789abcdef"


class FakeBand:
    def __init__(self):
        self.workers = {
            "w1": {"worker_id": "w1", "name": "WIN11-DESKTOP", "band": "x",
                   "caps": ["shell.exec", "info.host"], "last_seen": 0},
            "w2": {"worker_id": "w2", "name": "gpu-box", "band": "x",
                   "caps": ["shell.exec", "proc.start", "info.host"], "last_seen": 0},
        }

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        return {"id": "c", "from": target, "ok": True, "result": {}}


def body(r):
    if r.headers["content-type"].startswith("application/json"):
        return r.json()
    return json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith("data:")))


@asynccontextmanager
async def server(tmp_path):
    mcp, store = build_server(FakeBand(), public_url="https://mcp.example.com",
                              persist_path=str(tmp_path / "tokens.json"), static_token=STATIC,
                              journal_path=str(tmp_path / "journal.db"))
    guidance, reapply = mcp._rook_guidance
    accounts = SimpleNamespace(session=lambda c: {"id": "u1", "username": "operator", "csrf": "k",
                                                  "admin": c == "admin"} if c else None)
    app = mcp.streamable_http_app()
    for route in routes(guidance, reapply, lambda: list(mcp._tool_manager._tools), accounts):
        app.router.routes.insert(0, route)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Accept": "application/json, text/event-stream"}) as http:
        async def connect():
            h = {"Authorization": "Bearer " + STATIC}
            r = await http.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
            h["mcp-session-id"] = r.headers["mcp-session-id"]
            await http.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

            async def rpc(method, params):
                return body(await http.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))["result"]
            return body(r)["result"], rpc
        yield SimpleNamespace(http=http, connect=connect, guidance=guidance)


def reply(res):
    return json.loads(res["content"][0]["text"])


@pytest.mark.asyncio
async def test_defaults_reach_agents_at_each_placement(tmp_path):
    async with server(tmp_path) as env:
        init, rpc = await env.connect()
        assert "rook_workers (who)" in init["instructions"] and "hint=true" in init["instructions"]
        tools = {t["name"]: t["description"] for t in (await rpc("tools/list", {}))["tools"]}
        assert "worker= (name or id) is required" in tools["rook_call"] and "hint=true" in tools["rook_call"]
        assert "Tip:" not in tools["rook_call"]  # default tool tips are empty; essentials are in the description
        first = reply(await rpc("tools/call", {"name": "rook_call", "arguments": {"cap": "shell.exec", "worker": "WIN11-DESKTOP"}}))
        assert first["_tips"] == [guidance_mod.DEFAULTS["cap:shell.exec"]]  # cap tip only, no host tips
        again = reply(await rpc("tools/call", {"name": "rook_call", "arguments": {"cap": "shell.exec", "worker": "WIN11-DESKTOP"}}))
        assert "_tips" not in again and "_hint" not in again  # once per session, no repeat line
        forced = reply(await rpc("tools/call", {"name": "rook_call", "arguments": {"cap": "shell.exec", "worker": "WIN11-DESKTOP", "hint": True}}))
        assert forced["_tips"] == [guidance_mod.DEFAULTS["cap:shell.exec"]] and "_hint" not in forced
        plain = reply(await rpc("tools/call", {"name": "rook_call", "arguments": {"cap": "info.host", "worker": "gpu-box"}}))
        assert "_tips" not in plain and "_hint" not in plain  # caps without a tip get neither
        proc = reply(await rpc("tools/call", {"name": "rook_call", "arguments": {"cap": "proc.start", "worker": "gpu-box"}}))
        assert proc["_tips"] == [guidance_mod.DEFAULTS["cap:proc."]]
        _, rpc2 = await env.connect()
        fresh = reply(await rpc2("tools/call", {"name": "rook_call", "arguments": {"cap": "shell.exec", "worker": "WIN11-DESKTOP"}}))
        assert len(fresh["_tips"]) == 1  # new session sees it again


@pytest.mark.asyncio
async def test_operator_edits_apply_live_with_history_and_reset(tmp_path):
    async with server(tmp_path) as env:
        api = "/guidance/account-api"
        assert (await env.http.get(api)).status_code == 401
        assert (await env.http.get(api, headers={"Cookie": "rook_account=member"})).status_code == 403
        admin = {"Cookie": "rook_account=admin"}
        listing = (await env.http.get(api, headers=admin)).json()
        assert listing["editable"] and "rook_call" in listing["tools"]
        keys = {s["key"] for s in listing["slots"]}
        assert keys >= {"server", "tool:rook_call", "cap:proc."}
        assert not any(k.startswith("worker:") for k in keys)
        assert (await env.http.post(api, headers=admin, json={"action": "set", "key": "server", "text": "x"})).status_code == 403
        for bad in ({"key": "tool:nope"}, {"key": "bogus"}, {"key": "worker:gpu-box"}, {"key": "cap:x", "text": "y" * 1001}):
            r = await env.http.post(api, headers=admin, json={"csrf": "k", "action": "set", "text": "t"} | bad)
            assert r.status_code == 400, bad
        ok = await env.http.post(api, headers=admin, json={"csrf": "k", "action": "set", "key": "server", "text": "Be brief."})
        assert ok.status_code == 200
        await env.http.post(api, headers=admin, json={"csrf": "k", "action": "set", "key": "tool:rook_caps", "text": ""})
        await env.http.post(api, headers=admin, json={"csrf": "k", "action": "set", "key": "cap:info.", "text": "Cheap; safe to call first."})
        init, rpc = await env.connect()
        assert init["instructions"] == "Be brief.\n\n" + guidance_mod.DEFAULTS["contact"]
        tools = {t["name"]: t["description"] for t in (await rpc("tools/list", {}))["tools"]}
        assert "Tip:" not in tools["rook_caps"]  # empty text disables the tip
        res = reply(await rpc("tools/call", {"name": "rook_call", "arguments": {"cap": "info.host", "worker": "gpu-box"}}))
        assert res["_tips"] == ["Cheap; safe to call first."]
        hist = (await env.http.get(api + "?history=server", headers=admin)).json()["history"]
        assert hist[0] == {"text": "Be brief.", "ts": hist[0]["ts"], "actor": "human:operator"}
        await env.http.post(api, headers=admin, json={"csrf": "k", "action": "reset", "key": "server"})
        init, _ = await env.connect()
        assert init["instructions"] == guidance_mod.DEFAULTS["server"] + "\n\n" + guidance_mod.DEFAULTS["contact"]
        slot = next(s for s in (await env.http.get(api, headers=admin)).json()["slots"] if s["key"] == "cap:info.")
        assert slot["edited"] and slot["default"] is None and slot["actor"] == "human:operator"


@pytest.mark.asyncio
async def test_broken_store_serves_defaults_and_tip_failure_never_breaks_calls(tmp_path, monkeypatch):
    g = guidance_mod.Guidance(str(tmp_path / "missing-dir" / "guidance.db"))
    assert not g.editable and g.get("server") == guidance_mod.DEFAULTS["server"]
    with pytest.raises(ValueError):
        g.set("server", "x", "human:operator")
    async with server(tmp_path) as env:
        def boom(*a, **k):
            raise RuntimeError("tips broke")
        monkeypatch.setattr(env.guidance, "tips", boom)
        _, rpc = await env.connect()
        res = await rpc("tools/call", {"name": "rook_call", "arguments": {"cap": "shell.exec", "worker": "gpu-box"}})
        assert not res.get("isError") and "_tips" not in reply(res)


def test_every_default_key_is_valid_within_limits_and_host_neutral():
    hosts = ("gpu-box", "hypervisor", "workstation", "laptop", "agent-host", "win11", "ct102")
    for key, text in guidance_mod.DEFAULTS.items():
        assert not any(h in (key + text).lower() for h in hosts), key
        assert not re.search(r"\d\s*[KMG]i?B\b", text), key  # no point-in-time size estimates
        assert guidance_mod.KEY.match(key), key
        assert len(text) <= guidance_mod.LIMITS.get(key, guidance_mod.MAX_TIP), key


def test_contact_slot_is_appended_and_can_be_cleared():
    from rook.band_mcp.guidance import compose_instructions
    assert compose_instructions("Server.", "Call Sam.") == "Server.\n\nCall Sam."
    assert compose_instructions("Server.", "") == "Server."
    assert "notify.post" in guidance_mod.DEFAULTS["contact"] and "voice.speak" in guidance_mod.DEFAULTS["contact"]
