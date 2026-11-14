"""A hub that stops wakes the requests waiting on it (C3, S2; Bake 13 Nov on long calls; F1). A stream
read waiting with `wait=40` answers at once with what it has, and a call that has not answered answers
"running" with its stream, both exactly as at the end of their wait, at the site and at the MCP door.
`stop_serving()` returns within 1.5 s with all of them waiting, and with a linked worker and strangers
on /band too. The next start ends the call's stream with hub_restarted; the read stream is still there."""

import asyncio
import json
import signal
import sys
import time

import aiohttp

import rook_hub
from helpers import eventually
from memory_link import connector
from rook_hub import Hub
from rook_worker import Cap, Worker
from test_door import mcp_client, tool
from test_first_run import Started
from test_hub_stop import BOUND, linked, stranger
from test_link_carrier import Setup
from test_site import login

NAME, PASSWORD = "Alice", "a password"   # Setup's user


async def opener(args, stream):
    """A stream that says one line, then waits."""
    async def first():
        await stream.send(b"ready\n")
    asyncio.create_task(first())
    return {"title": "a shell", "description": "waits for more"}


async def never(args):
    await asyncio.Event().wait()        # never answers


CAPS = {"demo.open": Cap("Open a stream.", {"type": "object"}, opener, opens_stream=True),
        "demo.slow": Cap("Never answers.", {"type": "object"}, never)}


async def get(client, url):
    async with client.get(url) as response:
        return response.status, await response.json()


async def post(client, url, body):
    async with client.post(url, json=body) as response:
        return response.status, await response.json()


def without(answer, *names):
    return {k: v for k, v in answer.items() if k not in names}


def test_stop_wakes_reads_and_calls_at_the_site_and_the_door(tmp_path, monkeypatch):
    asyncio.run(wakes(tmp_path, monkeypatch))


async def wakes(tmp_path, monkeypatch):
    async with Setup(tmp_path / "hub") as setup:
        hub, base = setup.hub, setup.base
        played = await linked(setup)                               # a worker linked over /band
        strangers = [await stranger(base.replace("http", "ws") + "/band") for _ in range(3)]
        token = hub.auth.mint("agent", user=setup.alice)
        worker = Worker(tmp_path / "w", base, connector(hub), name="shell-box", caps=CAPS, macs=[],
                        knock_every=0.02)
        running = asyncio.create_task(worker.run())
        await eventually(lambda: any(p["name"] == "shell-box" for p in hub.list_pending()))
        box = hub.approve(worker.key, user=setup.alice)["moniker"]
        await eventually(lambda: hub.get_worker(box)["online"])
        stream = (await hub.call(box, "demo.open", {}, "a shell", user=setup.alice))["stream"]
        await eventually(lambda: hub.streams.get(stream) and hub._db.execute(
            "SELECT count(*) FROM stream_data").fetchone()[0] == 1)
        read_url = f"{base}/api/streams/{stream}/read?from=1"

        site = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        assert (await login(site, base, NAME, PASSWORD))[0] == 200
        async with mcp_client(f"{base}/mcp", token) as door:
            # What each answers today at the end of its wait: the reference, with short waits.
            status, read_ref = await get(site, read_url + "&wait=0.2")
            assert status == 200 and read_ref == {"stream": stream, "open": True, "entries": [], "next": 1}
            failed, door_read_ref = await tool(door, "stream", action="read", stream=stream, wait=0.2, **{"from": 1})
            assert not failed and door_read_ref["entries"] == []
            monkeypatch.setattr(rook_hub, "CALL_WAIT", 0.3)
            status, call_ref = await post(site, f"{base}/api/call", {"worker": box, "cap": "demo.slow", "args": {},
                                                                     "why": "the reference"})
            assert status == 200 and call_ref["type"] == "running", call_ref
            failed, door_call_ref = await tool(door, "call", worker=box, cap="demo.slow", args={}, why="the reference")
            assert not failed and door_call_ref["running"] is True
            monkeypatch.setattr(rook_hub, "CALL_WAIT", 45)

            # Each waits: a read with wait=40 and a call that never answers, at the site and the door.
            waits = {
                "site read": asyncio.create_task(get(site, read_url + "&wait=40")),
                "site call": asyncio.create_task(post(site, f"{base}/api/call", {
                    "worker": box, "cap": "demo.slow", "args": {}, "why": "waits at the site"})),
                "door read": asyncio.create_task(tool(door, "stream", action="read", stream=stream, wait=40,
                                                      **{"from": 1})),
                "door call": asyncio.create_task(tool(door, "call", worker=box, cap="demo.slow", args={},
                                                      why="waits at the door")),
            }
            await asyncio.sleep(0.5)
            assert not any(task.done() for task in waits.values())

            started = time.monotonic()
            await hub.stop_serving()
            assert time.monotonic() - started < BOUND
            answers = {name: await asyncio.wait_for(task, 2) for name, task in waits.items()}

        # Each got its ordinary answer, field by field as at the end of its wait.
        assert answers["site read"] == (200, read_ref)
        assert answers["door read"] == (False, door_read_ref)
        status, call = answers["site call"]
        assert status == 200 and set(call) == set(call_ref) == {"type", "id", "worker", "stream"}
        assert without(call, "id", "stream") == without(call_ref, "id", "stream") == {"type": "running", "worker": box}
        failed, door_call = answers["door call"]
        assert not failed and set(door_call) == set(door_call_ref) == {"id", "running", "worker", "stream", "next"}
        assert without(door_call, "id", "stream", "next") == without(door_call_ref, "id", "stream", "next")
        assert door_call["next"] == door_call_ref["next"].replace(door_call_ref["stream"], door_call["stream"])
        for answer in (call, door_call):
            record = hub.streams.get(answer["stream"])
            assert record["open"] and record["title"] == f"demo.slow on {box}"

        await site.close()
        for session, _ in strangers:
            await session.close()
        worker.stop()
        await asyncio.gather(running, return_exceptions=True)

    # The next start on the same data directory: the calls left running end with hub_restarted, on
    # their streams and in the journal; the stream that was being read is still there, and reads on
    # from where the client left off.
    again = Hub(tmp_path / "hub")
    await again.serve("127.0.0.1", 0)
    try:
        for answer in (call, door_call):
            read = await again.streams.read(answer["stream"])
            assert read["open"] is False
            last = json.loads(read["entries"][-1]["out"])
            assert last["type"] == "error" and last["code"] == "hub_restarted" and last["id"] == answer["id"]
            [entry] = again._db.execute("SELECT result FROM journal WHERE kind = 'call' AND "
                                        "json_extract(result, '$.id') = ?", (answer["id"],)).fetchall()
            assert json.loads(entry[0])["code"] == "hub_restarted"
        read = await again.streams.read(stream, read_ref["next"])
        assert read == {"stream": stream, "open": True, "entries": [], "next": 1}
        assert (await again.streams.read(stream))["entries"] == [{"position": 0, "out": "ready\n"}]
    finally:
        await again.stop_serving()
        again.close()


def test_the_start_command_exits_with_a_read_waiting(tmp_path):
    asyncio.run(start_command(tmp_path))


async def start_command(tmp_path):
    """The real start command with one client waiting on a stream read: SIGTERM, exit 0 within 3 s."""
    hub = await Started(tmp_path / "data", tmp_path / "vault.key").__aenter__()
    worker = running = None
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as site:
            assert (await post(site, hub.base + "/api/setup", {"name": NAME, "password": PASSWORD}))[0] == 200
            worker = Worker(tmp_path / "w", hub.base, name="shell-box", caps=CAPS, macs=[], knock_every=0.02)
            running = asyncio.create_task(worker.run())             # the real carrier, over /band

            async def pending():
                return (await get(site, hub.base + "/api/workers"))[1]["pending"]
            while not await pending():
                await asyncio.sleep(0.05)
            status, approved = await post(site, hub.base + "/api/approve", {"key": worker.key})
            assert status == 200, approved
            box = approved["approved"]["moniker"]
            for _ in range(200):                                     # until the worker has linked
                status, opened = await post(site, hub.base + "/api/call", {
                    "worker": box, "cap": "demo.open", "args": {}, "why": "a shell"})
                if status == 200:
                    break
                await asyncio.sleep(0.05)
            assert status == 200, opened
            stream = opened["stream"]
            reading = asyncio.create_task(get(site, f"{hub.base}/api/streams/{stream}/read?from=1&wait=40"))
            await asyncio.sleep(0.5)
            assert not reading.done()
            started = time.monotonic()
            hub.process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(hub.process.wait(), 3) == 0
            assert time.monotonic() - started < 3
            status, read = await reading
            assert status == 200 and read == {"stream": stream, "open": True, "entries": [], "next": 1}
    finally:
        if hub.process.returncode is None:
            hub.process.kill()
            await hub.process.wait()
        if worker is not None:
            worker.stop()
            await asyncio.gather(running, return_exceptions=True)
