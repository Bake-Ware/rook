"""Calls that run long (C1, C3, S1 to S4, L1, L4; Bake 13 Nov): through the real MCP door with a
real rook_worker on an in-memory link. The wait is shortened from 45 seconds to half a second."""

import asyncio
import json
import os

import rook_hub
from helpers import eventually
from memory_link import connector
from rook_hub import Hub
from rook_worker import Cap, Worker
from test_door import mcp_client, tool

SECRET = "hunter2-correct-horse-battery"
WAIT = 0.5


class Slow:
    """A worker's caps: one that answers at once, one that runs until the test lets it end, and a
    stream cap. Counts every run of the slow handler."""

    def __init__(self):
        self.runs, self.gate = [], asyncio.Event()

    def caps(self):
        async def quick(args):
            return {"quick": True}

        async def slow(args):
            self.runs.append(args)
            await self.gate.wait()
            if args.get("fail"):
                raise RuntimeError("the build broke at step 3")
            return {"built": args.get("what"), "with": args.get("key")}

        async def shell(args, stream):
            return {"title": "a shell", "description": "opened at once"}

        return {"demo.quick": Cap("Answer at once.", {"type": "object"}, quick),
                "demo.slow": Cap("Take as long as the test likes.", {"type": "object"}, slow),
                "demo.shell": Cap("Open a stream at once.", {"type": "object"}, shell, opens_stream=True)}


def test_long_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(rook_hub, "CALL_WAIT", WAIT)
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub", vault_key=os.urandom(32))
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "a password")
    agent = hub.auth.mint("agent", user=alice["guid"])
    slow = Slow()
    worker = Worker(tmp_path / "w", base, connector(hub), name="builder", caps=slow.caps(), macs=[], knock_every=0.02)
    running = asyncio.create_task(worker.run())
    await eventually(lambda: any(k["key"] == worker.key for k in hub.list_pending()))
    box = hub.approve(worker.key, user=alice["guid"])["moniker"]
    await eventually(lambda: hub.get_worker(box)["online"])
    hub.vault.create({"scope": "hub", "name": "build-key", "value": SECRET}, {"user": alice["guid"], "token": None})

    try:
        async with mcp_client(f"{base}/mcp", agent) as door, mcp_client(f"{base}/mcp", agent) as late:
            async def call(cap, why, **args):
                return await tool(door, "call", worker=box, cap=cap, args=args, why=why)

            async def journal_of(cap):
                failed, read = await tool(door, "journal", cap=cap)
                return read["entries"]

            async def follow(session, name):
                """Read a stream with waits until it closes, as the door tells an agent to."""
                entries, position = [], 0
                while True:
                    failed, read = await tool(session, "stream", action="read", stream=name, wait=5,
                                              **{"from": position})
                    assert not failed, read
                    entries, position = entries + read["entries"], read["next"]
                    if not read["open"]:
                        return entries

            # 1. A quick call answers as before, and no stream is made.
            failed, answer = await call("demo.quick", "a quick one")
            assert not failed and answer == {"quick": True}
            failed, listing = await tool(door, "stream", action="list")
            assert listing["streams"] == []
            [entry] = await journal_of("demo.quick")
            assert entry["result"]["type"] == "result" and entry["result"]["result"] == {"quick": True}

            # A stream cap that opens at once is unchanged: its stream_open is the answer (S1).
            failed, opened = await call("demo.shell", "open a shell")
            assert not failed and opened["title"] == "a shell" and "running" not in opened

            # 2. A call slower than the wait answers "still running" with a stream, and goes on.
            failed, answer = await call("demo.slow", "build the release", what="release 7", key="secret:build-key")
            assert not failed, answer
            assert answer["running"] is True and answer["worker"] == box and len(answer["id"]) == 32
            assert "Do not call again" in answer["next"] and answer["stream"] in answer["next"]
            name = answer["stream"]
            failed, record = await tool(door, "stream", action="list", open=True)
            [mine] = [s for s in record["streams"] if s["moniker"] == name]
            assert (mine["title"], mine["description"], mine["worker"], mine["cap"], mine["opened_by"]) == (
                f"demo.slow on {box}", "build the release", box, "demo.slow", alice["moniker"])
            failed, read = await tool(door, "stream", action="read", stream=name, **{"from": 0})
            assert read["entries"] == [] and read["open"] is True

            # The journal shows the call running, with its stream (L1).
            [entry] = await journal_of("demo.slow")
            assert entry["result"] == {"type": "running", "id": answer["id"], "stream": name}
            assert entry["worker"] == box and entry["why"] == "build the release" and entry["user"] == alice["moniker"]

            # The stream takes no input, and no client closes it.
            for action, extra in (("write", {"text": "go faster\n"}), ("close", {"why": "impatient"})):
                failed, refused = await tool(door, "stream", action=action, stream=name, **extra)
                assert failed and refused.startswith("call_stream:") and "Next:" in refused, refused

            # Following it starts nothing: the handler has run exactly once.
            following = asyncio.create_task(follow(door, name))
            await asyncio.sleep(WAIT * 2)
            assert len(slow.runs) == 1 and not following.done()

            # 3. The call ends: its reply is the stream's last entry, then the hub closes the stream (S4).
            slow.gate.set()
            entries = await following
            assert [list(e) for e in entries] == [["position", "out"]]
            reply = json.loads(entries[-1]["out"])
            assert reply == {"type": "result", "id": answer["id"],
                             "result": {"built": "release 7", "with": "(secret:build-key)"}}   # scrubbed (L4)
            assert len(slow.runs) == 1 and slow.runs[0]["key"] == SECRET    # the worker got the value (X2)
            failed, listing = await tool(door, "stream", action="list")
            [mine] = [s for s in listing["streams"] if s["moniker"] == name]
            assert mine["open"] is False and mine["closed_by"] == "hub"
            recorded = hub._db.execute("SELECT data FROM stream_data d JOIN streams s ON s.guid = d.stream "
                                       "WHERE s.moniker = ?", (name,)).fetchall()
            assert len(recorded) == 1 and SECRET.encode() not in recorded[0][0]
            # The journal: finished, with the same reply; and the hub's close, with why.
            [entry] = await journal_of("demo.slow")
            assert entry["result"] == reply
            failed, closes = await tool(door, "journal", kind="stream")
            assert closes["entries"][0]["result"]["closed_by"] == "hub" and "call ended" in closes["entries"][0]["why"]

            # 4. A reader who attaches after the end reads the reply from the recording.
            assert json.loads((await follow(late, name))[-1]["out"]) == reply

            # 5. The same for a call that ends in an error: the error the tool would have returned.
            slow.gate.clear()
            failed, answer = await call("demo.slow", "build a broken one", fail=True)
            assert answer["running"] is True
            slow.gate.set()
            reply = json.loads((await follow(door, answer["stream"]))[-1]["out"])
            assert reply["type"] == "error" and reply["code"] == "cap_failed" and reply["id"] == answer["id"]
            assert "the build broke at step 3" in reply["message"] and reply["next"]
            [entry] = [e for e in await journal_of("demo.slow") if e["result"].get("id") == answer["id"]]
            assert entry["result"] == reply
            assert len(slow.runs) == 2

            # The same failure as a quick call: the door's error carries the same code, message and next.
            failed, text = await call("demo.slow", "a broken one, quickly", fail=True)
            assert failed and text == f"cap_failed: {reply['message']} Next: {reply['next']}"
    finally:
        worker.stop()
        await asyncio.gather(running, return_exceptions=True)
        await hub.stop_serving()
        hub.close()


def test_a_hub_restart_ends_a_long_call_with_an_error(tmp_path, monkeypatch):
    """The hub stops while a long call runs: when it starts again, the call's stream ends with an
    error that says what to try next, and the journal entry says the same (C3)."""
    monkeypatch.setattr(rook_hub, "CALL_WAIT", WAIT)

    async def go():
        hub = Hub(tmp_path / "hub")
        host, port = await hub.serve("127.0.0.1", 0)
        alice = hub.auth.create_user("Alice", "a password")
        slow = Slow()
        worker = Worker(tmp_path / "w", f"http://{host}:{port}", connector(hub), name="builder", caps=slow.caps(),
                        macs=[], knock_every=0.02)
        running = asyncio.create_task(worker.run())
        await eventually(lambda: hub.list_pending())
        box = hub.approve(worker.key, user=alice["guid"])["moniker"]
        await eventually(lambda: hub.get_worker(box)["online"])
        answer = await hub.call_or_follow(box, "demo.slow", {"what": "nightly"}, "the nightly build", user=alice["guid"])
        assert answer["type"] == "running"
        again = None
        try:
            # A new hub process on the same data directory; the old one is gone as far as it knows.
            again = Hub(tmp_path / "hub")
            read = await again.streams.read(answer["stream"])
            assert read["open"] is False
            reply = json.loads(read["entries"][-1]["out"])
            assert reply["code"] == "hub_restarted" and reply["id"] == answer["id"] and "call again" in reply["next"]
            assert again.streams.get(answer["stream"])["closed_by"] == "hub"
            [entry] = [r for r in again._db.execute("SELECT result FROM journal WHERE cap = 'demo.slow'")]
            assert json.loads(entry[0]) == reply
            assert len(slow.runs) == 1          # nothing was sent to the worker again
        finally:
            slow.gate.set()
            worker.stop()
            await asyncio.gather(running, return_exceptions=True)
            await asyncio.sleep(0.05)
            await hub.stop_serving()
            hub.close()
            if again is not None:
                again.close()
    asyncio.run(go())
