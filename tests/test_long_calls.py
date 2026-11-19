"""Calls that run long (C1, C3, S1 to S4, L1, L4; Bake 13 Nov): through the real MCP door with a
real rook_worker on an in-memory link. The wait is shortened from 45 seconds to half a second."""

import asyncio
import base64
import json
import os

import rook_hub
from helpers import eventually
from memory_link import connector
from rook_hub import Hub
from rook_worker import Cap, Worker
from test_door import mcp_client, tool
from test_first_run import ROOT, Started, start_command
from test_site import ask, browser, login

SECRET = "hunter2-correct-horse-battery"
WAIT = 0.5


class Slow:
    """A worker's caps: one that answers at once, one that runs until the test lets it end, and a
    stream cap. Counts every run of the slow handler."""

    def __init__(self):
        self.runs, self.gate, self.cancelled = [], asyncio.Event(), []

    def caps(self):
        async def quick(args):
            return {"quick": True}

        async def slow(args):
            self.runs.append(args)
            try:
                await self.gate.wait()
            except asyncio.CancelledError:      # stopped (brief 59)
                self.cancelled.append(args)
                raise
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
    asyncio.run(scenario(tmp_path, monkeypatch))


async def scenario(tmp_path, monkeypatch):
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

            # The stream takes no input, no resize and no signal (closing it stops the call: see 7).
            for action, extra in (("write", {"text": "go faster\n"}), ("resize", {"cols": 80, "rows": 24}),
                                  ("signal", {"signal": "INT"})):
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

            # 6. The same through the site: POST /api/call, as a page's call() sends it, goes through the
            #    same code. Quick: the reply as before. Slow: "running" and a stream, followed through the
            #    site's own stream reads, whose last entry is the reply.
            async with browser() as client:
                assert (await login(client, base, "Alice", "a password"))[0] == 200
                status, quick = await ask(client, "POST", f"{base}/api/call",
                                          {"worker": box, "cap": "demo.quick", "args": {}, "why": "quick, from a page"})
                assert status == 200 and quick["type"] == "result" and quick["result"] == {"quick": True}
                slow.gate.clear()
                status, answer = await ask(client, "POST", f"{base}/api/call", {
                    "worker": box, "cap": "demo.slow", "args": {"what": "from a page"}, "why": "built from a page"})
                assert status == 200 and set(answer) == {"type", "id", "worker", "stream"}, answer
                assert answer["type"] == "running" and answer["worker"] == box
                slow.gate.set()
                position, last, still = 0, None, True
                while still:
                    status, read = await ask(client, "GET", f"{base}/api/streams/{answer['stream']}/read?from={position}&wait=5")
                    assert status == 200, read
                    last = read["entries"][-1] if read["entries"] else last
                    position, still = read["next"], read["open"]
                reply = json.loads(base64.b64decode(last["data"]))
                assert reply == {"type": "result", "id": answer["id"], "result": {"built": "from a page", "with": None}}
                assert len(slow.runs) == 4                     # three through the door, one from the page
                [entry] = [e for e in await journal_of("demo.slow") if e["result"].get("id") == answer["id"]]
                assert entry["result"] == reply and entry["why"] == "built from a page"

            # 7. Stopped (brief 59). A call whose caller is still waiting, with no stream yet: the caller
            #    gets call_stopped at once, and the handler on the worker is cancelled.
            slow.gate.clear()
            waiting = asyncio.ensure_future(hub.call(box, "demo.slow", {"what": "never"}, "for a job",
                                                     user=alice["guid"]))
            await eventually(lambda: len(slow.runs) == 5)
            [call_id] = [c for c, r in hub._running.items() if r["cap"] == "demo.slow"]
            stopped = hub.stop_call(call_id, (alice["guid"], None), "to test stopping")
            reply = await waiting
            assert reply == stopped and reply["code"] == "call_stopped" and reply["id"] == call_id
            assert reply["message"] == f"Alice ({alice['moniker']}) stopped this call, to test stopping."
            await eventually(lambda: slow.cancelled == [{"what": "never"}])
            assert call_id not in hub._running and not hub._stops
            failed, text = await tool(door, "journal", text=call_id)
            assert text["entries"][0]["result"] == reply
            refused = None
            try:
                hub.stop_call(call_id, (alice["guid"], None), "again")
            except rook_hub.HubError as e:
                refused = e.message
            assert refused["code"] == "call_not_running" and refused["next"]
            #    The worker goes on serving.
            failed, answer = await call("demo.quick", "still there")
            assert not failed and answer == {"quick": True}

            # 8. A call made inside another (a device's call, made through a worker, D1): stopping the outer
            #    one ends both, each with call_stopped, and the inner one's worker ends its work. A stand-in
            #    device here: its call is a call to demo.slow on the worker.
            async def through_worker(hub_, row, cap, args):
                return hub_.joined(await hub_.call(box, "demo.slow", args, "for the device", user=alice["guid"]))
            monkeypatch.setattr(rook_hub.devices, "find", lambda hub_, worker: {"guid": "lamp"} if worker == "lamp" else None)
            monkeypatch.setattr(rook_hub.devices, "call", through_worker)
            outer = asyncio.ensure_future(hub.call("lamp", "lamp.on", {"what": "inner"}, "turn it on",
                                                   user=alice["guid"]))
            await eventually(lambda: len(slow.runs) == 6)
            ids = {r["cap"]: c for c, r in hub._running.items()}
            reply = hub.stop_call(ids["lamp.on"], (alice["guid"], None), "to test a nested stop")
            assert await outer == reply
            await eventually(lambda: slow.cancelled[-1] == {"what": "inner"})
            failed, text = await tool(door, "journal", text=ids["demo.slow"])
            inner = text["entries"][0]["result"]
            assert inner["code"] == "call_stopped" and inner["id"] == ids["demo.slow"]
            assert inner["message"] == reply["message"] and not hub._running and not hub._stops
    finally:
        worker.stop()
        await asyncio.gather(running, return_exceptions=True)
        await hub.stop_serving()
        hub.close()


def test_one_hub_per_data_directory_and_a_restart(tmp_path, monkeypatch):
    """Only the hub that serves a data directory ends calls a previous run left running (C3).
    While a hub serves it with a long call running, the real start command, run twice (on a free
    port and on the same port), refuses and changes nothing; the call ends as it should. When that
    hub is gone mid-call, the real command takes the directory over and ends the call's stream with
    hub_restarted; the old call ending later adds nothing to the closed stream or the journal."""
    monkeypatch.setattr(rook_hub, "CALL_WAIT", WAIT)
    asyncio.run(one_hub_per_directory(tmp_path))


async def one_hub_per_directory(tmp_path):
    data, key = tmp_path / "data", tmp_path / "keys" / "vault.key"
    hub = Hub(data, vault_key_file=key)
    host, port = await hub.serve("127.0.0.1", 0)
    alice = hub.auth.create_user("Alice", "a password")
    slow = Slow()
    worker = Worker(tmp_path / "w", f"http://{host}:{port}", connector(hub), name="builder", caps=slow.caps(),
                    macs=[], knock_every=0.02)
    running = asyncio.create_task(worker.run())
    await eventually(lambda: hub.list_pending())
    box = hub.approve(worker.key, user=alice["guid"])["moniker"]
    await eventually(lambda: hub.get_worker(box)["online"])

    def entry(call_id):
        [row] = [json.loads(r[0]) for r in hub._db.execute("SELECT result FROM journal WHERE cap = 'demo.slow'")
                 if json.loads(r[0])["id"] == call_id]
        return row

    async def recording(stream):
        return [json.loads(e["out"]) for e in (await hub.streams.read(stream))["entries"]]

    async def command(*extra):
        args, env = start_command(data, key)
        process = await asyncio.create_subprocess_exec(*args, *extra, cwd=ROOT, env=env,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), 20)
        return process.returncode, err.decode()

    try:
        # 1. The real command, twice, while this hub serves the directory and a long call runs.
        answer = await hub.call_or_follow(box, "demo.slow", {"what": "nightly"}, "the nightly build", user=alice["guid"])
        assert answer["type"] == "running"
        for extra in ((), ("--port", str(port))):           # a free port, then this hub's own
            code, err = await command(*extra)
            assert code != 0 and "Another hub is serving" in err and "Stop that hub first" in err, err
        assert entry(answer["id"])["type"] == "running" and hub.streams.get(answer["stream"])["open"]
        assert await recording(answer["stream"]) == []
        slow.gate.set()                                       # the call ends as it should
        await eventually(lambda: not hub.streams.get(answer["stream"])["open"])
        [reply] = await recording(answer["stream"])
        assert reply["type"] == "result" and entry(answer["id"]) == reply
        assert len(slow.runs) == 1

        # 2. This hub is gone mid-call (its process ended, so the system let go of its lock). The real
        #    command takes the directory over and ends the call with an error saying what to try next.
        slow.gate.clear()
        answer = await hub.call_or_follow(box, "demo.slow", {"what": "weekly"}, "the weekly build", user=alice["guid"])
        assert answer["type"] == "running"
        hub._serving_dir.close()
        async with Started(data, key):
            [reply] = await recording(answer["stream"])
            assert reply["code"] == "hub_restarted" and reply["id"] == answer["id"] and "call again" in reply["next"]
            assert hub.streams.get(answer["stream"])["closed_by"] == "hub"
            assert entry(answer["id"]) == reply
        # The old call ending afterwards adds nothing: the stream and the journal keep the error.
        slow.gate.set()
        await asyncio.sleep(0.3)
        assert await recording(answer["stream"]) == [reply] and entry(answer["id"]) == reply
        assert len(slow.runs) == 2                           # nothing was sent to the worker again
    finally:
        slow.gate.set()
        worker.stop()
        await asyncio.gather(running, return_exceptions=True)
        await hub.stop_serving()
        hub._serving_dir = None
        hub.close()
