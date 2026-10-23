"""Persistent sessions end to end: the hub's supervisor (rook/hub/plugins/home/sessions.py)
and the real session host (rook/agents) over a real websocket, with the fake claude of
tests/agents_fake_claude.py. Checks that both halves speak the same protocol
(docs/design/persistent-agents.md 3.3-3.4): configure, an ask through the inbox, the
voice lane with a back-end result pushed back, btw and a fork folded in."""
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from rook.agents.host import Host
from rook.hub.plugins.home import sessions as S
from tests.test_agents_host import config, fx, wait_for  # noqa: F401
from tests.test_persistent_sessions import TOKEN, penv  # noqa: F401


class AiohttpWS:
    """aiohttp's WebSocketResponse as the starlette WebSocket that Sessions.accept uses."""

    def __init__(self, request):
        self.request = request
        self.headers = {k.lower(): v for k, v in request.headers.items()}
        self.ws = web.WebSocketResponse()

    async def accept(self):
        await self.ws.prepare(self.request)

    async def receive_text(self):
        msg = await self.ws.receive()
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError("closed")
        return msg.data

    async def send_text(self, text):
        await self.ws.send_str(text)

    async def close(self, code=1000):
        if not self.ws.prepared:
            await self.ws.prepare(self.request)
        await self.ws.close(code=code)


@pytest_asyncio.fixture
async def stack(penv, fx, unused_tcp_port_factory):  # noqa: F811
    async def link(request):
        ws = AiohttpWS(request)
        await penv.sess.accept(ws)
        return ws.ws

    app = web.Application()
    app.router.add_get(S.LINK_PATH, link)
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_tcp_port_factory()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    vport = unused_tcp_port_factory()
    penv.node.settings.set("home.voice_token_sha256", S.token_hash("voice-tok"), actor="human:op")
    host = Host(config(fx, hub_url=f"ws://127.0.0.1:{port}{S.LINK_PATH}",
                       voice_listen=f"127.0.0.1:{vport}"), TOKEN)
    task = asyncio.create_task(host.run())
    await wait_for(lambda: (penv.sess.lanes.get("house/main") or {}).get("state") == "idle",
                   10, "the house main lane")
    try:
        yield penv, host, vport
    finally:
        host.request_stop()
        await asyncio.wait_for(task, 10)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_hub_and_session_host_end_to_end(stack, fx):  # noqa: F811
    penv, host, vport = stack
    home = fx.roles / "house"
    assert "the house agent" in (home / "CLAUDE.md").read_text()
    assert (home / "voice" / ".mcp.json").exists()
    # An ask goes through the inbox to the main lane and back.
    res = await penv.node.dispatch("home.ask", {"question": "capital of France?"},
                                   "agent:claude")
    assert res["ok"], res
    assert res["result"]["answer"].startswith("echo: ")
    assert penv.sess.status()["main"]["session_id"]
    assert penv.sess.status()["line"] == "house agent: idle"
    # btw: answered by a throwaway fork of the main session.
    b = await penv.sess.btw("house", "where are we at?")
    assert b["ok"] and "where are we at?" in b["answer"], b
    # The voice lane: a turn streams back; a queued request comes back as a push.
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"http://127.0.0.1:{vport}/voice/house",
                                   headers={"Authorization": "Bearer voice-tok"}) as ws:
            ready = await ws.receive_json(timeout=10)
            assert ready["t"] == "ready"
            await ws.send_json({"t": "user", "turn": "t1", "text": "hello there",
                                "identity": {"owner": True, "name": "Bake"}})
            got = []
            while True:
                m = await ws.receive_json(timeout=10)
                got.append(m)
                if m["t"] == "end":
                    break
            assert any(m["t"] == "delta" for m in got) and got[-1]["first_ms"] is not None
            await wait_for(lambda: (penv.sess.voice.get("house") or {}).get("identity"), 5,
                           "voice identity at the hub")
            out = await penv.sess.flow("house", "queue", {"request": "turn on the lights"})
            assert out["queued"]
            push, seen = None, []
            while push is None:
                try:
                    m = await ws.receive_json(timeout=15)
                except asyncio.TimeoutError:
                    raise AssertionError(f"no push: {seen} metrics={penv.sess.metrics} "
                                         f"activity={list(penv.home.activity)[:3]}") from None
                seen.append(m)
                pushed = {x["turn"] for x in seen if x["t"] == "push"}
                if m["t"] == "end" and m["turn"] in pushed:
                    push = m
            assert "[back end]" in push["text"] and "turn on the lights" in push["text"]
    sends = [m for m in penv.sess.metrics if m["kind"] == "main"]
    assert len(sends) == 2                                   # the ask and the voice request
    # A fork: started from the main session, folded back in when done.
    f = await penv.sess.flow("house", "fork", {"request": "look something up"})
    await wait_for(lambda: penv.sess.forks[f["fork"]]["state"] == "done", 15, "the fork")
    await wait_for(lambda: len([m for m in penv.sess.metrics if m["kind"] == "main"]) == 3, 15,
                   "the fold turn")
