"""The 0.5 hub's MCP door (rook/core2/hub/doors/mcp.py) and its stdio shim:
a hub in-process on a loopback band with the fake worker from
test_core2_hub_runtime, reached as an MCP server by raw JSON-RPC, by the
``mcp`` client library and through the shim.

Every test runs under a hard timeout; HTTP requests run in a thread with
their own socket timeout."""
from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from rook.core2.hub import runtime  # noqa: E402
from rook.core2.hub.doors import mcp_stdio  # noqa: E402
from rook.core2.hub.doors.mcp import TOOLS, McpDoor  # noqa: E402
from test_core2_hub_runtime import WHY, WID, FakeWorker, config, run, until  # noqa: E402

AGENT = runtime.Caller(person="person:bake", agent="agent:claude")


@pytest.fixture
def mcp_hub(tmp_path):
    """start(**config) -> (hub, worker, url, token for claude acting for bake)."""
    async def start(**over):
        band = runtime.Loopback()
        cfg = config(tmp_path, **over)
        cfg.doors = {"mcp": {"bind": "127.0.0.1:0", "origins": ["http://ok.example"]}}
        hub = runtime.Hub(cfg, transport=band)
        await hub.start()
        w = FakeWorker(hub, band, tmp_path / "w")
        await w.announce()
        await until(lambda: "echo.say" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
        return hub, w, f"http://127.0.0.1:{hub.doors[0].port}/mcp", hub.add_token(AGENT)
    return start


def post(url, body, token=None, raw=None, headers=None, method="POST"):
    """(status, headers, parsed body or None), in a thread."""
    def go():
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, hdrs, text = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, text = e.code, e.headers, e.read()
        return status, hdrs, json.loads(text) if text else None
    return asyncio.to_thread(go)


async def rpc(url, token, method, params=None, mid=1):
    status, _, out = await post(url, {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, token)
    assert status == 200 and out["id"] == mid, (status, out)
    return out


async def tool(url, token, name, **args):
    out = await rpc(url, token, "tools/call", {"name": name, "arguments": args})
    res = out["result"]
    assert [c["type"] for c in res["content"]] == ["text"]
    return res["isError"], json.loads(res["content"][0]["text"])


def test_tools_end_at_dispatch(mcp_hub):
    async def go():
        hub, w, url, token = await mcp_hub()
        try:
            init = (await rpc(url, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                         "clientInfo": {"name": "t", "version": "0"}}))["result"]
            assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
            assert "why" in init["instructions"] and init["serverInfo"]["name"] == "rook"
            old = await rpc(url, token, "initialize", {"protocolVersion": "1999-01-01"})
            assert old["result"]["protocolVersion"] == "2025-11-25"
            status, _, body = await post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token)
            assert (status, body) == (202, None)
            assert (await rpc(url, token, "ping"))["result"] == {}
            tools = (await rpc(url, token, "tools/list"))["result"]["tools"]
            assert [t["name"] for t in tools] == ["call", "caps", "workers", "journal", "holds", "session", "stream"]
            assert [t["name"] for t in tools if t.get("annotations", {}).get("readOnlyHint")] == [
                "caps", "workers", "journal", "holds"]

            # a call: the worker verifies a ticket for the agent acting for the person
            err, out = await tool(url, token, "call", worker="kaiju", cap="echo.say", args={"text": "hi"}, why=WHY)
            assert not err and out["result"] == {"said": "hi", "person": "person:bake", "agent": "agent:claude"}
            t = w.verified[-1]
            assert (t["cap"], t["person"], t["agent"], t["why"]) == ("echo.say", "person:bake", "agent:claude", WHY)
            entry = hub.journal.query(call=out["id"])[0]
            assert (entry["decision"], entry["ok"], entry["agent"]) == ("allow", True, "agent:claude")
            # why is required, as at every door
            err, out = await tool(url, token, "call", worker="kaiju", cap="echo.say", args={"text": "hi"})
            assert err and out["error"] == "denied: why required"
            err, out = await tool(url, token, "call", worker="nowhere", cap="echo.say", why=WHY)
            assert err and out["error"] == "unknown worker: nowhere"

            # discovery: one line per cap, full detail for an exact cap
            err, out = await tool(url, token, "caps", match="echo.")
            assert not err and list(out["workers"]) == ["kaiju"]
            assert out["workers"]["kaiju"] == {"online": True, "caps": {
                "echo.relay": "read: Calls echo.say again through the hub.",
                "echo.root": "write: Needs administrator rights.", "echo.say": "read: Say it back."}}
            _, out = await tool(url, token, "caps", match="echo.say")
            say = out["workers"]["kaiju"]["caps"]["echo.say"]
            assert say["risk"] == "read" and say["args"]["text"]["required"] is True
            _, out = await tool(url, token, "caps", match="*.manifests", full=True)
            assert out["workers"]["kaiju"]["caps"]["core.manifests"]["scope"] == "self"
            _, out = await tool(url, token, "caps", match="nothing.")
            assert out["workers"] == {}
            _, out = await tool(url, token, "workers")
            names = {x["name"]: x for x in out["workers"]}
            assert names["kaiju"]["online"] is True and names["kaiju"]["id"] == WID and "echo" in names["kaiju"]["plugins"]
            assert "problem" not in names["kaiju"]       # nothing to say: left out

            # the journal reads back, trimmed
            _, out = await tool(url, token, "journal", cap="echo.say", agent="agent:claude", limit=5)
            assert out["entries"] and all(e["cap"] == "echo.say" for e in out["entries"])
            assert not {"hash", "prev", "ticket"} & set(out["entries"][0])
            err, out = await tool(url, token, "journal", since=-1e30, limit=10**9)
            assert not err                                     # clamped by the hub
            err, out = await tool(url, token, "journal", limit="lots")
            assert err and out["error"] == "bad arguments: limit must be an integer"
            err, out = await tool(url, token, "caps", match=["echo."])
            assert err and out["error"] == "bad arguments: match must be a string"
            err, out = await tool(url, token, "call", worker="kaiju", cap="echo.say", reason=WHY)
            assert err and out["error"].startswith("bad arguments: unknown argument reason (takes worker, cap")
            err, out = await tool(url, token, "call", worker="kaiju", cap="echo.say", elevate=1)
            assert err and out["error"] == "bad arguments: elevate must be a boolean"

            # a session narrows the agent's calls
            _, s = await tool(url, token, "session", caps=["core.*"], project="castling")
            err, out = await tool(url, token, "call", worker="kaiju", cap="echo.say", args={"text": "x"},
                                  why=WHY, session=s["session"])
            assert err and out["error"].startswith("denied: session")
            assert (await tool(url, token, "session", end=s["session"]))[1] == {"ok": True}

            # protocol errors
            assert (await rpc(url, token, "tools/call", {"name": "nope"}))["error"]["code"] == -32602
            assert (await rpc(url, token, "resources/list"))["error"]["code"] == -32601
        finally:
            await hub.stop()
    run(go())


def test_door_refuses_before_dispatch(mcp_hub):
    async def go():
        hub, _, url, token = await mcp_hub()
        try:
            ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            status, hdrs, _ = await post(url, ping)
            assert status == 401 and hdrs["WWW-Authenticate"] == "Bearer"
            assert (await post(url, ping, "not-a-token"))[0] == 401
            assert (await post(url, ping, token, headers={"Origin": "http://evil.example"}))[0] == 403
            assert (await post(url, ping, token, headers={"Origin": "http://ok.example"}))[0] == 200
            assert (await post(url, None, token, method="GET"))[0] == 405
            assert (await post(url.replace("/mcp", "/other"), ping, token))[0] == 404
            status, _, out = await post(url, None, token, raw=b"{nope")
            assert status == 400 and out["error"]["code"] == -32700
            status, _, out = await post(url, {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": []}, token)
            assert out["error"]["code"] == -32600

            async def broken(caller):
                raise RuntimeError("boom")
            hub.workers_view = broken
            out = await rpc(url, token, "tools/call", {"name": "workers"})
            assert out["error"] == {"code": -32603, "message": "internal error"}
        finally:
            await hub.stop()
    run(go())


def test_holds_show_and_wait_for_the_person(mcp_hub):
    async def go():
        hub, _, url, token = await mcp_hub(sacred=["kaiju"])
        hub.identities.add_agent("claude", {"grants": ["*"]})     # echo.root runs elevated: admin
        try:
            err, out = await tool(url, token, "call", worker="kaiju", cap="echo.root", why=WHY)
            assert not err and out["held"] and "sacred machine" in out["error"]
            _, holds = await tool(url, token, "holds")
            assert [(h["hold"], h["agent"]) for h in holds["holds"]] == [(out["hold"], "agent:claude")]
            assert hub.journal.query(call=out["id"])[0]["decision"] == "held"
        finally:
            await hub.stop()
    run(go())


@pytest.mark.filterwarnings("ignore::DeprecationWarning")   # streamablehttp_client: newest mcp renamed it
def test_the_mcp_client_library_talks_to_the_door(mcp_hub):
    mcp = pytest.importorskip("mcp")
    from mcp.client.streamable_http import streamablehttp_client

    async def go():
        hub, _, url, token = await mcp_hub()
        try:
            async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
                async with mcp.ClientSession(r, w) as s:
                    init = await s.initialize()
                    assert "caps" in init.instructions
                    assert {t.name for t in (await s.list_tools()).tools} == {t["name"] for t in TOOLS}
                    res = await s.call_tool("call", {"worker": "kaiju", "cap": "echo.say",
                                                     "args": {"text": "sdk"}, "why": WHY})
                    assert not res.isError and json.loads(res.content[0].text)["result"]["said"] == "sdk"
        finally:
            await hub.stop()
    run(go())


def test_stdio_shim(mcp_hub, tmp_path, monkeypatch):
    async def go():
        hub, _, url, token = await mcp_hub()
        try:
            lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "call", "arguments": {
                         "worker": "kaiju", "cap": "echo.say", "args": {"text": "pipe"}, "why": WHY}}}]
            out = io.StringIO()
            monkeypatch.setenv("ROOK_TOKEN", token)
            rc = await asyncio.to_thread(mcp_stdio.main, ["--url", url],
                                         io.StringIO("".join(json.dumps(m) + "\n" for m in lines) + "{bad\n"), out)
            replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
            assert rc == 0 and set(replies) == {1, 2, None}       # nothing for the notification
            assert replies[1]["result"]["serverInfo"]["name"] == "rook"
            assert json.loads(replies[2]["result"]["content"][0]["text"])["result"]["said"] == "pipe"
            assert replies[None]["error"]["code"] == -32700
            # a bad token: the request still gets a JSON-RPC answer
            (tmp_path / "tok").write_text("wrong\n")
            out = io.StringIO()
            await asyncio.to_thread(mcp_stdio.main, ["--url", url, "--token-file", str(tmp_path / "tok")],
                                    io.StringIO(json.dumps(lines[0]) + "\n"), out)
            assert "HTTP 401" in json.loads(out.getvalue())["error"]["message"]
        finally:
            await hub.stop()
    run(go())


def test_stream_tool_calls_the_stream_caps():
    """The stream tool is call with the castling-streams cap and args filled in."""
    seen = []

    class Hub:
        async def call(self, caller, body, door):
            seen.append((body, door))
            return {"id": "c", "ok": True, "result": {"events": [], "next": 3}}

    door = McpDoor(Hub(), {})
    run(door.rpc(AGENT, "tools/call", {"name": "stream", "arguments": {
        "worker": "kaiju", "stream": "s_1", "why": WHY, "after": 2, "wait": 5}}))
    run(door.rpc(AGENT, "tools/call", {"name": "stream", "arguments": {
        "worker": "kaiju", "stream": "s_1", "why": WHY, "input": {"text": "ls\n"}}}))
    assert seen == [
        ({"worker": "kaiju", "cap": "stream.read", "args": {"stream": "s_1", "after": 2, "wait": 5}, "why": WHY}, "mcp"),
        ({"worker": "kaiju", "cap": "stream.write", "args": {"stream": "s_1", "type": "input",
                                                             "body": {"text": "ls\n"}}, "why": WHY}, "mcp")]


def test_the_kill_switch_closes_every_path_and_survives_a_restart(mcp_hub, tmp_path):
    # Review D7: a closed MCP door refuses its reads and session starts too,
    # the same agent token on the HTTP door, and stays closed after a
    # restart; an owner closes and opens it by route or cap.
    async def go():
        hub, w, url, token = await mcp_hub()
        try:
            from rook.core2.hub.doors import make_door
            http = make_door("http", hub, {"bind": "127.0.0.1:0"})
            await http.start()
            hub.doors.append(http)
            base = f"http://127.0.0.1:{http.port}"
            owner = (hub.data / "owner.token").read_text().strip()
            bare = hub.add_token(runtime.Caller(agent="agent:claude"))
            status, _, out = await post(f"{base}/doors/mcp", {"open": False}, token)       # an agent: no
            assert out["error"].startswith("denied: only an owner") and not hub.dispatcher.closed_doors
            status, _, out = await post(f"{base}/doors/mcp", {"open": False}, owner)
            assert out == {"ok": True, "closed": ["mcp"]}, out
            for method in ("tools/list", "initialize"):
                status, _, out = await post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}, token)
                assert status == 503 and out["error"] == "denied: door mcp closed", (status, out)
            for agent_token in (token, bare):
                status, _, out = await post(f"{base}/workers", None, agent_token, method="GET")
                assert status == 503 and out["error"] == "denied: door mcp closed"
                status, _, out = await post(f"{base}/call", {"worker": "kaiju", "cap": "echo.say",
                                                             "args": {"text": "x"}, "why": WHY}, agent_token)
                assert status == 503
            status, _, out = await post(f"{base}/workers", None, owner, method="GET")     # the person: yes
            assert status == 200 and out["ok"]
            status, _, out = await post(f"{base}/call", {"worker": "rook", "cap": "hub.status", "why": WHY}, owner)
            assert out["result"]["closed_doors"] == ["mcp"]
            # the cap: an agent acting for the owner can't open it (nor reach it while mcp is closed)
            out = await hub.call(AGENT, {"worker": "rook", "cap": "hub.door", "why": WHY,
                                         "args": {"door": "mcp", "open": True}}, "web")
            assert out["error"] == "denied: door mcp closed"
            hub.dispatcher.closed_doors.discard("mcp")
            out = await hub.call(AGENT, {"worker": "rook", "cap": "hub.door", "why": WHY,
                                         "args": {"door": "web", "open": False}}, "web")
            assert out["error"].startswith("denied:") and not hub.dispatcher.closed_doors, out
            hub.dispatcher.closed_doors.add("mcp")
            closes = [e for e in hub.journal.query(kind="door")]
            assert closes and closes[0]["by"] == "person:bake" and closes[0]["open"] is False
        finally:
            await hub.stop()

        hub2 = runtime.Hub(hub.cfg, transport=runtime.Loopback())          # same data folder
        await hub2.start()
        try:
            assert hub2.dispatcher.closed_doors == {"mcp"}
            url2 = f"http://127.0.0.1:{hub2.doors[0].port}/mcp"
            status, _, out = await post(url2, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token)
            assert status == 503
            out = await hub2.call(runtime.Caller(person="person:bake"), {
                "worker": "rook", "cap": "hub.door", "args": {"door": "mcp", "open": True}, "why": WHY}, "web")
            assert out["ok"] and out["result"] == {"closed": []}, out
            assert (await rpc(url2, token, "ping"))["result"] == {}
        finally:
            await hub2.stop()
    run(go())
