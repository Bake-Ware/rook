"""Nothing that blocks runs on the hub's event loop (brief 53; docs/stress.md, fault 7).

The hub serves links, the door and the site on one asyncio loop. A read of the whole journal used
to run on it, under the hub's lock: at 100,000 entries it held the loop about 2 s, and no request
and no link was served meanwhile. Now the read runs on a thread, on a read-only connection of its
own, and its reply is encoded on a thread too.

And moving the work did not change its order: each link's messages are taken one after another, so
a stream's recording keeps the worker's order, starting with its stream_open; and the event bus
journals events in the order they were emitted, from the loop or from a thread."""

import asyncio
import base64
import json
import threading
import time

import aiohttp

from helpers import eventually
from memory_link import link_pair
from rook_hub import Hub
from rook_protocol import decode, encode
from test_hub import SHELL, approved, heartbeat

ENTRIES = 50_000


def fill(hub, n):
    """`n` journal entries, written straight into the database as a long run of calls would."""
    with hub._lock, hub._db:
        hub._db.executemany(
            "INSERT INTO journal (at, kind, user, token, worker, cap, why, result) "
            "VALUES (?, 'call', NULL, NULL, 'hub', 'secret.list', ?, ?)",
            [(1_700_000_000.0 + i, f"Fill {i}", json.dumps({"type": "result", "id": f"{i:032x}",
                                                             "result": {"secrets": []}})) for i in range(n)])


def elsewhere(base, token, out):
    """An agent on its own thread and event loop, as on another machine: it pings the door every
    20 ms while it reads the whole journal, and notes how long the read and the slowest ping took."""
    async def main():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as http:
            async def rpc(body):
                async with http.post(base + "/mcp", json=body, headers={"Authorization": f"Bearer {token}"}) as r:
                    return await r.read()
            reading, slowest = True, [0.0]

            async def pings():
                n = 0
                while reading:
                    started = time.monotonic()
                    await rpc({"jsonrpc": "2.0", "id": n, "method": "ping"})
                    slowest[0] = max(slowest[0], time.monotonic() - started)
                    n += 1
                    await asyncio.sleep(0.02)
            pinging = asyncio.create_task(pings())
            await asyncio.sleep(0.2)
            started = time.monotonic()
            raw = await rpc({"jsonrpc": "2.0", "id": "all", "method": "tools/call",
                             "params": {"name": "journal", "arguments": {"limit": 1_000_000}}})
            out["read"] = time.monotonic() - started
            reading = False
            await pinging
            out["slowest"], out["entries"] = slowest[0], len(json.loads(json.loads(raw)["result"]["content"][0]["text"])["entries"])
    asyncio.run(main())


def test_a_read_of_the_whole_journal_leaves_the_door_answering(tmp_path):
    asyncio.run(whole_journal(tmp_path))


async def whole_journal(tmp_path):
    hub = Hub(tmp_path / "hub")
    token = hub.auth.mint("agent", user=hub.auth.create_user("Alice", "a password")["guid"])
    fill(hub, ENTRIES)
    host, port = await hub.serve("127.0.0.1", 0)
    try:
        out = {}
        agent = threading.Thread(target=elsewhere, args=(f"http://{host}:{port}", token, out))
        agent.start()
        await asyncio.to_thread(agent.join)
        assert out["entries"] == ENTRIES + 1           # every entry, and the mint of the agent's token
        # While the read ran, the door kept answering: on the loop, the slowest ping waited about as
        # long as the whole read (0.97 of it here); off the loop, about a tenth.
        assert out["slowest"] < out["read"] / 3, out
    finally:
        await hub.stop_serving()
        hub.close()


def test_a_stream_keeps_the_workers_order_and_events_keep_theirs(tmp_path):
    asyncio.run(order(tmp_path))


async def order(tmp_path):
    hub = Hub(tmp_path / "hub")
    hub.bake = hub.auth.create_user("Bake", "a password")["guid"]
    try:
        key, record = approved(hub)
        hub_end, worker = link_pair(key)
        serving = asyncio.create_task(hub.serve_link(hub_end))
        await worker.send(encode(heartbeat(key, caps=(SHELL,))))
        assert decode(await worker.recv())["type"] == "heartbeat_reply"

        # A call opens a stream; the worker answers stream_open and at once sends 300 pieces of
        # output, without waiting for anything. Each is recorded, in the order sent.
        opening = asyncio.create_task(hub.call(record["guid"], "shell.open", {}, "Open a terminal.", user=hub.bake))
        call = decode(await worker.recv())
        await worker.send(encode({"type": "stream_open", "id": call["id"], "stream": "s1", "title": "t",
                                  "description": "d"}))
        for n in range(300):
            await worker.send(encode({"type": "stream_data", "stream": "s1",
                                      "data": base64.b64encode(f"{n};".encode()).decode()}))
        stream = (await opening)["stream"]
        entries = []
        while len(entries) < 300:
            got = await hub.streams.read(stream, len(entries), wait=5)
            entries += got["entries"]
        assert "".join(e["out"] for e in entries) == "".join(f"{n};" for n in range(300))

        # Events emitted from the loop and from threads at once are journaled in the order emitted.
        def emit(n):
            hub.events.emit("ordered", {"n": n})
        hub.events.start()
        for n in range(0, 200, 2):
            emit(n)
            await hub.off_loop(emit, n + 1)
        await eventually(lambda: len(journaled(hub)) == 200)
        assert journaled(hub) == list(range(200))
        await hub.events.stop()
        worker.close()
        await serving
    finally:
        hub.close()


def journaled(hub):
    with hub._lock:
        rows = hub._db.execute("SELECT result FROM journal WHERE kind = 'event' ORDER BY id").fetchall()
    return [json.loads(r[0])["payload"]["n"] for r in rows if json.loads(r[0])["event"] == "ordered"]
