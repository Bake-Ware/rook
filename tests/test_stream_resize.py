"""Terminal size (Bake 13 Nov; S1, S2, S3, S4, C2, P8, W1, U8): a terminal cap opened with cols
and rows, and resizes sent through the real MCP door and through the site to a real rook_worker
on an in-memory link. A resize reaches the stream's handler and nowhere else: not the recording,
not the input. The last resize sent wins."""

import asyncio

import rook_hub
from helpers import eventually
from memory_link import connector
from rook_hub import Hub
from rook_worker import Cap, Worker
from test_door import mcp_client, tool, until
from test_site import ask, browser, login

SIZE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "cols": {"type": "integer", "minimum": 1}, "rows": {"type": "integer", "minimum": 1}}}
opened = {}     # the worker's Stream objects by title, so the test can see what the handler was told


async def terminal(args, stream):
    """A sample terminal: it reports the size it was opened at and every resize it receives."""
    async def run():
        await stream.send(f"opened at {args.get('cols')}x{args.get('rows')}\r\n")
        while (size := await stream.resized()) is not None:
            await stream.send(f"resized to {size[0]}x{size[1]}\r\n")
    opened["terminal"] = stream
    asyncio.create_task(run())
    return {"title": "sample terminal", "description": "Reports the size it is told."}


async def echo(args, stream):
    """A stream whose handler takes no resizes: it echoes input."""
    async def run():
        while (data := await stream.read()) is not None:
            await stream.send(b"heard: " + data)
    opened["echo"] = stream
    asyncio.create_task(run())
    return {"title": "echo", "description": "Echoes input; knows nothing of sizes."}


async def slow(args):
    await asyncio.sleep(1.0)
    return {"done": True}


CAPS = {"term.open": Cap("Open a sample terminal.", SIZE_SCHEMA, terminal, opens_stream=True),
        "echo.open": Cap("Open a stream that echoes input.", {"type": "object"}, echo, opens_stream=True),
        "demo.slow": Cap("Answer after a second.", {"type": "object"}, slow)}


def test_stream_resize(tmp_path, monkeypatch):
    monkeypatch.setattr(rook_hub, "CALL_WAIT", 0.2)      # so demo.slow becomes a long call with a stream
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    bob = hub.auth.create_user("Bob", "battery staple")
    worker = Worker(tmp_path / "box", base, connector(hub), name="box", caps=dict(CAPS), macs=[], knock_every=0.02)
    running = asyncio.create_task(worker.run())
    await eventually(lambda: any(k["key"] == worker.key for k in hub.list_pending()))
    box = hub.approve(worker.key, user=alice["guid"])["moniker"]
    await eventually(lambda: hub.get_worker(box)["online"])

    def recording(stream):
        with hub._lock:
            return [(r["direction"], bytes(r["data"]).decode()) for r in hub._db.execute(
                "SELECT direction, data FROM stream_data s JOIN streams t ON t.guid = s.stream "
                "WHERE t.moniker = ? ORDER BY position", (stream,))]

    try:
        async with mcp_client(f"{base}/mcp", hub.auth.mint("agent", user=alice["guid"])) as one, \
                mcp_client(f"{base}/mcp", hub.auth.mint("agent", user=bob["guid"])) as two:
            # 1. A terminal cap opened with cols and rows: they are ordinary arguments, passed on.
            failed, term = await tool(one, "call", worker=box, cap="term.open", args={"cols": 132, "rows": 43},
                                      why="open a sample terminal")
            assert not failed, term
            stream = term["stream"]
            await until(lambda: asyncio.sleep(0, recording(stream) == [("out", "opened at 132x43\r\n")]))

            # 2. A resize through the door reaches the handler with the new size; not the recording,
            #    and not the journal.
            with hub._lock:
                journaled = hub._db.execute("SELECT count(*) FROM journal").fetchone()[0]
            failed, reply = await tool(one, "stream", action="resize", stream=stream, cols=100, rows=30)
            assert not failed and reply == {"resized": stream, "cols": 100, "rows": 30}
            await until(lambda: asyncio.sleep(0, len(recording(stream)) == 2))
            assert recording(stream) == [("out", "opened at 132x43\r\n"), ("out", "resized to 100x30\r\n")]
            assert opened["terminal"].size == (100, 30)
            with hub._lock:
                assert hub._db.execute("SELECT count(*) FROM journal").fetchone()[0] == journaled

            # 3. Two clients at different sizes: their resizes arrive in order, and the last one stands.
            failed, _ = await tool(two, "stream", action="resize", stream=stream, cols=80, rows=24)
            assert not failed
            await until(lambda: asyncio.sleep(0, len(recording(stream)) == 3))
            failed, _ = await tool(one, "stream", action="resize", stream=stream, cols=200, rows=50)
            failed, _ = await tool(two, "stream", action="resize", stream=stream, cols=90, rows=25)
            await until(lambda: asyncio.sleep(0, opened["terminal"].size == (90, 25)))
            await until(lambda: asyncio.sleep(0, recording(stream)[-1] == ("out", "resized to 90x25\r\n")))
            reports = [text for _, text in recording(stream)]
            assert reports[:3] == ["opened at 132x43\r\n", "resized to 100x30\r\n", "resized to 80x24\r\n"]
            assert reports[-1] == "resized to 90x25\r\n"
            # The recording, as any reader gets it, holds only what the worker sent: no resize, no input.
            failed, read = await tool(one, "stream", action="read", stream=stream)
            assert [e["out"] for e in read["entries"]] == reports and all(set(e) == {"position", "out"}
                                                                          for e in read["entries"])

            # 4. Sizes are checked as a message needs them: whole numbers of 1 or more.
            for cols, rows in ((0, 24), (80, -1), (80.5, 24), ("80", 24), (True, 24), (None, 24)):
                failed, text = await tool(one, "stream", action="resize", stream=stream,
                                          **{k: v for k, v in (("cols", cols), ("rows", rows)) if v is not None})
                assert failed and "bad_message" in text, (cols, rows, text)

            # 5. A worker whose handler takes no resizes keeps working when one arrives.
            failed, echoed = await tool(one, "call", worker=box, cap="echo.open", args={}, why="open an echo")
            failed, reply = await tool(one, "stream", action="resize", stream=echoed["stream"], cols=80, rows=24)
            assert not failed
            failed, _ = await tool(one, "stream", action="write", stream=echoed["stream"], text="ping")
            await until(lambda: asyncio.sleep(0, recording(echoed["stream"]) == [("in", "ping"), ("out", "heard: ping")]))
            failed, again = await tool(one, "call", worker=box, cap="term.open", args={}, why="a second terminal")
            assert not failed

            # 6. Refused, with what to do: a long call's stream, a closed stream, an offline worker.
            failed, long = await tool(one, "call", worker=box, cap="demo.slow", args={}, why="a long call")
            assert not failed and long["running"], long
            failed, text = await tool(one, "stream", action="resize", stream=long["stream"], cols=80, rows=24)
            assert failed and text.startswith("call_stream:") and "Next:" in text
            failed, _ = await tool(one, "stream", action="close", stream=echoed["stream"], why="done with it")
            failed, text = await tool(one, "stream", action="resize", stream=echoed["stream"], cols=80, rows=24)
            assert failed and text.startswith("stream_closed:") and "Next:" in text

            # 7. The same resize through the site, as the logged-in person; the site knows it is a terminal.
            async with browser() as client:
                assert (await ask(client, "POST", f"{base}/api/streams/{stream}/resize", {"cols": 80, "rows": 24}))[0] == 401
                assert (await login(client, base, "Alice", "correct horse"))[0] == 200
                assert (await ask(client, "GET", f"{base}/api/streams/{stream}"))[1]["stream"]["terminal"] is True
                status, view = await ask(client, "GET", f"{base}/api/streams/{echoed['stream']}")
                assert view["stream"]["terminal"] is False                    # its cap takes no cols and rows
                assert (await ask(client, "GET", f"{base}/api/streams/{long['stream']}"))[1]["stream"]["terminal"] is False
                status, reply = await ask(client, "POST", f"{base}/api/streams/{stream}/resize", {"cols": 120, "rows": 40})
                assert status == 200 and reply == {"resized": stream, "cols": 120, "rows": 40}
                await until(lambda: asyncio.sleep(0, recording(stream)[-1] == ("out", "resized to 120x40\r\n")))
                status, reply = await ask(client, "POST", f"{base}/api/streams/{stream}/resize", {"cols": 0, "rows": 40})
                assert status == 400 and reply["code"] == "bad_message"

            # The worker goes offline: a resize says so.
            worker_task, running = running, None
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            await eventually(lambda: hub._links.get(hub.get_worker(box)["guid"]) is None)
            failed, text = await tool(one, "stream", action="resize", stream=again["stream"], cols=80, rows=24)
            assert failed and text.startswith("worker_offline:") and "Next:" in text
    finally:
        if running is not None:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await hub.stop_serving()
        hub.close()
