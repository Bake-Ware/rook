"""The hub's stream index (gate R3, N28): Streams' change reports, the
reporter's throttling, and the index's rows, ownership, paging and rights.
Every async test has a hard timeout."""
from __future__ import annotations

import asyncio

import pytest

from rook.streams import KINDS, MemoryStore, SqliteStore, StreamIndex, Streams
from rook.streams.index import Reporter, visible


def run(coro, seconds=10):
    return asyncio.run(asyncio.wait_for(coro, seconds))


@pytest.fixture
def index(tmp_path):
    ix = StreamIndex(str(tmp_path / "index.db"))
    yield ix
    ix.close()


def _row(sid, participants=("person:bake",), kind="terminal", **kw):
    return {"id": sid, "kind": kind, "title": kw.pop("title", sid), "participants": list(participants),
            "attrs": {}, "types": ["bytes"], "input": True, "durable": False, "created": 1.0,
            "last_at": kw.pop("last_at", 1.0), "last_seq": 0, "size": 0, **kw}


S1, S2, S3 = "s_" + "1" * 16, "s_" + "2" * 16, "s_" + "3" * 16


# -- Streams: change reports and summaries ------------------------------------------

def test_streams_report_every_change_and_summaries_carry_size_and_activity(tmp_path):
    db = SqliteStore(str(tmp_path / "s.db"))
    s = Streams(durable=db, host="hub", clock=iter(range(10, 100)).__next__)
    seen = []
    s.on_change = lambda sid, change: seen.append((change, s.summary(sid)))
    m = s.open("session", title="house with bake", participants=["agent:house", "person:bake"],
               attrs={"agent": "house", "big": {"nested": 1}, "nan": float("nan"), "long": "x" * 500})
    s.append(m.id, "turn", {"role": "prompt", "text": "hi"})
    s.update(m.id, title="renamed")
    s.close(m.id, code=0)
    s.delete(m.id)
    changes = [c for c, _ in seen]
    assert changes == ["open", "append", "update", "close", "delete"]
    first, last = seen[0][1], seen[-1][1]
    assert first["attrs"] == {"agent": "house", "long": "x" * 200}     # plain values only, cut short
    assert first["size"] == 0 and first["last_at"] == first["created"] and "closed" not in first
    assert last["title"] == "renamed" and last["closed"] and last["durable"] and last["size"] > 0
    assert last["last_at"] > last["created"] and last["last_seq"] == 2
    db.close()


def test_a_bounded_store_reports_the_finished_streams_it_prunes():
    s = Streams(bounded=MemoryStore(keep_closed=1))
    gone = []
    s.on_change = lambda sid, change: gone.append(sid) if change == "delete" else None
    a, b = s.open("process", title="a"), s.open("process", title="b")
    s.close(a.id)
    s.close(b.id)
    s.open("process", title="c")              # prunes a: one finished stream kept
    assert gone == [a.id] and a.id not in s.bounded


def test_a_failing_listener_never_breaks_the_stream():
    s = Streams()
    s.on_change = lambda sid, change: 1 / 0
    m = s.open("process", title="x")
    s.append(m.id, "bytes", b"ok")
    assert s.get(m.id).last_seq == 1


# -- the reporter ---------------------------------------------------------------------

def test_reporter_reports_lifecycle_at_once_and_activity_at_most_every_few_seconds():
    async def body():
        s = Streams(host="w1")
        got = []
        r = Reporter(s, lambda change, row: got.append((change, row["id"], row["last_seq"])), every=0.05)
        m = s.open("terminal", title="sh", participants=["person:bake"])
        for i in range(20):
            s.append(m.id, "bytes", b"x")
        assert got == [("changed", m.id, 0)]            # appends wait for the timer
        await asyncio.sleep(0.15)
        assert got[1:] == [("changed", m.id, 20)]       # twenty appends, one report
        s.append(m.id, "bytes", b"y")
        s.close(m.id)                                   # at once, and the pending activity goes with it
        assert got[-1][0] == "changed" and got[-1][2] == 22
        await asyncio.sleep(0.1)
        assert len(got) == 3
        assert [row["id"] for row in r.rows()] == [m.id]
        r.close()
        assert s.on_change is None
    run(body())


def test_reporter_reports_deletes_with_the_row_and_without_a_loop_reports_now():
    s = Streams()
    got = []
    Reporter(s, lambda change, row: got.append((change, row["kind"])))
    m = s.open("process")
    s.append(m.id, "bytes", b"x")              # no running loop: reported at once
    s.delete(m.id)
    assert got == [("changed", "process"), ("changed", "process"), ("deleted", "process")]


# -- the index --------------------------------------------------------------------------

def test_a_host_owns_its_rows(index):
    assert index.put("w1", "pixel", _row(S1))
    assert not index.put("w2", "evil", _row(S1, title="hijack"))      # another host's id
    assert index.get(S1)["title"] == S1 and index.get(S1)["worker"] == "pixel"
    index.drop("w2", S1)                                              # not its row: nothing happens
    assert index.get(S1)
    assert index.put("w1", "pixel", _row(S1, title="renamed"))        # its own: replaced
    assert index.get(S1)["title"] == "renamed"
    index.drop("w1", S1)
    assert index.get(S1) is None


@pytest.mark.parametrize("row", [{"id": "nope", "kind": "terminal"}, {"id": S1, "kind": "Bad Kind"},
                                 {"id": S1}, {"kind": "terminal"}])
def test_malformed_rows_are_refused(index, row):
    assert not index.put("w1", "pixel", row)


def test_sync_forgets_what_a_host_no_longer_holds(index):
    index.put("w1", "pixel", _row(S1))
    index.put("w1", "pixel", _row(S2))
    index.put("w2", "mac", _row(S3))
    assert index.sync("w1", [S2]) == 1
    assert index.get(S1) is None and index.get(S2) and index.get(S3)


def test_query_pages_latest_activity_first_and_filters(index):
    for i in range(7):
        index.put("w1", "pixel", _row(f"s_{i:016x}", last_at=float(i), title=f"build {i}",
                                      kind="terminal" if i % 2 else "process"))
    index.put("w2", "mac", _row(S3, kind="session", title="house with bake", closed=5.0,
                                participants=["agent:house", "person:bake"], attrs={"cwd": "/srv/Rook"}))
    rows, nxt = index.query(limit=3)
    assert [r["title"] for r in rows] == ["build 6", "build 5", "build 4"] and nxt == 3
    seen = [r["id"] for r in rows]
    while nxt is not None:
        rows, nxt = index.query(limit=3, offset=nxt)
        seen += [r["id"] for r in rows]
    assert len(seen) == len(set(seen)) == 8
    assert {r["kind"] for r in index.query(kind="terminal")[0]} == {"terminal"}
    assert {r["worker"] for r in index.query(worker="mac")[0]} == {"mac"}
    assert S3 not in [r["id"] for r in index.query(open_only=True, limit=200)[0]]
    assert [r["id"] for r in index.query(text="ROOK")[0]] == [S3]          # attrs, any case
    assert [r["id"] for r in index.query(participant="agent:house")[0]] == [S3]
    assert index.query(text="100%")[0] == [] and index.query(text="_")[0] == []   # LIKE wildcards are text
    row = index.get(S3)
    assert row["person"] == "person:bake" and row["agent"] == "agent:house" and row["closed"] == 5.0


def test_rights_owners_everything_people_theirs_agents_alone_their_own(index):
    index.put("w1", "pixel", _row(S1, participants=["person:bake"]))
    index.put("w1", "pixel", _row(S2, participants=["person:ann", "agent:house"]))
    index.put("hub:agents", "rook", _row(S3, participants=["agent:house"], kind="session"), plugin="agents")
    ids = lambda viewer: sorted(r["id"] for r in index.query(viewer)[0])   # noqa: E731
    assert ids(None) == [S1, S2, S3]
    assert ids(("person", "person:ann")) == [S2]
    assert ids(("person", "person:bob")) == []
    assert ids(("agent", "agent:house")) == [S3]           # one with a person is that person's to see
    assert index.get(S1, ("person", "person:ann")) is None
    assert index.get(S3, ("agent", "agent:house"))["plugin"] == "agents"
    assert visible(("agent", "agent:house"), ["agent:house", "person:ann"]) is False


def test_the_index_survives_a_restart(tmp_path):
    ix = StreamIndex(str(tmp_path / "i.db"))
    ix.put("w1", "pixel", _row(S1))
    ix.close()
    ix = StreamIndex(str(tmp_path / "i.db"))
    assert ix.get(S1)["worker"] == "pixel"
    ix.close()


def test_kinds_default_rows_say_durable_for_sessions():
    s = Streams(durable=None)
    m = s.open("terminal")
    assert s.summary(m.id)["durable"] is False and KINDS["session"].durable
