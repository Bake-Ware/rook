"""The brief's seven-step worker lifecycle, end to end: real HTTP for the knock,
an in-memory link for everything after it."""

import asyncio
import json
import uuid

import pytest

from helpers import Clock, eventually
from memory_link import connector
from rook_hub import DEFAULT_INTERVAL, Hub
from rook_protocol import decode
from rook_worker import Cap, Refused, Worker


async def echo(args):
    return args


async def fail(args):
    raise ValueError("no such file")


CAPS = {
    "demo.echo": Cap("Return the arguments unchanged.", {"type": "object"}, echo),
    "demo.fail": Cap("Always fails.", {"type": "object"}, fail),
}


def journal(hub, kinds=("call", "auth")):
    """Calls and auth entries; events are covered in test_jobs.py."""
    return [dict(row) for row in hub._db.execute("SELECT * FROM journal ORDER BY id") if row["kind"] in kinds]


def test_worker_lifecycle(tmp_path):
    asyncio.run(lifecycle(tmp_path))


async def lifecycle(tmp_path):
    clock = Clock()
    hub = Hub(tmp_path / "hub", clock=clock)
    host, port = await hub.serve("127.0.0.1", 0)
    connect = connector(hub)
    bake = hub.auth.create_user("Bake", "a password")["guid"]

    def worker(state):
        return Worker(tmp_path / state, f"http://{host}:{port}", connect, name="test-box",
                      caps=CAPS, macs=["02:00:00:00:00:01"], knock_every=0.02)

    try:
        # 1. A worker with no state generates a keypair and knocks. It shows as
        #    pending with its name and offered caps (R2, R4, R5).
        first = worker("state")
        assert len(first.key) == 64 and (tmp_path / "state" / "key").exists()
        running = asyncio.create_task(first.run())
        pending = await eventually(hub.list_pending)
        assert len(pending) == 1
        assert pending[0]["key"] == first.key
        assert pending[0]["name"] == "test-box"
        assert [c["name"] for c in pending[0]["caps"]] == ["demo.echo", "demo.fail"]
        assert pending[0]["likely_rekey_of"] == []
        assert hub.list_workers() == []

        # 2. Approval mints GUID and moniker, records the owner, signs the key;
        #    the worker persists its identity (W6, W7, G1, R5, U2).
        record = hub.approve(first.key, user=bake)
        guid, moniker = record["guid"], record["moniker"]
        assert str(uuid.UUID(guid)) == guid
        assert len(moniker.split("-")) == 3
        assert record["owner"] == bake
        assert hub.list_pending() == []
        await eventually(lambda: first.identity)
        assert first.identity["guid"] == guid and first.identity["moniker"] == moniker
        stored = json.loads((tmp_path / "state" / "identity.json").read_text())
        assert stored == first.identity
        assert stored["hub_key"] == hub.public_key   # the worker verified the signature with it
        assert hub.get_worker(moniker)["guid"] == guid  # either ID is accepted (G1)
        [entry] = journal(hub)                     # approval is journaled (L1)
        assert (entry["kind"], entry["user"], entry["token"], entry["worker"]) == ("auth", bake, None, guid)
        assert json.loads(entry["result"]) == {"approved": first.key}

        # 3. The worker heartbeats its full state; the hub stores it, replies
        #    with the interval and keeps the raw history (H1, H2, H4, H5 raw).
        await eventually(lambda: hub.get_worker(guid)["online"])
        await eventually(lambda: first.interval)
        assert first.interval == DEFAULT_INTERVAL
        record = hub.get_worker(guid)
        assert [c["name"] for c in record["caps"]] == ["demo.echo", "demo.fail"]
        assert record["facts"]["form"] == "python" and "disk" in record["health"]
        assert "gateway_mac" in record["band"]
        raw = [decode(row[0].encode()) for row in hub._db.execute(
            "SELECT body FROM heartbeats WHERE worker = ?", (guid,))]
        assert raw and raw[0]["identity"]["key"] == first.key

        hub.set_interval(guid, 7)       # per worker (H2) ...
        first.changed()                 # ... and a change heartbeats at once (H1)
        await eventually(lambda: first.interval == 7)
        assert len(hub._db.execute("SELECT id FROM heartbeats WHERE worker = ?", (guid,)).fetchall()) == 2

        # 4. Calls go through the hub; failures say what to try next; every
        #    call is journaled (C3, W5, L1).
        reply = await hub.call(moniker, "demo.echo", {"text": "hi"}, "check the echo", user=bake)
        assert reply["type"] == "result" and reply["result"] == {"text": "hi"}

        reply = await hub.call(guid, "video.capture", {}, "try a cap it lacks", user=bake)
        assert reply["type"] == "error" and reply["code"] == "unknown_cap" and reply["next"]
        reply = await hub.call(guid, "demo.fail", {}, "see a worker-side failure", user=bake)
        assert reply["code"] == "cap_failed" and "no such file" in reply["message"] and reply["next"]
        reply = await hub.call("no-such-worker", "demo.echo", {}, "try a worker that does not exist", user=bake)
        assert reply["code"] == "unknown_worker" and reply["next"]

        rows = [r for r in journal(hub) if r["kind"] == "call"]
        assert [(r["user"], r["token"], r["worker"], r["cap"], r["why"]) for r in rows] == [
            (bake, None, guid, "demo.echo", "check the echo"),
            (bake, None, guid, "video.capture", "try a cap it lacks"),
            (bake, None, guid, "demo.fail", "see a worker-side failure"),
            (bake, None, None, "demo.echo", "try a worker that does not exist"),
        ]
        assert json.loads(rows[0]["result"])["result"] == {"text": "hi"}
        assert json.loads(rows[1]["result"])["code"] == "unknown_cap"

        # 5. The worker stops. After three missed heartbeats it is offline,
        #    and its record and caps are kept (H3).
        first.stop()
        await running
        clock.advance(3 * 7)
        assert hub.get_worker(guid)["online"]
        clock.advance(1)
        record = hub.get_worker(guid)
        assert not record["online"]
        assert [c["name"] for c in record["caps"]] == ["demo.echo", "demo.fail"]
        reply = await hub.call(guid, "demo.echo", {}, "call it while offline", user=bake)
        assert reply["code"] == "worker_offline" and reply["next"]

        # 6. It restarts from its state directory as the same identity, with no
        #    new approval (R5).
        again = worker("state")
        assert again.key == first.key and again.identity == first.identity
        running = asyncio.create_task(again.run())
        await eventually(lambda: hub.get_worker(guid)["online"])
        assert hub.list_pending() == []
        assert [w["guid"] for w in hub.list_workers()] == [guid]
        reply = await hub.call(guid, "demo.echo", {"n": 1}, "call after restart", user=bake)
        assert reply["result"] == {"n": 1}
        again.stop()
        await running

        # 7. A worker that lost its key knocks fresh under the same name; the hub
        #    flags a likely rekey. Attaching is an explicit call. The old key is
        #    refused afterwards; owner and journal history stay (W7).
        sam = hub.auth.create_user("Sam", "a password")["guid"]      # someone other than the owner attaches
        sam_agent = hub.auth.mint("agent", user=sam)
        history = journal(hub)
        fresh = worker("lost-key")
        assert fresh.key != first.key
        running = asyncio.create_task(fresh.run())
        pending = await eventually(hub.list_pending)
        assert pending[0]["key"] == fresh.key
        assert pending[0]["likely_rekey_of"] == [
            {"guid": guid, "moniker": moniker, "matched": ["mac 02:00:00:00:00:01", "name test-box"]}]
        await asyncio.sleep(0.1)
        assert hub.list_pending()[0]["key"] == fresh.key   # never attached on its own
        assert fresh.identity is None

        record = hub.attach(fresh.key, moniker, token=sam_agent)
        assert record["guid"] == guid and record["owner"] == bake
        await eventually(lambda: fresh.identity)
        assert fresh.identity["guid"] == guid and fresh.identity["moniker"] == moniker
        await eventually(lambda: hub.get_worker(guid)["online"])
        reply = await hub.call(guid, "demo.echo", {"n": 2}, "call with the new key", user=bake)
        assert reply["result"] == {"n": 2}
        assert journal(hub)[:len(history)] == history
        entry = journal(hub)[len(history)]       # attaching is journaled with what it revoked (L1)
        assert (entry["kind"], entry["user"], entry["token"], entry["worker"]) == (
            "auth", sam, hub.auth.verify(sam_agent)["guid"], guid)
        assert json.loads(entry["result"]) == {"attached": fresh.key, "revoked": [first.key]}

        keys = hub.seen(guid)["keys"]
        assert [(k["key"], k["revoked_at"] is None) for k in keys] == [(first.key, False), (fresh.key, True)]
        assert keys[0]["revoked_at"] == clock.now and keys[1]["attached_at"] == clock.now

        old = worker("state")           # the old state directory still holds the old key
        with pytest.raises(Refused) as refused:
            await old.run()
        assert refused.value.message["code"] == "key_revoked"
        assert hub.get_worker(guid)["online"]
        fresh.stop()
        await running
    finally:
        await hub.stop_serving()
        hub.close()
