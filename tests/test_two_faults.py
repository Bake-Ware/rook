"""Faults 5 and 6 of docs/stress.md, against C3 (every refusal says what happened and what to do
next), with a real rook_worker on an in-memory link:

- a link that closes under a stream write, resize or close answers `worker_disconnected` with a
  next step, as JSON-RPC through the MCP door and as JSON on the site, not HTTP 500;
- a result over the message size is refused in a whole sentence naming both sizes, with a next
  step that helps; a `cap_failed` whose cause is not the arguments does not point at the schema."""

import asyncio

from helpers import eventually
from memory_link import connector
from rook_hub import Hub
from rook_protocol import MAX_MESSAGE, encode
from rook_worker import Cap, Worker
from test_door import mcp_client, tool
from test_site import ask, browser, login

BIG = MAX_MESSAGE + 1      # characters of result: over the message size once encoded


async def echo(args, stream):
    async def run():
        while (data := await stream.read()) is not None:
            await stream.send(b"heard: " + data)
    asyncio.create_task(run())
    return {"title": "echo", "description": "Echoes input."}


async def huge(args):
    return "a" * BIG


async def no_battery(args):
    raise ValueError("this worker has no battery")


CAPS = {"echo.open": Cap("Open a stream that echoes input.", {"type": "object"}, echo, opens_stream=True),
        "demo.huge": Cap("A result over the message size.", {"type": "object"}, huge),
        "demo.flat": Cap("Fails for a reason that is not its arguments.", {"type": "object"}, no_battery),
        "demo.flat.open": Cap("A stream cap that fails the same way.", {"type": "object"},
                              lambda args, stream: no_battery(args), opens_stream=True)}


async def start(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    alice = hub.auth.create_user("Alice", "correct horse")
    worker = Worker(tmp_path / "box", f"http://{host}:{port}", connector(hub), name="box", caps=dict(CAPS),
                    macs=[], knock_every=0.02)
    running = asyncio.create_task(worker.run())
    await eventually(lambda: any(k["key"] == worker.key for k in hub.list_pending()))
    box = hub.approve(worker.key, user=alice["guid"])
    await eventually(lambda: hub.get_worker(box["guid"])["online"])
    return hub, f"http://{host}:{port}", alice, worker, running, box


def close_under_send(hub, guid):
    """The next send on `guid`'s link finds it closing: it closes and raises EOFError, as
    link.py does when the Channel resets mid-send."""
    link = hub._links[guid]

    async def send(data):
        link.close()
        raise EOFError
    link.send = send


def test_link_closed_under_a_stream_write(tmp_path):
    async def go():
        hub, base, alice, worker, running, box = await start(tmp_path)
        try:
            async with mcp_client(f"{base}/mcp", hub.auth.mint("agent", user=alice["guid"])) as mcp:
                failed, opened = await tool(mcp, "call", worker=box["moniker"], cap="echo.open", args={},
                                            why="a stream to write to")
                assert not failed, opened
                stream = opened["stream"]

                # Through the door: write, resize and close each answer the code and a next step.
                for action, extra, what, again in (
                        ("write", {"text": "hello\n"}, "the input", "write again"),
                        ("resize", {"cols": 80, "rows": 24}, "the new size", "resize again"),
                        ("close", {"why": "done with it"}, "the close", "close it again")):
                    await eventually(lambda: hub._links.get(box["guid"]) is not None)
                    close_under_send(hub, box["guid"])
                    failed, text = await tool(mcp, "stream", action=action, stream=stream, **extra)
                    assert failed and text == (
                        f"worker_disconnected: The link to the worker of stream {stream} closed before {what} "
                        f"was sent. Next: Check whether the worker is online, then {again}."), (action, text)

                # The worker links again by itself; the stream is still open, and a write goes through.
                await eventually(lambda: hub._links.get(box["guid"]) is not None)
                failed, wrote = await tool(mcp, "stream", action="write", stream=stream, text="again\n")
                assert not failed and wrote == {"written": 6}

            # On the site: the same refusal as JSON, not HTTP 500.
            async with browser() as client:
                await login(client, base, "Alice", "correct horse")
                await eventually(lambda: hub._links.get(box["guid"]) is not None)
                close_under_send(hub, box["guid"])
                status, body = await ask(client, "POST", f"{base}/api/streams/{stream}/input", {"text": "hi\n"})
                assert status == 400 and body["code"] == "worker_disconnected" and body["next"], (status, body)
        finally:
            worker.stop()
            await running
            await hub.stop_serving()
    asyncio.run(go())


def test_cap_failed_wording(tmp_path):
    async def go():
        hub, base, alice, worker, running, box = await start(tmp_path)
        try:
            # Over the message size: both sizes, a full stop, and a next step that helps.
            reply = await hub.call(box["moniker"], "demo.huge", {}, "a result too big", user=alice["guid"])
            size = len(encode({"type": "result", "id": reply["id"], "result": "a" * BIG}))
            assert reply["code"] == "cap_failed", reply
            assert reply["message"] == (f"demo.huge failed: its result is {size} bytes encoded, "
                                        f"and a message may be at most {MAX_MESSAGE} bytes.")
            assert reply["next"] == ("Ask for less at once, or have the result written to a file on box "
                                     "and read that file in parts.")

            # A failure that is not the arguments' fault, for a plain cap and a stream cap: the
            # reason ends in a full stop, and the next step does not name the schema.
            for cap in ("demo.flat", "demo.flat.open"):
                reply = await hub.call(box["moniker"], cap, {}, "no battery here", user=alice["guid"])
                assert reply["code"] == "cap_failed", reply
                assert reply["message"] == f"{cap} failed: this worker has no battery."
                assert reply["next"] == (f"Change what the reason names and call again, or call {cap} on "
                                         "another worker that announces it.")
                assert "schema" not in reply["next"]

            # Through the door, the text an agent reads.
            async with mcp_client(f"{base}/mcp", hub.auth.mint("agent", user=alice["guid"])) as mcp:
                failed, text = await tool(mcp, "call", worker=box["moniker"], cap="demo.flat", args={},
                                          why="no battery here")
                assert failed and text == ("cap_failed: demo.flat failed: this worker has no battery. Next: Change "
                                           "what the reason names and call again, or call demo.flat on another "
                                           "worker that announces it."), text
        finally:
            worker.stop()
            await running
            await hub.stop_serving()
    asyncio.run(go())
