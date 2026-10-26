"""Permissions end to end through the MCP bridge: principals from tokens,
decisions on journal rows, audit vs enforce, hub tools as caps on `rook`."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from rook.band_mcp.client import BandClient
from rook.band_mcp.server import build_server
from rook.hub.policy import DEFAULT_POLICY
from test_audit_attribution import STATIC, Session, rows


def _band():
    client = BandClient("test-band")
    sent = []

    async def send(data):
        msg = json.loads(data)
        sent.append(msg)
        if msg.get("cap"):
            asyncio.get_running_loop().call_soon(client._handle_reply, {
                "id": msg["id"], "from": msg.get("target"), "ok": True, "result": "done"})
    client.transport.send = send
    client.workers["w1"] = {"worker_id": "w1", "name": "gpu-box", "caps": ["shell.exec", "info.host"],
                            "last_seen": 1e12}
    return client, sent


@asynccontextmanager
async def mcp(tmp_path, policy=None):
    if policy is not None:
        (tmp_path / "policy.json").write_text(json.dumps(policy))
    band, sent = _band()
    server, store = build_server(band, public_url="https://mcp.example.com",
                                 persist_path=str(tmp_path / "tokens.json"), static_token=STATIC,
                                 journal_path=str(tmp_path / "journal.db"))
    app = server.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Accept": "application/json, text/event-stream"}) as http:
        yield SimpleNamespace(http=http, store=store, sent=sent, journal=str(tmp_path / "journal.db"))


def _text(res):
    return json.loads(res["content"][0]["text"])


def _people(tmp_path):
    """access.db with an owner (u_own) and a member (u_mem) who may only
    read info on gpu-box."""
    from rook.hub.access import AccessStore
    a = AccessStore(str(tmp_path / "access.db"))
    a.set_role("u_own", "owner", actor="test", words="the first owner")
    a.grant("u_mem", "worker", "gpu-box", caps=["info.*"], actor="test", words="info only")
    return a


@pytest.mark.asyncio
async def test_a_key_reaches_what_its_person_reaches(tmp_path):
    access = _people(tmp_path)
    async with mcp(tmp_path) as env:
        mine, theirs = env.store.mint_api_token("mine"), env.store.mint_api_token("theirs")
        access.set_token_user(theirs["agent_id"], "u_mem", actor="test", words="their key")
        # A key made before keys belonged to people goes to the first owner.
        s = await Session(env.http, mine["token"]).open()
        res = _text(await s.result("rook_call", {"why": "test", "cap": "shell.exec", "worker": "gpu-box",
                                                 "args": {"cmd": "id"}}))
        assert res["ok"] and env.sent[-1]["cap"] == "shell.exec"
        row = rows(env.journal, "cap='shell.exec'")[-1]
        assert row["decision"] == "allow" and row["principal"] == f"token:{mine['agent_id']}"
        assert access.token_user(mine["agent_id"], adopt=False) == "u_own"
        assert access.history("u_own")[0]["action"] == "token"
        # The member's key: their grant, in audit mode too (grants always hold).
        t = await Session(env.http, theirs["token"]).open()
        before = len(env.sent)
        res = _text(await t.result("rook_call", {"why": "test", "cap": "shell.exec", "worker": "gpu-box"}))
        assert not res["ok"] and "may not call shell.exec" in res["error"]
        assert len(env.sent) == before                     # never reached the band
        assert _text(await t.result("rook_call", {"why": "test", "cap": "info.host", "worker": "gpu-box"}))["ok"]
        denied = await t.result("rook_secret", {"action": "get", "name": "x"})
        assert denied.get("isError") and "denied" in denied["content"][0]["text"]
        # Moved to the owner, the same key reaches everything.
        access.set_token_user(theirs["agent_id"], "u_own", actor="test", words="give it to me")
        assert _text(await t.result("rook_call", {"why": "test", "cap": "shell.exec", "worker": "gpu-box"}))["ok"]


@pytest.mark.asyncio
async def test_the_policy_sees_a_key_as_its_person(tmp_path):
    access = _people(tmp_path)
    access.grant("u_mem", "any", caps=["*"], actor="test", words="everything")
    doc = {**DEFAULT_POLICY, "mode": "enforce",
           "rules": [{"id": "no-shell-for-u_mem", "who": "human:u_mem", "deny": "shell.exec"},
                     {"id": "old-key-rule", "who": "token:*", "deny": "info.host"}]}
    async with mcp(tmp_path, doc) as env:
        key = env.store.mint_api_token("theirs")
        access.set_token_user(key["agent_id"], "u_mem", actor="test", words="their key")
        s = await Session(env.http, key["token"]).open()
        res = _text(await s.result("rook_call", {"why": "test", "cap": "shell.exec", "worker": "gpu-box"}))
        assert not res["ok"] and "no-shell-for-u_mem" in json.dumps(res)
        # The key-only rule was retired when the policy loaded.
        assert _text(await s.result("rook_call", {"why": "test", "cap": "info.host", "worker": "gpu-box"}))["ok"]
        assert (tmp_path / "policy.json.pre-tokens").exists()
        # The static key is the hub's own owner.
        op = await Session(env.http, STATIC).open()
        assert _text(await op.result("rook_call", {"why": "test", "cap": "shell.exec", "worker": "gpu-box"}))["ok"]
