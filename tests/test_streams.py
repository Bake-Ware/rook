"""rook.streams: the one stream model (castling-streams)."""

import asyncio

import pytest

from rook.streams import (KINDS, MemoryStore, SqliteStore, StreamError, StreamSpec, Streams,
                          cap_stream)
from rook.streams.events import MAX_EVENT


@pytest.fixture
def hub(tmp_path):
    db = SqliteStore(str(tmp_path / "streams.db"))
    yield Streams(durable=db, host="hub")
    db.close()


def run(coro):
    return asyncio.run(coro)


# -- specs and kinds -----------------------------------------------------------

def test_spec_from_manifest_block():
    s = StreamSpec.from_manifest({"types": ["bytes", "status"], "input": True, "durable": False})
    assert s.types == ("bytes", "status") and s.input and not s.durable
    # "input" in the type list is the same as input: true
    assert StreamSpec.from_manifest({"types": ["text", "input"]}).input
    assert StreamSpec.from_manifest({"types": ["text", "input"]}).types == ("text",)


@pytest.mark.parametrize("block", [{"types": ["video"]}, {"types": []}, {"types": ["input"]}, "bytes"])
def test_spec_refuses_bad_blocks(block):
    with pytest.raises(StreamError):
        StreamSpec.from_manifest(block)


def test_kind_defaults_follow_the_durability_decision():
    for k in ("room", "huddle", "session"):
        assert KINDS[k].durable
    for k in ("terminal", "process"):
        assert not KINDS[k].durable and KINDS[k].input


def test_unknown_kind_needs_a_spec(hub):
    with pytest.raises(StreamError):
        hub.open("tv-log")
    m = hub.open("tv-log", spec=StreamSpec(("text",)))
    assert m.kind == "tv-log" and not m.spec.durable
    with pytest.raises(StreamError):
        hub.open("Bad Kind", spec=StreamSpec(("text",)))


def test_durable_needs_a_durable_store():
    worker = Streams(host="cachyrig")
    with pytest.raises(StreamError, match="hub"):
        worker.open("room")
    assert worker.open("terminal").host == "cachyrig"


# -- append and read -----------------------------------------------------------

@pytest.mark.parametrize("kind", ["room", "terminal"])
def test_append_and_read_by_cursor(hub, kind):
    spec = StreamSpec(("text", "bytes"), input=True, durable=(kind == "room"))
    m = hub.open(kind, spec=spec, title="t")
    hub.append(m.id, "text", {"text": "hello", "mentions": ["bake"]}, by="agent:a")
    hub.append(m.id, "bytes", b"\x1b[1mout\x00", by="pty")
    hub.append(m.id, "input", {"text": "ls\r"}, by="human:bake")
    r = run(hub.read(m.id))
    assert [e.seq for e in r["events"]] == [1, 2, 3] and r["next"] == 3 and r["dropped"] == 0
    assert r["events"][0].body == {"text": "hello", "mentions": ["bake"]}
    assert r["events"][1].body == b"\x1b[1mout\x00"
    assert r["events"][1].wire()["b64"] == "G1sxbW91dAA="
    assert r["events"][0].wire()["data"]["text"] == "hello"
    # cursor, limit, type filter
    assert [e.seq for e in run(hub.read(m.id, after=1))["events"]] == [2, 3]
    r = run(hub.read(m.id, limit=2))
    assert [e.seq for e in r["events"]] == [1, 2] and r["next"] == 2
    r = run(hub.read(m.id, types=["input"]))
    assert [e.seq for e in r["events"]] == [3] and r["next"] == 3
    r = run(hub.read(m.id, types=["status"]))
    assert r["events"] == [] and r["next"] == 3     # nothing matched: skip ahead
    assert run(hub.read(m.id, after=99))["next"] == 3


def test_types_are_enforced(hub):
    room = hub.open("room")
    with pytest.raises(StreamError, match="does not take bytes"):
        hub.append(room.id, "bytes", b"x")
    with pytest.raises(StreamError, match="does not take input"):
        hub.append(room.id, "input", {"text": "x"})
    hub.append(room.id, "status", {"state": "busy"})      # status is always allowed
    term = hub.open("terminal")
    with pytest.raises(StreamError, match="does not take text"):
        hub.append(term.id, "text", {"text": "x"})


@pytest.mark.parametrize("type_,body", [
    ("text", {"txt": "x"}), ("text", "x"), ("bytes", "x"), ("turn", {"role": "boss"}),
    ("input", {}), ("input", {"text": "a", "signal": "INT"}), ("input", {"signal": "STOP"}),
    ("input", {"resize": [80]}), ("input", {"resize": [0, 24]}), ("status", {"x": object()}),
])
def test_bad_bodies_are_refused(hub, type_, body):
    m = hub.open("any", spec=StreamSpec(("text", "bytes", "turn"), input=True))
    with pytest.raises(StreamError):
        hub.append(m.id, type_, body)


def test_good_inputs_and_turns(hub):
    m = hub.open("session")
    hub.append(m.id, "turn", {"role": "prompt", "text": "hi"})
    hub.append(m.id, "turn", {"role": "tool", "name": "shell.exec", "args": {"argv": ["ls"]}})
    hub.append(m.id, "input", {"signal": "INT"})
    hub.append(m.id, "input", {"resize": [120, 40]})
    assert hub.get(m.id).last_seq == 4


def test_event_size_cap(hub):
    m = hub.open("terminal")
    with pytest.raises(StreamError, match="split"):
        hub.append(m.id, "bytes", b"x" * (MAX_EVENT + 1))


# -- bounded buffers -----------------------------------------------------------

def test_bounded_stream_drops_oldest_and_says_how_many():
    s = Streams()
    m = s.open("terminal", budget=4096)
    for i in range(100):
        s.append(m.id, "bytes", bytes([65 + i % 26]) * 100)
    r = run(s.read(m.id, after=0, limit=1000))
    first = r["events"][0].seq
    assert r["dropped"] == first - 1 > 0
    assert r["events"][-1].seq == 100 and r["next"] == 100
    held = sum(e.size for e in r["events"])
    assert held <= 4096
    # a reader inside the window loses nothing
    assert run(s.read(m.id, after=95))["dropped"] == 0


def test_closed_bounded_streams_are_pruned():
    s = Streams(bounded=MemoryStore(keep_closed=2))
    ids = []
    for _ in range(4):
        m = s.open("process")
        s.close(m.id)
        ids.append(m.id)
    s.open("process")
    assert [m.id for m in s.list(kind="process", open_only=False) if m.closed] == ids[2:]


def test_export_keeps_a_terminal(hub):
    m = hub.open("terminal", title="build")
    hub.append(m.id, "bytes", b"compiling\n")
    hub.append(m.id, "bytes", b"done\n")
    d = hub.export(m.id)
    assert d.spec.durable and d.attrs["exported_from"] == m.id and d.title == "build"
    assert [e.body for e in run(hub.read(d.id))["events"]] == [b"compiling\n", b"done\n"]
    with pytest.raises(StreamError):
        hub.export(d.id)


# -- metadata, list, close, delete, durability ----------------------------------

def test_metadata_and_list(hub):
    a = hub.open("room", title="Kitchen", participants=["human:bake", "agent:house", "human:bake"])
    b = hub.open("terminal", producer="pty", attrs={"cwd": "/tmp"})
    assert a.participants == ["human:bake", "agent:house"]
    hub.update(a.id, title="Kitchen talk", participants=["human:bake"], attrs={"pinned": True})
    hub.update(b.id, attrs={"pid": 42})
    assert hub.get(a.id).title == "Kitchen talk" and hub.get(a.id).attrs == {"pinned": True}
    assert hub.get(b.id).attrs == {"cwd": "/tmp", "pid": 42}
    assert [m.id for m in hub.list()] == [a.id, b.id]
    assert [m.id for m in hub.list(kind="room")] == [a.id]
    assert [m.id for m in hub.list(participant="human:bake")] == [a.id]
    w = hub.get(a.id).wire()
    assert w["kind"] == "room" and w["durable"] and w["types"] == ["text"] and w["host"] == "hub"


def test_close_keeps_history_and_refuses_writes(hub):
    m = hub.open("huddle")
    hub.append(m.id, "text", {"text": "ship it?"})
    hub.close(m.id, by="human:bake", reason="archived")
    hub.close(m.id)                                 # idempotent
    r = run(hub.read(m.id))
    assert r["closed"] and r["events"][-1].body == {"state": "closed", "reason": "archived"}
    with pytest.raises(StreamError, match="closed"):
        hub.append(m.id, "text", {"text": "late"})
    assert hub.list(open_only=True) == []


def test_delete_erases(hub):
    m = hub.open("room")
    hub.append(m.id, "text", {"text": "secret plans"})
    hub.delete(m.id)
    with pytest.raises(StreamError):
        hub.get(m.id)
    assert hub.search("secret") == []
    with pytest.raises(StreamError):
        hub.delete(m.id)


def test_durable_streams_survive_a_restart(tmp_path):
    path = str(tmp_path / "s.db")
    db = SqliteStore(path)
    s = Streams(durable=db)
    m = s.open("session", title="castling", participants=["agent:streams"])
    s.append(m.id, "turn", {"role": "prompt", "text": "build streams"})
    s.append(m.id, "input", {"text": "also tests"})
    db.close()
    db = SqliteStore(path)
    s = Streams(durable=db)
    got = s.get(m.id)
    assert got.title == "castling" and got.spec == StreamSpec(("turn", "text"), True, True)
    assert got.last_seq == 2
    s.append(m.id, "turn", {"role": "reply", "text": "on it"})
    assert [e.seq for e in run(s.read(m.id))["events"]] == [1, 2, 3]
    db.close()


def test_search_across_stores(hub):
    room = hub.open("room", title="plans")
    term = hub.open("terminal")
    hub.append(room.id, "text", {"text": "Deploy at 5_pm%"})
    hub.append(term.id, "bytes", b"deploying hub...\n")
    hub.append(room.id, "text", {"text": "unrelated"})
    hits = hub.search("deploy")
    assert {h["stream"] for h in hits} == {room.id, term.id}
    assert [h["stream"] for h in hub.search("deploy", kind="room")] == [room.id]
    assert [h["seq"] for h in hub.search("5_pm%")] == [1]     # LIKE wildcards are literal
    assert hub.search("5xpm") == []
    with pytest.raises(StreamError):
        hub.search("")


# -- long-poll and follow -------------------------------------------------------

@pytest.mark.parametrize("kind", ["room", "terminal"])
def test_read_waits_for_new_events(hub, kind):
    async def go():
        m = hub.open(kind, spec=StreamSpec(("text",), durable=(kind == "room")))
        t0 = asyncio.get_running_loop().time()
        r = await hub.read(m.id, wait=0.05)
        assert r["events"] == [] and asyncio.get_running_loop().time() - t0 >= 0.04
        asyncio.get_running_loop().call_later(0.02, hub.append, m.id, "text", {"text": "hi"})
        r = await hub.read(m.id, wait=5)
        assert [e.body["text"] for e in r["events"]] == ["hi"]
    run(go())


def test_follow_streams_until_closed(hub):
    async def go():
        m = hub.open("room")
        hub.append(m.id, "text", {"text": "one"})
        got = []

        async def reader():
            async for ev in hub.follow(m.id, types=["text"]):
                got.append(ev.body["text"])

        task = asyncio.create_task(reader())
        await asyncio.sleep(0.01)
        for w in ("two", "three"):
            hub.append(m.id, "text", {"text": w})
            await asyncio.sleep(0)
        hub.close(m.id)
        await asyncio.wait_for(task, 2)
        assert got == ["one", "two", "three"]
    run(go())


def test_many_followers_see_everything():
    async def go():
        s = Streams()
        m = s.open("terminal")

        async def reader():
            return [ev.seq async for ev in s.follow(m.id)]

        tasks = [asyncio.create_task(reader()) for _ in range(3)]
        await asyncio.sleep(0)
        for i in range(20):
            s.append(m.id, "bytes", b"%d" % i)
            if i % 7 == 0:
                await asyncio.sleep(0)
        s.close(m.id)
        results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert all(r == list(range(1, 22)) for r in results)
    run(go())


# -- producers and input --------------------------------------------------------

def test_producer_takes_queued_input_then_live_input(hub):
    """A message to a sleeping agent waits in its session; the agent gets it on wake."""
    async def go():
        m = hub.open("session", producer="claude")
        hub.append(m.id, "input", {"text": "while you slept"}, by="human:bake")
        p = hub.attach(m.id, "claude")
        assert hub.attached(m.id)
        with pytest.raises(StreamError, match="already has a producer"):
            hub.attach(m.id, "codex")
        got = []

        async def harness():
            async for ev in p.inputs():
                got.append(ev.body["text"])
                p.append("turn", {"role": "reply", "text": "ok: " + ev.body["text"]})

        task = asyncio.create_task(harness())
        await asyncio.sleep(0.01)
        hub.append(m.id, "input", {"text": "now"}, by="human:bake")
        await asyncio.sleep(0.01)
        p.detach()
        await asyncio.wait_for(task, 2)
        assert got == ["while you slept", "now"]
        assert not hub.attached(m.id)
        with pytest.raises(StreamError, match="no longer attached"):
            p.append("turn", {"role": "reply", "text": "x"})
        # nothing is re-delivered to the next producer
        hub.append(m.id, "input", {"text": "next time"})
        p2 = hub.attach(m.id, "claude")
        agen = p2.inputs()
        assert (await agen.__anext__()).body["text"] == "next time"
        await agen.aclose()
        replies = [e.body["text"] for e in (await hub.read(m.id, types=["turn"]))["events"]]
        assert replies == ["ok: while you slept", "ok: now"]
    run(go())


def test_input_survives_a_producer_crash_mid_event(hub):
    async def go():
        m = hub.open("session")
        hub.append(m.id, "input", {"text": "a"})
        p = hub.attach(m.id, "claude")
        agen = p.inputs()
        await agen.__anext__()          # got "a" but never asked for the next
        await agen.aclose()
        p.detach()
        p = hub.attach(m.id, "claude")
        agen = p.inputs()
        assert (await agen.__anext__()).body["text"] == "a"
        await agen.aclose()
    run(go())


def test_close_ends_the_producer_loop():
    async def go():
        s = Streams()
        m = s.open("terminal")
        p = s.attach(m.id, "pty")
        task = asyncio.create_task(_drain(p.inputs()))
        await asyncio.sleep(0)
        s.append(m.id, "input", {"signal": "INT"})
        await asyncio.sleep(0)
        p.close(exit_code=130)
        assert await asyncio.wait_for(task, 2) == [{"signal": "INT"}]
        assert s.get(m.id).closed
        last = (await s.read(m.id))["events"][-1]
        assert last.by == "pty" and last.body == {"state": "closed", "exit_code": 130}
    run(go())


async def _drain(agen):
    return [ev.body async for ev in agen]


def test_cap_stream_matches_the_manifest_block(hub):
    """What ctx.stream does for a cap declaring stream: {types, input, durable}."""
    async def go():
        p = cap_stream(hub, {"types": ["bytes", "status"], "input": True, "durable": False},
                       producer="deluge.logs.follow", title="deluge log")
        assert p.result() == {"stream": p.id}
        m = hub.get(p.id)
        assert m.kind == "process" and m.spec.types == ("bytes", "status") and m.spec.input
        assert not m.spec.durable and m.producer == "deluge.logs.follow"
        p.append("bytes", b"line\n")
        p.append("status", {"state": "running"})
        r = await hub.read(p.id)
        assert [e.type for e in r["events"]] == ["bytes", "status"]
        d = cap_stream(hub, {"types": ["turn"], "durable": True}, producer="voice", kind="voice")
        assert hub.get(d.id).spec.durable and not hub.get(d.id).spec.input
    run(go())
