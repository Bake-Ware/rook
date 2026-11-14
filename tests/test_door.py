"""The MCP door, driven by a real MCP client (the MCP Python SDK) over HTTP on localhost."""

import asyncio
import contextlib
import json

import httpx2
import pytest
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client

from helpers import Clock, eventually, link_fields
from memory_link import connector
from rook_hub import Hub
from rook_worker import Cap, Worker


async def echo(args):
    return args


CAPS = {"demo.echo": Cap("Return the arguments unchanged.",
                         {"type": "object", "properties": {"text": {"type": "string"}}}, echo)}


@contextlib.asynccontextmanager
async def mcp_client(url, token):
    """A connected, initialised MCP client session, as a stock client makes one."""
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with httpx2.AsyncClient(headers=headers) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def refusal(group):
    """The MCPError inside the exception group the client raises on a refused connection."""
    while isinstance(group, BaseExceptionGroup):
        group = group.exceptions[0]
    assert isinstance(group, MCPError), group
    return group.error.message


async def tool(session, name, **arguments):
    """Call a tool; return (is_error, parsed JSON or the error text)."""
    result = await session.call_tool(name, arguments)
    text = result.content[0].text
    return result.is_error, (text if result.is_error else json.loads(text))


async def until(check, timeout=5.0):
    """Wait until the coroutine function `check` returns something truthy, and return it."""
    async with asyncio.timeout(timeout):
        while not (value := await check()):
            await asyncio.sleep(0.02)
        return value


def test_mcp_door(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    clock = Clock()
    hub = Hub(tmp_path / "hub", clock=clock)
    host, port = await hub.serve("127.0.0.1", 0)
    url, hub_url = f"http://{host}:{port}/mcp", f"http://{host}:{port}"
    connect = connector(hub)
    alice = hub.auth.create_user("Alice", "a password")
    agent = hub.auth.mint("agent", user=alice["guid"])
    agent_moniker = hub.auth.verify(agent)["moniker"]
    workers_running = []

    def start(state, name, token=None):
        worker = Worker(tmp_path / state, hub_url, connect, name=name, caps=CAPS, macs=[],
                        knock_every=0.02, token=token)
        workers_running.append((worker, asyncio.create_task(worker.run())))
        return worker

    try:
        # 1. No token, a made-up token and a join token are each refused, saying what to try next.
        join = hub.auth.mint("join", user=alice["guid"])
        for token, expected in ((None, "Authorization: Bearer"), ("made-up", "mint a new token"),
                                (join, "client scope")):
            with pytest.raises(BaseException) as e:
                async with mcp_client(url, token):
                    pass
            assert expected in refusal(e.value)

        async with mcp_client(url, agent) as session:
            # 2. An agent token connects and sees exactly the four tools.
            listed = await session.list_tools()
            assert sorted(t.name for t in listed.tools) == ["call", "caps", "journal", "stream", "workers"]
            assert all(t.description for t in listed.tools)

            # 3. A worker knocks; the agent sees it knocking with name and offered caps,
            #    approves it through call, and then sees it online, owned by the token's user.
            first = start("w1", "lab-box")

            async def knocking():
                return (await tool(session, "workers"))[1]["knocking"]
            [knock] = await until(knocking)
            assert knock["name"] == "lab-box" and knock["caps"][0] == {
                "name": "demo.echo", "description": "Return the arguments unchanged."}
            assert [c["name"] for c in knock["caps"]] == ["demo.echo", "net.http", "net.tcp", "net.udp"]   # and the primitives (D3)
            failed, approved = await tool(session, "call", worker="hub", cap="worker.approve",
                                          args={"key": knock["key"]}, why="approve the new lab box")
            assert not failed and approved["owner"] == alice["moniker"]
            moniker = approved["approved"]["moniker"]
            await eventually(lambda: first.identity)
            await eventually(lambda: hub.get_worker(moniker)["online"])
            failed, listing = await tool(session, "workers")
            [w] = listing["workers"]
            assert listing["knocking"] == []
            assert (w["moniker"], w["online"], w["owner"]["moniker"], w["caps"]) == (
                moniker, True, alice["moniker"], ["demo.echo", "net.http", "net.tcp", "net.udp"])
            assert w["facts"]["form"] == "python" and "disk" in w["health"] and "gateway_mac" in w

            # 4. The worker's caps, with description and schema.
            failed, listing = await tool(session, "caps", worker=moniker, match="demo.")
            assert listing == {"caps": [{"worker": moniker, "online": True, "caps": [{
                "name": "demo.echo", "description": "Return the arguments unchanged.", "returns": "result",
                "schema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}]}
            failed, hub_caps = await tool(session, "caps", worker="hub")
            assert [c["name"] for c in hub_caps["caps"][0]["caps"]] == [
                "worker.approve", "worker.attach", "worker.join-token",
                "job.create", "job.list", "job.pause", "job.unpause", "job.delete", "event.emit",
                "secret.create", "secret.list", "journal.redact", "plugin.install", "plugin.list", "worker.plugins",
                "device.add"]
            failed, matched = await tool(session, "caps", match="join")
            assert [(g["worker"], [c["name"] for c in g["caps"]]) for g in matched["caps"]] == [
                ("hub", ["worker.join-token"]), (moniker, [])]

            # 5. A call by moniker returns the result; a missing cap and a missing worker
            #    each say what to try next.
            failed, result = await tool(session, "call", worker=moniker, cap="demo.echo",
                                        args={"text": "hello"}, why="check the echo cap answers")
            assert not failed and result == {"text": "hello"}
            failed, text = await tool(session, "call", worker=moniker, cap="video.capture", args={},
                                      why="try a cap it does not have")
            assert failed and text.startswith("unknown_cap:") and "Next: List the caps" in text
            failed, text = await tool(session, "call", worker="no-such-box", cap="demo.echo", args={},
                                      why="try a worker that does not exist")
            assert failed and text.startswith("unknown_worker:") and "Next: List the workers" in text

            # 6. The agent mints a join token; a second worker knocks with it and is approved at once.
            failed, minted = await tool(session, "call", worker="hub", cap="worker.join-token",
                                        why="let the second lab box join unattended")
            assert not failed and minted["token"].count(".") == 2
            second = start("w2", "second-box", token=minted["token"])
            await eventually(lambda: second.identity)
            assert hub.list_pending() == []
            assert hub.get_worker(second.identity["guid"])["owner"] == alice["guid"]

            # 7. The journal shows the agent's calls with token, user and why, and narrows
            #    by worker and by a word in the text.
            clock.advance(5)
            failed, read = await tool(session, "journal", kind="call")
            whys = [(e["user"], e["token"], e["worker"], e["why"]) for e in read["entries"]]
            assert (alice["moniker"], agent_moniker, moniker, "check the echo cap answers") in whys
            assert (alice["moniker"], agent_moniker, "hub", "approve the new lab box") in whys
            failed, by_worker = await tool(session, "journal", worker=moniker)
            assert {e["worker"] for e in by_worker["entries"]} == {moniker}
            assert ("event", "worker.online") in [(e["kind"], e["result"].get("event"))
                                                  for e in by_worker["entries"]]          # its coming online
            assert [(e["kind"], e["why"]) for e in by_worker["entries"] if e["kind"] != "event"] == [
                ("call", "try a cap it does not have"), ("call", "check the echo cap answers"),
                ("auth", None)]                                  # its approval
            failed, by_word = await tool(session, "journal", text="ECHO CAP")
            assert [e["why"] for e in by_word["entries"]] == ["check the echo cap answers"]
            assert minted["token"] not in json.dumps(read)      # the journal never keeps a usable token

        # 8. Using the door moved the token's last-used time.
        assert hub.auth.verify(agent)["last_used"] == clock.now
    finally:
        for worker, running in workers_running:
            worker.stop()
            await running
        await hub.stop_serving()
        hub.close()


# ------------------------------------------------------------------- edges

async def post(http, url, token, method, params=None, message_id=1):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if message_id is not None:
        body["id"] = message_id
    return await http.post(url, json=body, headers=headers)


def test_door_edges(tmp_path):
    async def go():
        hub = Hub(tmp_path / "hub")
        host, port = await hub.serve("127.0.0.1", 0)
        url = f"http://{host}:{port}/mcp"
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        agent = hub.auth.mint("agent", user=alice)
        try:
            async with httpx2.AsyncClient() as http:
                assert (await http.get(url)).status_code == 405        # no server-pushed stream
                notified = await post(http, url, agent, "notifications/initialized", message_id=None)
                assert notified.status_code == 202
                reply = (await post(http, url, agent, "tools/call", {"name": "shell"})).json()
                assert "The tools are workers, caps, call, journal" in reply["error"]["message"]
                reply = (await post(http, url, agent, "tools/call",
                                    {"name": "call", "arguments": {"worker": "hub", "cap": "worker.join-token"}})).json()
                assert reply["result"]["isError"] and "why is required" in reply["result"]["content"][0]["text"]
                reply = (await post(http, url, agent, "tools/call",
                                    {"name": "journal", "arguments": {"since": "yesterday"}})).json()
                text = reply["result"]["content"][0]["text"]
                assert reply["result"]["isError"] and "Next: Give since like" in text
                hub.auth.revoke_token(hub.auth.verify(agent)["guid"], user=alice)
                refused = await post(http, url, agent, "tools/list")
                assert refused.status_code == 401 and "was revoked" in refused.json()["error"]["message"]
                assert json.loads(hub._db.execute(
                    "SELECT result FROM journal ORDER BY id DESC").fetchone()[0]) == {"refused": "token_revoked"}
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())


def test_attach_through_the_door(tmp_path):
    # W7: an agent attaches a likely rekey to the known worker with the worker.attach hub cap.
    from nacl.signing import SigningKey

    def knock(name):
        signing_key = SigningKey.generate()
        key = signing_key.verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": name, "key": key, "macs": [], "caps": [],
                   **link_fields(signing_key)})
        return key

    async def go():
        host, port = await hub.serve("127.0.0.1", 0)
        try:
            async with mcp_client(f"http://{host}:{port}/mcp", agent) as session:
                old = knock("lab-box")
                failed, approved = await tool(session, "call", worker="hub", cap="worker.approve",
                                              args={"key": old}, why="approve the lab box")
                moniker = approved["approved"]["moniker"]
                new = knock("lab-box")                     # the same box, after losing its key
                failed, listing = await tool(session, "workers")
                assert listing["knocking"][0]["likely_rekey_of"] == [{"moniker": moniker, "matched": ["name lab-box"]}]
                failed, attached = await tool(session, "call", worker="hub", cap="worker.attach",
                                              args={"key": new, "worker": moniker}, why="the lab box was reinstalled")
                assert not failed and attached["attached_to"]["moniker"] == moniker
                assert [(k["key"], k["revoked_at"] is None) for k in hub.seen(moniker)["keys"]] == [
                    (old, False), (new, True)]
                failed, text = await tool(session, "call", worker="hub", cap="worker.attach",
                                          args={"key": new}, why="forget the worker argument")
                assert failed and "args.worker is required" in text and "Next:" in text
        finally:
            await hub.stop_serving()

    hub = Hub(tmp_path / "hub")
    agent = hub.auth.mint("agent", user=hub.auth.create_user("Alice", "a password")["guid"])
    try:
        asyncio.run(go())
    finally:
        hub.close()


def test_no_body_limit_on_the_door(tmp_path):
    # Call arguments will carry file contents: /mcp takes any size; /knock keeps aiohttp's default.
    async def go():
        hub = Hub(tmp_path / "hub")
        host, port = await hub.serve("127.0.0.1", 0)
        agent = hub.auth.mint("agent", user=hub.auth.create_user("Alice", "a password")["guid"])
        big = "x" * (2 * 1024 * 1024)
        try:
            async with httpx2.AsyncClient() as http:
                reply = await post(http, f"http://{host}:{port}/mcp", agent, "tools/call",
                                   {"name": "workers", "arguments": {"padding": big}})
                assert reply.status_code == 200 and not reply.json()["result"]["isError"]
                knock = await http.post(f"http://{host}:{port}/knock", content=big.encode())
                assert knock.status_code == 413
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())
