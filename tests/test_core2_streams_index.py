"""The hub's stream index on a real hub and worker (gate R3, N28): a
worker's streams reach the index as stream.* events for their own band,
resync after a restart, can't touch another host's rows, hub plugins' own
streams join through ctx.index_streams, and stream.list / get / search page
it under each caller's rights. Every test has a hard timeout."""
from __future__ import annotations

import asyncio
import json
import secrets
import time

import pytest

pytest.importorskip("nacl")
pytest.importorskip("telesthete")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import wire  # noqa: E402
from rook.core2.hub.identity import Caller as HubCaller  # noqa: E402
from rook.core2.hub.runtime import Config as HubConfig, Hub  # noqa: E402
from rook.core2.transport import LoopbackRelay, Transport, band_of  # noqa: E402
from rook.core2.worker.runtime import Config, Worker  # noqa: E402
from rook.kit.ctx import Caller as KitCaller, Ctx  # noqa: E402
from rook.streams import SqliteStore, StreamIndex, Streams  # noqa: E402

PSK = "streams-index-band"
BAND = band_of(PSK)
WID = "5e1d" * 8
ROOT, OP = SigningKey(b"\x51" * 32), SigningKey(b"\x52" * 32)
HOME = wire.key_id(bytes(ROOT.verify_key))
WHY = "Testing the stream index."
BAKE, ANN, HOUSE = "person:bake", "person:ann", "agent:house"

TAIL_YAML = """manifest: 1
plugin: tail
version: 1.0.0
caps:
  open:
    doc: Open a process stream and write a line to it.
    risk: write
    scope: self
    args: { title: { type: str } }
    stream: { types: [bytes], input: true }
    run: scripts/tail.py:open_
"""

TAIL_PY = """
def open_(ctx, title=""):
    h = ctx.stream("process", title=title, attrs={"cwd": "/srv/build"})
    h.append("bytes", b"hello\\n")
    return h
"""


def go(body, seconds=90):
    async def main():
        return await asyncio.wait_for(body(), seconds)
    return asyncio.run(main())


async def until(cond, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not (got := cond()):
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
    return got


@pytest.fixture
def band(tmp_path):
    tail = tmp_path / "local" / "tail"
    (tail / "scripts").mkdir(parents=True)
    (tail / "plugin.yaml").write_text(TAIL_YAML)
    (tail / "scripts" / "tail.py").write_text(TAIL_PY)
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"hub": HOME, "root": wire.pub_text(ROOT), "band": BAND}))
    keys = tmp_path / "hub" / "keys"
    keys.mkdir(parents=True)
    (keys / "root.key").write_text(bytes(ROOT).hex())
    (keys / "op.key").write_text(bytes(OP).hex())
    node = secrets.token_hex(16)
    (tmp_path / "hub" / "hub.json").write_text(json.dumps({"node": node}))
    relay = LoopbackRelay()
    cfg = Config(data=tmp_path / "data", anchor=anchor, name="pixel", worker_id=WID,
                 bands=[{"psk": PSK}], plugins=[tmp_path / "local"])
    hub_cfg = HubConfig(data=tmp_path / "hub", band=BAND, users={"bake": {"preset": "owner"},
                                                                 "ann": {"preset": "adult"}},
                        agents={"house": {"grants": ["stream.*"]}}, doors={})

    def worker():
        w = Worker(cfg, make_link=lambda spec: relay.link(spec["psk"]),
                   transport_opts={"every": 0.05, "lost_after": 3.0})   # contact is announces and calls
        return w

    def hub():
        return Hub(hub_cfg, transport=Transport(node, [relay.link(PSK)], every=0.05))
    return worker, hub


async def call(hub, cap, args=None, worker="rook", person=BAKE, agent=None):
    return await hub.call(HubCaller(person=person, agent=agent),
                          {"worker": worker, "cap": cap, "args": args or {}, "why": WHY}, "test")


async def listed(hub, **who) -> list:
    out = await call(hub, "stream.list", {"limit": 200}, **who)
    assert out["ok"], out
    return out["result"]["streams"]


async def up(make_worker, make_hub):
    w, hub = make_worker(), make_hub()
    await w.start()
    await hub.start()
    await until(lambda: "tail.open" in getattr(hub.dispatcher.workers.get(WID), "caps", {}))
    w.stream_reporter.every = 0.05
    return w, hub


def test_a_workers_streams_are_listed_for_whoever_may_see_them(band):
    async def body():
        w, hub = await up(*band)
        mine = await call(hub, "tail.open", {"title": "make test"}, worker="pixel")
        theirs = await call(hub, "tail.open", {"title": "ann's build"}, worker="pixel", person=ANN)
        assert mine["ok"] and theirs["ok"], (mine, theirs)
        sid, other = mine["result"]["stream"], theirs["result"]["stream"]
        # the first events can meet the outbox's retry backoff while the hub reads the announce
        await until(lambda: hub.streams.index.get(sid) and hub.streams.index.get(other), 30)
        await until(lambda: hub.streams.index.get(sid)["last_seq"] >= 1, 30)

        rows = await listed(hub)                                   # an owner: everything
        assert {r["id"] for r in rows} == {sid, other}
        row = next(r for r in rows if r["id"] == sid)
        assert row["worker"] == "pixel" and row["kind"] == "process" and row["title"] == "make test"
        assert row["person"] == BAKE and row["online"] and row["closed"] is None and row["size"] > 0
        assert row["attrs"]["cwd"] == "/srv/build" and row["attrs"]["band"] == BAND
        assert [r["id"] for r in await listed(hub, person=ANN)] == [other]      # a person: theirs
        assert await listed(hub, person=None, agent=HOUSE) == []                # an agent alone: none of these

        found = await call(hub, "stream.search", {"query": "ANN'S"}, person=ANN)
        assert [r["id"] for r in found["result"]["streams"]] == [other]
        got = await call(hub, "stream.get", {"stream": sid}, person=ANN)
        assert not got["ok"] and "you can see" in got["error"]
        assert (await call(hub, "stream.get", {"stream": sid}))["result"]["id"] == sid

        # the stream itself is still read where it lives; the hub's ticket tells the worker
        # who its owners are, so an owner reads someone else's too (N28)
        read = await call(hub, "stream.read", {"stream": sid}, worker="pixel")
        assert read["ok"] and read["result"]["events"], read
        theirs_read = await call(hub, "stream.read", {"stream": other}, worker="pixel")
        assert theirs_read["ok"] and theirs_read["result"]["events"], theirs_read
        refused = await call(hub, "stream.read", {"stream": sid}, worker="pixel", person=ANN)
        assert not refused["ok"] and "of yours" in refused["error"], refused
        on_worker = await call(hub, "stream.list", worker="pixel")
        assert {r["id"] for r in on_worker["result"]["streams"]} == {sid, other}

        closed = await call(hub, "stream.close", {"stream": sid}, worker="pixel")
        assert closed["ok"], closed
        await until(lambda: hub.streams.index.get(sid)["closed"], 30)
        page = await call(hub, "stream.list", {"open": True})
        assert [r["id"] for r in page["result"]["streams"]] == [other]
        await hub.stop()
        await w.stop()
    go(body)


def test_a_restarted_worker_resyncs_and_the_index_drops_what_it_lost(band):
    make_worker, make_hub = band

    async def body():
        w, hub = await up(make_worker, make_hub)
        out = await call(hub, "tail.open", {"title": "gone after restart"}, worker="pixel")
        sid = out["result"]["stream"]
        await until(lambda: hub.streams.index.get(sid), 30)
        await w.stop()                                   # bounded streams live in the worker's memory
        w = make_worker()
        await w.start()
        await until(lambda: hub.streams.index.get(sid) is None, 15)
        await hub.stop()
        await w.stop()
    go(body)


def test_the_index_outlives_the_hub_and_a_hub_without_one_learns_by_resync(band, tmp_path):
    make_worker, make_hub = band

    async def body():
        w, hub = await up(make_worker, make_hub)
        sid = (await call(hub, "tail.open", {"title": "kept"}, worker="pixel"))["result"]["stream"]
        await until(lambda: hub.streams.index.get(sid), 30)
        drained = lambda: all(not box.queue for box in w.outboxes.values())   # noqa: E731
        await until(drained, 20)               # nothing in flight to a hub about to stop (it would time out)
        await hub.stop()
        kept = StreamIndex(str(tmp_path / "hub" / "streams.db"))
        assert kept.get(sid)                             # the index outlives the hub process
        kept.close()
        await until(lambda: w.transport.lost(BAND), 20)
        # A new hub process's announce seq starts at the clock; the old one's ran ahead of it
        # (several announces a second), and the worker takes only a newer seq (spec 5.4).
        await asyncio.sleep(max(0.0, hub._seq + 1 - time.time()))
        for f in (tmp_path / "hub").glob("streams.db*"):  # a hub that lost its index
            f.unlink()
        hub = make_hub()
        await hub.start()
        assert hub.streams.index.get(sid) is None
        await until(lambda: hub.streams.index.get(sid), 15)   # hub contact: the worker resyncs
        await hub.stop()
        await w.stop()
    go(body)


def test_worker_rows_stay_its_own_and_its_band(band):
    async def body():
        w, hub = await up(*band)
        k = hub.known[WID]
        row = {"id": "s_" + "a" * 16, "kind": "terminal", "participants": [BAKE], "attrs": {"band": "f" * 32},
               "created": 1.0}
        hub.streams.take(k, "stream.changed", row)                     # another band's stream
        assert hub.streams.index.get(row["id"]) is None
        hub.streams.index.put("hub:agents", "rook", {**row, "attrs": {}})
        hub.streams.take(k, "stream.changed", {**row, "attrs": {"band": BAND}, "title": "hijack"})
        assert hub.streams.index.get(row["id"])["worker"] == "rook"    # not the worker's to replace
        hub.streams.take(k, "stream.deleted", {"id": row["id"]})
        assert hub.streams.index.get(row["id"])
        hub.streams.take(k, "stream.synced", {"ids": [], "complete": True})
        assert hub.streams.index.get(row["id"])                       # a sync only touches its own
        await hub.stop()
        await w.stop()
    go(body)


def test_hub_plugins_list_their_own_streams_with_ctx_index_streams(band, tmp_path):
    async def body():
        hub = band[1]()
        await hub.start()
        db = SqliteStore(str(tmp_path / "sessions.db"))
        sessions = Streams(db, host="hub")
        old = sessions.open("session", title="before the hub listened", participants=[HOUSE])
        ctx = hub.plugins._ctx(hub.plugins.plugins["stream"], KitCaller(plugin="stream"))
        assert ctx.index_streams(Streams(host="hub"))                   # a hub plugin's ctx reaches it
        assert hub.streams._reporters.keys() == {"stream"}
        assert Ctx({"plugin": "x"}, KitCaller()).index_streams(sessions) is False   # no index here
        hub.streams.watch("agents", sessions)       # what the agents plugin's ctx.index_streams does
        new = sessions.open("session", title="house with bake", participants=[HOUSE, BAKE])
        rows = await listed(hub, person=None, agent=HOUSE)
        assert [r["id"] for r in rows] == [old.id]                      # alone: not the one with bake
        assert rows[0]["plugin"] == "agents" and rows[0]["worker"] == "rook" and rows[0]["durable"]
        assert {r["id"] for r in await listed(hub)} == {old.id, new.id}
        assert [r["id"] for r in (await call(hub, "stream.list", {"worker": "rook", "kind": "session",
                                                                  "limit": 1}))["result"]["streams"]] == [new.id]
        sessions.delete(old.id)
        assert hub.streams.index.get(old.id) is None
        await hub.stop()
        db.close()
    go(body)
