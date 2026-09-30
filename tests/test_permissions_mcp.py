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


@pytest.mark.asyncio
async def test_audit_mode_changes_nothing_but_journals_would_deny(tmp_path):
    async with mcp(tmp_path) as env:
        ro = env.store.mint_api_token("reader", role="readonly")
        s = await Session(env.http, ro["token"]).open()
        res = _text(await s.result("rook_call", {"cap": "shell.exec", "worker": "gpu-box",
                                                 "args": {"cmd": "id"}}))
        assert res["ok"] and env.sent[-1]["cap"] == "shell.exec"
        row = rows(env.journal, "cap='shell.exec'")[-1]
        assert row["decision"] == "would_deny" and row["principal"] == f"token:{ro['agent_id']}"
        assert row["tier"] == "exec" and row["rule"] == "default:role:readonly"
        op = await Session(env.http, STATIC).open()
        await op.result("rook_call", {"cap": "shell.exec", "worker": "gpu-box", "args": {"cmd": "id"}})
        assert rows(env.journal, "cap='shell.exec'")[-1]["decision"] == "allow"


@pytest.mark.asyncio
async def test_enforce_mode_denies_readonly_exec_and_admin_tools(tmp_path):
    async with mcp(tmp_path, {**DEFAULT_POLICY, "mode": "enforce"}) as env:
        ro = env.store.mint_api_token("reader", role="readonly")
        s = await Session(env.http, ro["token"]).open()
        before = len(env.sent)
        res = _text(await s.result("rook_call", {"cap": "shell.exec", "worker": "gpu-box"}))
        assert not res["ok"] and res["error"].startswith("denied: token:")
        assert len(env.sent) == before                     # never reached the band
        assert rows(env.journal, "cap='shell.exec'")[-1]["decision"] == "deny"
        ok = _text(await s.result("rook_call", {"cap": "info.host", "worker": "gpu-box"}))
        assert ok["ok"]
        denied = await s.result("rook_secret", {"action": "get", "name": "x"})
        assert denied.get("isError") and "denied" in denied["content"][0]["text"]
        listed = await s.result("rook_secret", {"action": "list"})
        assert not listed.get("isError")
        # Agents keep exec; their admin calls stay allowed but are journaled.
        agent = env.store.mint_api_token("worker-bot")
        a = await Session(env.http, agent["token"]).open()
        assert _text(await a.result("rook_call", {"cap": "shell.exec", "worker": "gpu-box"}))["ok"]
