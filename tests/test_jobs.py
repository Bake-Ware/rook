"""Jobs and events (J1 to J8), driven through the MCP door and the webhook URL."""

import asyncio
import json
import time

import httpx2
import pytest

from helpers import Clock, eventually, link_fields
from memory_link import connector
from rook_hub import Hub
from rook_hub.jobs import schedule_due
from rook_worker import Cap, Worker
from test_door import mcp_client, refusal, tool


def journal(hub, **where):
    """Journal rows, oldest first, whose columns equal `where`."""
    rows = [dict(r) for r in hub._db.execute("SELECT * FROM journal ORDER BY id")]
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


def events(hub, name=None):
    found = [json.loads(r["result"]) for r in journal(hub, kind="event")]
    return [e for e in found if name is None or e["event"] == name]


def runs(hub, job):
    """The job's runs that have ended: an entry is written as its call starts and holds the reply once it
    ends, and a run read in between would be compared too early."""
    return [e for e in journal(hub, kind="job", job=job["guid"])
            if not e["result"] or json.loads(e["result"]).get("type") != "running"]


def test_jobs_and_events(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    clock = Clock(time.mktime((2026, 11, 12, 7, 10, 0, 0, 0, -1)))    # 07:10 on the hub's local clock
    hub = Hub(tmp_path / "hub", clock=clock)
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "a password")
    agent = hub.auth.mint("agent", user=alice["guid"])
    flaky = {"fail": False}
    running = []

    async def echo(args):
        return args

    async def maybe(args):
        if flaky["fail"]:
            raise RuntimeError("asked to fail")
        return {"ok": True}

    async def ring(args):
        await x.emit("doorbell", {"door": args.get("door", "front")})
        return {"rang": True}

    def worker(state, name, caps):
        return Worker(tmp_path / state, base, connector(hub), name=name, caps=caps, macs=[], knock_every=0.02)

    def run(w):
        running.append((w, asyncio.create_task(w.run())))

    y = worker("y", "box-y", {"demo.echo": Cap("Echo.", {}, echo), "demo.flaky": Cap("Fails when told.", {}, maybe)})
    run(y)
    await eventually(hub.list_pending)
    y_moniker = hub.approve(y.key, user=alice["guid"])["moniker"]
    await eventually(lambda: hub.get_worker(y_moniker)["online"])
    x = worker("x", "box-x", {"demo.ring": Cap("Ring the doorbell.", {}, ring)})
    hub.knock({"type": "knock", "protocol": 1, "name": "box-x", "key": x.key, "macs": [], "caps": [],
               **link_fields(x.signing_key)})
    x_moniker = hub.approve(x.key, user=alice["guid"])["moniker"]           # approved, not yet connected

    try:
        async with mcp_client(f"{base}/mcp", agent) as door:
            async def create(title, event, call, payload=None):
                failed, reply = await tool(door, "call", worker="hub", cap="job.create", why=f"create {title}", args={
                    "title": title, "description": f"test job {title}",
                    "match": {"event": event, **({"payload": payload} if payload else {})}, "call": call})
                assert not failed, reply
                return reply

            async def emit(name, payload=None):
                failed, reply = await tool(door, "call", worker="hub", cap="event.emit", why=f"emit {name}",
                                           args={"name": name, **({"payload": payload} if payload else {})})
                assert not failed, reply

            # 1. When X comes online, call a cap on Y with fixed arguments. The run is
            #    journaled as the job's, under its owner (J1, J2, J7, L1).
            online = (await create("greet", "worker.online",
                                   {"worker": y_moniker, "cap": "demo.echo", "args": {"hello": "x"}},
                                   {"worker_moniker": x_moniker}))["job"]
            assert online["owner"] == alice["moniker"]
            run(x)
            [entry] = await eventually(lambda: runs(hub, online))
            assert (entry["user"], entry["token"], entry["worker"], entry["cap"]) == (
                alice["guid"], None, hub.get_worker(y_moniker)["guid"], "demo.echo")
            assert json.loads(entry["result"])["result"] == {"hello": "x"}
            assert entry["why"].startswith(f"Job {online['moniker']} (greet)") and entry["run"]

            # 3. A second job matches a job's success, a third its failure; each chain
            #    shares one run ID, which the journal lists (J3, J5).
            base_job = (await create("base", "chain.start", {"worker": y_moniker, "cap": "demo.flaky"}))["job"]
            on_ok = (await create("on-ok", "job.success", {"worker": y_moniker, "cap": "demo.echo", "args": {"after": "ok"}},
                                  {"job_moniker": base_job["moniker"]}))["job"]
            on_fail = (await create("on-fail", "job.fail", {"worker": y_moniker, "cap": "demo.echo",
                                                             "args": {"after": "fail"}},
                                    {"job_moniker": base_job["moniker"]}))["job"]
            await emit("chain.start")
            [ok_run] = await eventually(lambda: runs(hub, on_ok))
            [first] = runs(hub, base_job)
            assert ok_run["run"] == first["run"] and runs(hub, on_fail) == []
            failed, chain = await tool(door, "journal", run=first["run"])
            assert [(e["kind"], e.get("job")) for e in reversed(chain["entries"])] == [
                ("job", base_job["moniker"]), ("event", base_job["moniker"]),
                ("job", on_ok["moniker"]), ("event", on_ok["moniker"])]
            flaky["fail"] = True
            await emit("chain.start")
            [fail_run] = await eventually(lambda: runs(hub, on_fail))
            assert fail_run["run"] == runs(hub, base_job)[1]["run"] != first["run"]
            assert len(runs(hub, on_ok)) == 1
            flaky["fail"] = False

            # 4. Two jobs that trigger each other: both create replies warn, the list
            #    warns, and both run (J4). L1 runs on any success and fails; L2 runs
            #    on L1's failure and succeeds, which runs L1 again.
            loop_one = await create("loop-1", "job.success", {"worker": "hub", "cap": "no.such-cap"})
            assert "loop" in loop_one["warnings"][0]
            loop_one = loop_one["job"]
            loop_two = await create("loop-2", "job.fail", {"worker": y_moniker, "cap": "demo.echo"},
                                    {"job_moniker": loop_one["moniker"]})
            assert loop_two["warnings"] and loop_one["moniker"] in loop_two["warnings"][0]
            loop_two = loop_two["job"]
            failed, listed = await tool(door, "call", worker="hub", cap="job.list", why="see the jobs")
            warned = {j["moniker"] for j in listed["jobs"] if "warning" in j}
            assert {loop_one["moniker"], loop_two["moniker"]} <= warned and base_job["moniker"] not in warned
            await emit("chain.start")                      # base succeeds, which starts the loop
            await eventually(lambda: len(runs(hub, loop_two)) >= 2)
            assert len(runs(hub, loop_one)) >= 2

            # 8. Paused, a job does not run; un-paused, it does. Deleted, it is gone
            #    from the table and the list, and its runs stay in the journal (J7).
            for job in (loop_one, loop_two):
                await tool(door, "call", worker="hub", cap="job.pause", args={"job": job["moniker"]}, why="stop the loop")
            await asyncio.sleep(0.2)                       # let runs already started finish
            before = len(runs(hub, loop_one))
            await emit("chain.start")
            await eventually(lambda: len(runs(hub, on_ok)) == 3)
            await asyncio.sleep(0.2)
            assert len(runs(hub, loop_one)) == before
            await tool(door, "call", worker="hub", cap="job.unpause", args={"job": loop_one["moniker"]}, why="once more")
            await emit("chain.start")
            await eventually(lambda: len(runs(hub, loop_one)) > before)
            for job in (loop_one, loop_two):
                failed, gone = await tool(door, "call", worker="hub", cap="job.delete", args={"job": job["moniker"]},
                                          why="the loop has made its point")
                assert gone == {"deleted": job["moniker"]}
            assert hub._db.execute("SELECT count(*) FROM jobs WHERE guid IN (?, ?)",
                                   (loop_one["guid"], loop_two["guid"])).fetchone()[0] == 0
            failed, listed = await tool(door, "call", worker="hub", cap="job.list", why="see the jobs")
            assert loop_one["moniker"] not in {j["moniker"] for j in listed["jobs"]}
            failed, history = await tool(door, "journal", job=loop_one["moniker"], kind="job")
            assert len(history["entries"]) >= before

            # 6. The webhook: a webhook token emits its event and a matching job runs;
            #    agent, join and missing tokens are refused there, and a webhook token
            #    is refused at the MCP door (J2, T2).
            hook = hub.auth.mint("webhook", user=alice["guid"])
            hook_moniker = hub.auth.verify(hook)["moniker"]
            hooked = (await create("hooked", "webhook", {"worker": y_moniker, "cap": "demo.echo", "args": {"via": "hook"}},
                                   {"token_moniker": hook_moniker}))["job"]
            async with httpx2.AsyncClient() as http:
                sent = await http.post(f"{base}/webhook", json={"build": "green"},
                                       headers={"Authorization": f"Bearer {hook}"})
                assert sent.status_code == 202
                for token, status, words in ((agent, 403, "webhook scope"), (hub.auth.mint("join", user=alice["guid"]),
                                                                           403, "webhook scope"), (None, 401, "Bearer")):
                    headers = {"Authorization": f"Bearer {token}"} if token else {}
                    refused = await http.post(f"{base}/webhook", json={}, headers=headers)
                    assert refused.status_code == status and words in refused.json()["next"]
            await eventually(lambda: runs(hub, hooked))
            assert events(hub, "webhook")[0]["payload"]["body"] == {"build": "green"}
            with pytest.raises(BaseException) as e:
                async with mcp_client(f"{base}/mcp", hook):
                    pass
            assert "client scope" in refusal(e.value)

            # 7. An agent emits through a hub cap, a worker through its link; each
            #    triggers a job; a payload filter runs only for the matching payload.
            kitchen = (await create("kitchen", "lights", {"worker": y_moniker, "cap": "demo.echo", "args": {"room": "k"}},
                                    {"room": "kitchen"}))["job"]
            await emit("lights", {"room": "bedroom"})
            await emit("lights", {"room": "kitchen"})
            await eventually(lambda: runs(hub, kitchen))
            await asyncio.sleep(0.1)
            assert len(runs(hub, kitchen)) == 1 and len(events(hub, "lights")) == 2
            bell = (await create("bell", "doorbell", {"worker": y_moniker, "cap": "demo.echo", "args": {"bell": True}},
                                 {"worker_moniker": x_moniker, "door": "front"}))["job"]
            failed, rang = await tool(door, "call", worker=x_moniker, cap="demo.ring", args={}, why="ring the bell")
            assert rang == {"rang": True}
            await eventually(lambda: runs(hub, bell))
            assert events(hub, "doorbell")[0]["payload"] == {"door": "front", "worker": x.identity["guid"],
                                                             "worker_moniker": x_moniker}

            # 2. X stops. After three missed heartbeats, with nobody reading the
            #    worker, the hub emits worker.offline and a matching job runs (H3, J2).
            gone = (await create("gone", "worker.offline", {"worker": "hub", "cap": "event.emit",
                                                            "args": {"name": "x.went"}}, {"worker_moniker": x_moniker}))["job"]
            x.stop()
            clock.advance(3 * 15 + 1)
            y.changed()                                    # y heartbeats at the new time and stays online
            await eventually(lambda: runs(hub, gone), timeout=3)   # the hub's own one-second look
            assert events(hub, "x.went")

            # 5. Schedules fire at their time, at the next, and not in between (J2).
            every = (await create("every", "schedule", {"worker": "hub", "cap": "event.emit", "args": {"name": "every.fired"}},
                                  {"schedule": "every 15m"}))["job"]
            daily = (await create("daily", "schedule", {"worker": "hub", "cap": "event.emit", "args": {"name": "daily.fired"}},
                                  {"schedule": "at 07:30"}))["job"]

            async def at(hour, minute, day=12):
                clock.now = time.mktime((2026, 11, day, hour, minute, 0, 0, 0, -1))
                hub.events.tick()
                await asyncio.sleep(0.1)
                return len(runs(hub, every)), len(runs(hub, daily))

            assert await at(7, 14) == (0, 0)
            assert await at(7, 16) == (1, 0)               # 07:15
            assert await at(7, 29) == (1, 0)
            assert await at(7, 31) == (2, 1)               # 07:30, both
            assert await at(7, 44) == (2, 1)
            assert await at(7, 31, day=13) == (3, 2)       # the next day's 07:30; one 15-minute fire, no catch-up

        # 9. Every event above is in the journal (J8); token.used is on the bus only.
        names = {e["event"] for e in events(hub)}
        assert {"worker.online", "worker.offline", "chain.start", "job.success", "job.fail", "webhook", "lights",
                "doorbell", "schedule", "x.went", "every.fired", "daily.fired"} <= names
        assert "token.used" not in names
    finally:
        for w, task in running:
            w.stop()
        await asyncio.gather(*(task for _, task in running), return_exceptions=True)
        await hub.stop_serving()
        hub.close()


# ------------------------------------------------------------------- edges

def test_schedule_due():
    at = lambda h, m, d=12: time.mktime((2026, 11, d, h, m, 0, 0, 0, -1))  # noqa: E731
    assert schedule_due("at 07:30", at(7, 0), at(7, 30)) and not schedule_due("at 07:30", at(7, 30), at(8, 0))
    assert schedule_due("at 23:59", at(23, 0), at(0, 30, 13))
    assert not schedule_due("at 07:30", at(7, 31), at(7, 29, 13))
    assert schedule_due("every 1m", 59, 60) and not schedule_due("every 1m", 60, 119)


def test_token_used_is_on_the_bus(tmp_path):
    async def go():
        hub = Hub(tmp_path / "hub")
        await hub.serve("127.0.0.1", 0)
        try:
            alice = hub.auth.create_user("Alice", "a password")["guid"]
            agent = hub.auth.mint("agent", user=alice)
            reply = await hub.call("hub", "job.create", {
                "title": "noticed", "match": {"event": "token.used", "payload": {"purpose": "webhook"}},
                "call": {"worker": "hub", "cap": "event.emit", "args": {"name": "hook.token.used"}}}, "watch hooks",
                token=agent)
            assert reply["type"] == "result" and reply["result"]["warnings"] == []
            hook = hub.auth.mint("webhook", user=alice)
            with hub._lock, hub._db:
                hub.auth.use(hook, "webhook")
            await eventually(lambda: any(e["event"] == "hook.token.used" for e in events(hub)))
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())


def test_job_create_says_what_is_wrong(tmp_path):
    async def go():
        hub = Hub(tmp_path / "hub")
        try:
            alice = hub.auth.create_user("Alice", "a password")["guid"]
            for args, words in (
                    ({"title": "t", "match": {"event": "schedule", "payload": {"schedule": "hourly"}},
                      "call": {"worker": "hub", "cap": "event.emit"}}, "every <N>m"),
                    ({"title": "t", "match": {"event": "x"}, "call": {"worker": "nowhere", "cap": "a.b"}},
                     "List the workers"),
                    ({"title": "t", "match": {"event": "x"}, "call": {"worker": "hub", "cap": "Not A Cap"}}, "cap name"),
                    ({"match": {"event": "x"}, "call": {"worker": "hub", "cap": "a.b"}}, "schema")):
                reply = await hub.call("hub", "job.create", args, "bad job", user=alice)
                assert reply["type"] == "error" and words in reply["message"] + reply["next"]
            assert hub._db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        finally:
            hub.close()
    asyncio.run(go())


def test_a_bad_event_does_not_stop_the_bus(tmp_path):
    # Anyone may emit job.success or job.fail, with any payload. Forged ones are
    # journaled as they are, and the bus goes on to the next event.
    from memory_link import link_pair
    from rook_protocol import encode
    from test_hub import heartbeat, knock, new_key

    async def go():
        hub = Hub(tmp_path / "hub")
        await hub.serve("127.0.0.1", 0)
        try:
            alice = hub.auth.create_user("Alice", "a password")["guid"]
            for title, match in (("ping", {"event": "ping"}), ("any success", {"event": "job.success"})):
                reply = await hub.call("hub", "job.create", {
                    "title": title, "match": match,
                    "call": {"worker": "hub", "cap": "event.emit", "args": {"name": f"{title} ran"}}}, "set up", user=alice)
                assert reply["type"] == "result"
            key = new_key()
            hub.knock(knock(key))
            hub.approve(key, user=alice)
            hub_end, link = link_pair(key)
            asyncio.get_running_loop().create_task(hub.serve_link(hub_end))
            await link.send(encode(heartbeat(key)))
            await link.recv()
            forged = ({}, {"result": "x"}, {"result": {"token": 1}, "run": {"not": "a string"}, "job": 7})
            for name in ("job.success", "job.fail"):
                for payload in forged:
                    await hub.call("hub", "event.emit", {"name": name, "payload": payload}, "forge one", user=alice)
                    await link.send(encode({"type": "event", "name": name, "payload": payload}))
            await hub.call("hub", "event.emit", {"name": "ping"}, "a normal event", user=alice)
            await eventually(lambda: events(hub, "ping ran"))
            assert len([e for e in events(hub) if e["event"] in ("job.success", "job.fail")
                        and set(e["payload"]) - {"worker", "worker_moniker"} <= {"result", "run", "job"}]) >= 12
            assert events(hub, "any success ran")           # the job matching every success ran on forged ones too
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())


def test_a_run_that_raises_still_ends_in_job_fail(tmp_path, monkeypatch):
    # J3: every run ends in job.success or job.fail, even when the call raises.
    from rook_hub import caps

    def explode(hub, args, actor):
        raise RuntimeError("something inside the hub broke")

    monkeypatch.setitem(caps.CAPS, "test.explode", (explode, {"description": "Raise.", "schema": {}}))

    async def go():
        hub = Hub(tmp_path / "hub")
        await hub.serve("127.0.0.1", 0)
        try:
            alice = hub.auth.create_user("Alice", "a password")["guid"]
            reply = await hub.call("hub", "job.create", {
                "title": "fragile", "match": {"event": "boom"},
                "call": {"worker": "hub", "cap": "test.explode"}}, "set up", user=alice)
            job = reply["result"]["job"]
            await hub.call("hub", "event.emit", {"name": "boom"}, "set it off", user=alice)
            [failed] = await eventually(lambda: events(hub, "job.fail"))
            assert failed["payload"]["job_moniker"] == job["moniker"]
            assert failed["payload"]["result"]["code"] == "run_failed" and failed["payload"]["result"]["next"]
            [run] = journal(hub, kind="job", job=job["guid"])
            assert json.loads(run["result"])["code"] == "run_failed" and run["run"] == failed["payload"]["run"]
        finally:
            await hub.stop_serving()
            hub.close()
    asyncio.run(go())
