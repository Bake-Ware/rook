"""Streams across a worker's restart (D5; docs/link-design.md sections 9, 10 row L6 and 11 item 10;
S2, S4, U8), over memory links. A worker heartbeats `open_streams`, the handles of every stream it
opened and still has open; the hub closes every other open stream that worker opened, as closed by
the worker. A heartbeat without the field closes nothing. A long call's stream (the hub's own) and a
stream on another worker are never closed this way."""

import asyncio
import json

import rook_hub
from helpers import Clock, eventually
from memory_link import connector
from rook_hub import Hub
from rook_protocol import decode, encode
from rook_worker import Cap, Worker
from test_hub import SHELL, approved, connected, heartbeat

SLOW = {"name": "demo.slow", "description": "Slow.", "schema": {"type": "object"}, "returns": "result"}
CAPS = (SHELL, SLOW)


def closes(hub, guid):
    """The journal's close entries for stream `guid`."""
    rows = hub._db.execute("SELECT * FROM journal WHERE kind = 'stream' ORDER BY id").fetchall()
    return [json.loads(r["result"]) for r in rows if json.loads(r["result"]).get("closed") == guid]


def close_events(hub, guid):
    """The stream.close events (J2) emitted for stream `guid`, as `hub.emitted` recorded them."""
    return [payload for name, payload in hub.emitted if name == "stream.close" and payload["stream"] == guid]


def recording(hub):
    """Keep every event the hub emits in `hub.emitted`, then emit it as usual."""
    hub.emitted, emit = [], hub.events.emit

    def emitting(name, payload, **who):
        hub.emitted.append((name, payload))
        return emit(name, payload, **who)
    hub.events.emit = emitting


async def open_stream(hub, link, worker, handle):
    """A call on shell.open that the hand-driven worker answers with stream_open `handle`."""
    call = asyncio.create_task(hub.call(worker, "shell.open", {}, "open a terminal", user=hub.bake))
    message = decode(await link.recv())
    await link.send(encode({"type": "stream_open", "id": message["id"], "stream": handle,
                            "title": f"terminal {handle}", "description": ""}))
    return hub.streams.get((await call)["stream"])


async def beat(hub, link, key, **extra):
    """One heartbeat, and its reply: everything the hub does with it is done when the reply comes."""
    await link.send(encode(heartbeat(key, caps=CAPS, **extra)))
    assert decode(await link.recv())["type"] == "heartbeat_reply"


def test_open_streams_close_only_what_the_worker_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(rook_hub, "CALL_WAIT", 0.2)
    hub = Hub(tmp_path / "hub", clock=Clock())
    hub.bake = hub.auth.create_user("Bake", "a password")["guid"]
    recording(hub)
    try:
        asyncio.run(lost(hub))
    finally:
        hub.close()


async def lost(hub):
    key, box = approved(hub, "box")
    other_key, other = approved(hub, "other")
    link, _ = await connected(hub, key, caps=CAPS)
    other_link, _ = await connected(hub, other_key, caps=CAPS)
    kept = await open_stream(hub, link, box["moniker"], "s1")
    gone = await open_stream(hub, link, box["moniker"], "s2")
    theirs = await open_stream(hub, other_link, other["moniker"], "s2")     # the same handle, on another worker

    # A long call on box gets a stream of its own (the hub's, Bake 13 Nov): box never lists it.
    slow = asyncio.create_task(hub.call_or_follow(box["moniker"], "demo.slow", {}, "a slow build", user=hub.bake))
    slow_id = decode(await link.recv())["id"]
    running = await slow
    assert running["type"] == "running"
    call_stream = hub.streams.get(running["stream"])

    # 1. A heartbeat without the field closes nothing (an older worker, U8).
    await beat(hub, link, key)
    assert all(hub.streams.get(s["guid"])["open"] for s in (kept, gone, theirs, call_stream))

    # 2. A heartbeat that lists s1 only: s2, which box opened and lost, is closed as closed by the worker,
    #    with the same record, journal entry and event as box's own stream_close. A reader following it
    #    sees it end.
    following = asyncio.create_task(hub.streams.read(gone["guid"], 0, wait=5))
    await asyncio.sleep(0.05)
    assert not following.done()
    await beat(hub, link, key, open_streams=["s1"])
    read = await asyncio.wait_for(following, 2)
    assert read["open"] is False and read["entries"] == []
    record = hub.streams.get(gone["guid"])
    assert record["open"] is False and record["closed_by"] == "worker" and record["closed_at"]
    assert closes(hub, gone["guid"]) == [{"closed": gone["guid"], "title": "terminal s2", "closed_by": "worker"}]
    assert [e["by"] for e in close_events(hub, gone["guid"])] == ["worker"]

    # The one it lists stays open and keeps carrying data; the other worker's s2 and the long call's
    # stream are untouched.
    await link.send(encode({"type": "stream_data", "stream": "s1", "data": "aGVsbG8K"}))     # "hello\n"
    await eventually(lambda: hub._db.execute("SELECT count(*) FROM stream_data").fetchone()[0] == 1)
    assert (await hub.streams.read(kept["guid"]))["entries"] == [{"position": 0, "out": "hello\n"}]
    assert hub.streams.get(kept["guid"])["open"] and hub.streams.get(theirs["guid"])["open"]
    assert hub.streams.get(call_stream["guid"])["open"]

    # 3. The same, closed by box's own stream_close: the record and the journal entry are the same.
    by_itself = await open_stream(hub, link, box["moniker"], "s3")
    await link.send(encode({"type": "stream_close", "stream": "s3"}))
    await eventually(lambda: not hub.streams.get(by_itself["guid"])["open"])
    assert hub.streams.get(by_itself["guid"])["closed_by"] == "worker"
    assert [e["by"] for e in close_events(hub, by_itself["guid"])] == ["worker"]
    assert closes(hub, by_itself["guid"]) == [{"closed": by_itself["guid"], "title": "terminal s3",
                                              "closed_by": "worker"}]

    # 4. A stream opened and closed between two heartbeats is not closed twice: the next heartbeat,
    #    which does not list it, finds it closed already.
    await beat(hub, link, key, open_streams=["s1"])
    assert len(closes(hub, by_itself["guid"])) == 1 and len(close_events(hub, by_itself["guid"])) == 1
    assert len(closes(hub, gone["guid"])) == 1

    # 5. An empty list closes s1 too, and still not the long call's stream nor the other worker's.
    await beat(hub, link, key, open_streams=[])
    assert hub.streams.get(kept["guid"])["closed_by"] == "worker"
    assert hub.streams.get(call_stream["guid"])["open"] and hub.streams.get(theirs["guid"])["open"]

    # The long call ends as usual: its reply is its stream's last entry, and the hub closes it.
    await link.send(encode({"type": "result", "id": slow_id, "result": {"built": True}}))
    await eventually(lambda: not hub.streams.get(call_stream["guid"])["open"])
    assert hub.streams.get(call_stream["guid"])["closed_by"] == "hub"
    link.close(), other_link.close()


# ------------------------------------------------------------------ the real worker

terminals = []


async def terminal(args, stream):
    """A stand-in terminal: it ends when told to through its input."""
    terminals.append(stream)

    async def run():
        while (data := await stream.read()) is not None:
            if data == b"exit\n":
                await stream.close()
    asyncio.create_task(run())
    return {"title": "terminal", "description": "a shell"}


TERMINAL = {"shell.open": Cap("Open a terminal.", {"type": "object"}, terminal, opens_stream=True)}


def last_open_streams(hub, guid):
    body = hub._db.execute("SELECT body FROM heartbeats WHERE worker = ? ORDER BY rowid DESC LIMIT 1",
                           (guid,)).fetchone()[0]
    return json.loads(body)["open_streams"]


def test_a_restarted_worker_loses_its_streams(tmp_path):
    terminals.clear()
    asyncio.run(restarted(tmp_path))


async def restarted(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    bake = hub.auth.create_user("Bake", "a password")["guid"]
    worker = Worker(tmp_path / "w", f"http://{host}:{port}", connector(hub), name="box", caps=TERMINAL, macs=[],
                    knock_every=0.02)
    running = asyncio.create_task(worker.run())
    again = None
    try:
        await eventually(hub.list_pending)
        box = hub.approve(worker.key, user=bake)
        await eventually(lambda: hub.get_worker(box["moniker"])["online"])

        # The real worker's heartbeats carry the list: empty, then with a terminal open, then without
        # it once the terminal has ended.
        assert last_open_streams(hub, box["guid"]) == []
        first = hub.streams.get((await hub.call(box["moniker"], "shell.open", {}, "a terminal", user=bake))["stream"])
        handle = hub._db.execute("SELECT handle FROM streams WHERE guid = ?", (first["guid"],)).fetchone()[0]
        await eventually(lambda: last_open_streams(hub, box["guid"]) == [handle])
        await hub.streams.write(first["guid"], "exit\n", user=bake)
        await eventually(lambda: last_open_streams(hub, box["guid"]) == [])
        assert hub.streams.get(first["guid"])["closed_by"] == "worker"
        assert len(closes(hub, first["guid"])) == 1       # its stream_close, and nothing more

        # Two terminals open; the worker stops and starts again with the same identity. The streams
        # outlive its link (S4) until the restarted worker's first heartbeat, which lists neither.
        second = hub.streams.get((await hub.call(box["moniker"], "shell.open", {}, "a terminal", user=bake))["stream"])
        third = hub.streams.get((await hub.call(box["moniker"], "shell.open", {}, "a terminal", user=bake))["stream"])
        await eventually(lambda: len(last_open_streams(hub, box["guid"])) == 2)
        following = asyncio.create_task(hub.streams.read(second["guid"], 0, wait=10))
        worker.stop()
        await asyncio.gather(running, return_exceptions=True)
        await eventually(lambda: box["guid"] not in hub._links)
        assert hub.streams.get(second["guid"])["open"] and hub.streams.get(third["guid"])["open"]
        assert not following.done()

        again = Worker(tmp_path / "w", f"http://{host}:{port}", connector(hub), name="box", caps=TERMINAL, macs=[])
        assert again.key == worker.key
        running = asyncio.create_task(again.run())
        read = await asyncio.wait_for(following, 5)
        assert read["open"] is False
        for stream in (second, third):
            record = hub.streams.get(stream["guid"])
            assert record["open"] is False and record["closed_by"] == "worker"
            assert len(closes(hub, stream["guid"])) == 1
        assert last_open_streams(hub, box["guid"]) == []

        # The restarted worker opens a new terminal; it stays open across its heartbeats.
        fourth = hub.streams.get((await hub.call(box["moniker"], "shell.open", {}, "a terminal", user=bake))["stream"])
        await eventually(lambda: len(last_open_streams(hub, box["guid"])) == 1)
        beats = hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0]
        again.changed()                    # one more heartbeat, which lists it
        await eventually(lambda: hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0] > beats)
        assert hub.streams.get(fourth["guid"])["open"]
    finally:
        (again or worker).stop()
        await asyncio.gather(running, return_exceptions=True)
        await hub.stop_serving()
        hub.close()
