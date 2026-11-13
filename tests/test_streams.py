"""Streams (S1 to S5): through the MCP door with real MCP clients, and a real
rook_worker on an in-memory link serving a demo stream cap."""

import asyncio
import json
import time

from helpers import eventually
from memory_link import connector
from rook_hub import Hub
from rook_worker import Cap, Worker
from test_door import mcp_client, tool

opened, closed_seen = [], []


async def demo(args, stream):
    """Open a stream that echoes its input; the test makes it emit lines with stream.send."""
    opened.append(stream)

    async def echo():
        while (data := await stream.read()) is not None:
            await stream.send(b"echo: " + data)
        closed_seen.append(stream.handle)
    asyncio.create_task(echo())
    return {"title": args.get("title", "demo"), "description": args.get("description", "")}


CAPS = {"demo.stream": Cap("Open a stream that echoes input.", {"type": "object"}, demo, opens_stream=True)}


def journal(hub, **where):
    rows = [dict(r) for r in hub._db.execute("SELECT * FROM journal ORDER BY id")]
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


def events(hub, name):
    return [json.loads(r["result"])["payload"] for r in journal(hub, kind="event")
            if json.loads(r["result"])["event"] == name]


def outs(read):
    return [e.get("out") or e.get("in") for e in read["entries"]]


async def read_until(session, name, start, count):
    """Read with waits from `start` until `count` entries have come."""
    entries, position = [], start
    while len(entries) < count:
        failed, read = await tool(session, "stream", action="read", stream=name, wait=5, **{"from": position})
        assert not failed, read
        entries, position = entries + read["entries"], read["next"]
    return entries


def test_streams(tmp_path):
    opened.clear(), closed_seen.clear()
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    alice, bob = (hub.auth.create_user(n, "a password") for n in ("Alice", "Bob"))
    token_a, token_b = hub.auth.mint("agent", user=alice["guid"]), hub.auth.mint("agent", user=bob["guid"])
    worker = Worker(tmp_path / "w", f"http://{host}:{port}", connector(hub), name="box", caps=CAPS, macs=[],
                    knock_every=0.02)
    running = asyncio.create_task(worker.run())
    await eventually(hub.list_pending)
    box = hub.approve(worker.key, user=alice["guid"])["moniker"]
    await eventually(lambda: hub.get_worker(box)["online"])

    try:
        async with mcp_client(f"http://{host}:{port}/mcp", token_a) as a, \
                mcp_client(f"http://{host}:{port}/mcp", token_b) as b:
            async def stream(session, **args):
                return await tool(session, "stream", **args)

            failed, job = await tool(a, "call", worker="hub", cap="job.create", why="watch for streams", args={
                "title": "saw a stream", "match": {"event": "stream.open"},
                "call": {"worker": "hub", "cap": "event.emit", "args": {"name": "saw.stream"}}})
            assert not failed

            # 1. A opens a stream; it is named by moniker and listed with title, description,
            #    worker and opener; the worker's status shows one open stream (S1, S3, S4, S5).
            failed, opening = await tool(a, "call", worker=box, cap="demo.stream", why="watch the build",
                                         args={"title": "build log", "description": "cargo build on box"})
            assert not failed and len(opening["stream"].split("-")) == 3
            name = opening["stream"]
            failed, listing = await stream(a, action="list")
            [listed] = listing["streams"]
            assert (listed["moniker"], listed["title"], listed["description"], listed["worker"], listed["opened_by"],
                    listed["open"]) == (name, "build log", "cargo build on box", box, alice["moniker"], True)
            await eventually(lambda: hub.get_worker(box)["streams"] == 1)
            failed, seen = await tool(a, "workers")
            assert seen["workers"][0]["streams"] == 1
            live = opened[0]

            # 2. Three lines; A reads them and the next position; then waits, and gets a
            #    fourth line the moment it arrives.
            for n in (1, 2, 3):
                await live.send(f"line {n}\n")
            entries = await read_until(a, name, 0, 3)
            assert [e["out"] for e in entries] == ["line 1\n", "line 2\n", "line 3\n"]
            failed, read = await stream(a, action="read", stream=name, **{"from": 0})
            assert outs(read) == ["line 1\n", "line 2\n", "line 3\n"] and read["next"] == 3
            waiting = asyncio.create_task(stream(a, action="read", stream=name, wait=10, **{"from": 3}))
            await asyncio.sleep(0.3)
            assert not waiting.done()                       # nothing yet: it is waiting
            started = time.monotonic()
            await live.send("line 4\n")
            failed, read = await waiting
            assert outs(read) == ["line 4\n"] and read["next"] == 4 and time.monotonic() - started < 2

            # 3. B attaches and reads from the start; both read on and see a fifth line (S2).
            failed, read_b = await stream(b, action="read", stream=name, **{"from": 0})
            assert outs(read_b) == ["line 1\n", "line 2\n", "line 3\n", "line 4\n"]
            await live.send("line 5\n")
            for session in (a, b):
                failed, read = await stream(session, action="read", stream=name, wait=5, **{"from": 4})
                assert outs(read) == ["line 5\n"]

            # 4. B writes; the worker gets it and echoes; both read the echo; the recording
            #    keeps the input with B as its sender.
            failed, wrote = await stream(b, action="write", stream=name, text="hello\n")
            assert not failed and wrote == {"written": 6}
            for session in (a, b):
                assert await read_until(session, name, 5, 2) == [
                    {"position": 5, "in": "hello\n", "from": bob["moniker"]}, {"position": 6, "out": "echo: hello\n"}]
            [row] = hub._db.execute("SELECT user, direction FROM stream_data WHERE position = 5").fetchall()
            assert (row["user"], row["direction"]) == (bob["guid"], "in")

            # 5. A's token is revoked: the stream stays open, and B still reads and writes (S2).
            hub.auth.revoke_token(hub.auth.verify(token_a)["guid"], user=alice["guid"])
            failed, wrote = await stream(b, action="write", stream=name, text="still here\n")
            assert not failed
            entries = await read_until(b, name, 7, 2)
            assert hub.streams.get(name)["open"] and [e.get("in") or e.get("out") for e in entries] == [
                "still here\n", "echo: still here\n"]

            # 6. The job matching stream.open ran at step 1 (J2).
            await eventually(lambda: journal(hub, kind="job", job=job["job"]["guid"]))
            assert events(hub, "stream.open")[0]["stream_moniker"] == name

        # 7. The hub closes and opens again; the worker stays up and reconnects. The stream
        #    is still listed, reads from the start, and new data arrives.
        await hub.stop_serving()
        hub.close()
        await running
        hub = Hub(tmp_path / "hub")
        host, port = await hub.serve("127.0.0.1", 0)
        worker.connect = connector(hub)
        running = asyncio.create_task(worker.run())
        await eventually(lambda: hub.get_worker(box)["online"])
        async with mcp_client(f"http://{host}:{port}/mcp", token_b) as b:
            async def stream(session, **args):
                return await tool(session, "stream", **args)

            failed, listing = await stream(b, action="list", open=True)
            assert [s["moniker"] for s in listing["streams"]] == [name]
            failed, read = await stream(b, action="read", stream=name, **{"from": 0})
            assert outs(read)[:5] == ["line 1\n", "line 2\n", "line 3\n", "line 4\n", "line 5\n"]
            end = read["next"]
            await live.send("after the restart\n")
            failed, read = await stream(b, action="read", stream=name, wait=5, **{"from": end})
            assert outs(read) == ["after the restart\n"]

            # 8. A value in the recording is redacted there too (L5).
            await live.send("password=plain-value-777\n")
            await read_until(b, name, end + 1, 1)
            failed, redacted = await tool(b, "call", worker="hub", cap="journal.redact",
                                          args={"value": "plain-value-777"}, why="take the password out")
            assert not failed and redacted["recordings"] == 1
            failed, read = await stream(b, action="read", stream=name, **{"from": 0})
            assert "plain-value-777" not in json.dumps(read) and "password=(redacted)\n" in outs(read)

            # 9. B closes it: the worker's cap sees the close, the open count goes to zero, the
            #    journal and the bus record it under B, and it can still be read (S4, L1, J2).
            failed, closed = await stream(b, action="close", stream=name, why="the build is done")
            assert not failed and closed == {"closed": name}
            await eventually(lambda: closed_seen == [live.handle])
            await eventually(lambda: hub.get_worker(box)["streams"] == 0)
            [entry] = journal(hub, kind="stream")
            assert (entry["user"], entry["why"]) == (bob["guid"], "the build is done")
            await eventually(lambda: events(hub, "stream.close"))
            assert events(hub, "stream.close")[0]["by"] == bob["moniker"]
            failed, listing = await stream(b, action="list")
            assert listing["streams"][0]["open"] is False and listing["streams"][0]["closed_by"] == bob["moniker"]
            failed, read = await stream(b, action="read", stream=name, **{"from": 0})
            assert read["open"] is False and outs(read)[0] == "line 1\n" and read["next"] == end + 2

            # 10. A second stream, closed by the worker itself: a waiting reader is told at once.
            failed, second = await tool(b, "call", worker=box, cap="demo.stream", why="a short one",
                                        args={"title": "short", "description": "closes itself"})
            waiting = asyncio.create_task(stream(b, action="read", stream=second["stream"], wait=30, **{"from": 0}))
            await asyncio.sleep(0.3)
            started = time.monotonic()
            await opened[1].close()
            failed, read = await waiting
            assert read["open"] is False and read["entries"] == [] and time.monotonic() - started < 2
            assert hub.streams.get(second["stream"])["closed_by"] == "worker"

            # 11. Errors say what to try next.
            for args, code in (({"action": "read", "stream": "no-such-stream"}, "unknown_stream"),
                               ({"action": "write", "stream": name, "text": "x"}, "stream_closed"),
                               ({"action": "read", "stream": name, "from": end + 99}, "bad_position"),
                               ({"action": "read", "stream": name, "limit": "x"}, "bad_message"),
                               ({"action": "read", "stream": name, "wait": "soon"}, "bad_message")):
                failed, text = await stream(b, **args)
                assert failed and text.startswith(code + ":") and "Next:" in text
    finally:
        worker.stop()
        await running
        await hub.stop_serving()
        hub.close()


def test_bytes_that_are_not_text_and_an_offline_worker(tmp_path):
    from memory_link import link_pair
    from rook_hub.errors import HubError
    from rook_protocol import decode, encode
    from test_hub import heartbeat, knock, new_key

    async def go():
        hub = Hub(tmp_path / "hub")
        await hub.serve("127.0.0.1", 0)
        try:
            alice = hub.auth.create_user("Alice", "a password")["guid"]
            key = new_key()
            hub.knock(knock(key))
            hub.approve(key, user=alice)
            hub_end, link = link_pair(key)
            asyncio.get_running_loop().create_task(hub.serve_link(hub_end))
            await link.send(encode(heartbeat(key, caps=({"name": "demo.stream", "description": "d",
                                                          "schema": {}, "returns": "stream"},))))
            await link.recv()
            call = asyncio.create_task(hub.call(hub.list_workers()[0]["guid"], "demo.stream", {}, "open", user=alice))
            request = decode(await link.recv())
            await link.send(encode({"type": "stream_open", "id": request["id"], "stream": "s1", "title": "t",
                                    "description": "d"}))
            name = (await call)["stream"]
            await link.send(encode({"type": "stream_data", "stream": "s1", "data": "/+5vawrinJM="}))
            read = await hub.streams.read(name, 0, wait=5)
            # Bytes that are not UTF-8 show as \xNN; the rest reads as text.
            assert read["entries"] == [{"position": 0, "out": "\\xff\\xeeok\n✓"}]
            link.close()
            await eventually(lambda: not hub._links)
            try:
                await hub.streams.close(name, "done", user=alice)
                raise AssertionError("closed with the worker offline")
            except HubError as e:
                assert e.message["code"] == "worker_offline" and e.message["next"]
            assert hub.streams.get(name)["open"]
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())
