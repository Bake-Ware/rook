"""Speakable names on console rooms, chat rooms and handoff threads: every
entry point takes the id or the name, a read may take one clear near miss, a
write needs the exact name, and replies carry the name beside the id."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from rook.band_mcp.chat_rooms import ChatStore
from rook.band_mcp.console_rooms import ConsoleStore
from rook.band_mcp.sessions import SessionStore
from rook.core import names

STATIC = "s" * 40


def rename(path, kind: str, rid: str, name: str) -> None:
    """Give a record a known name, so near misses are deterministic."""
    db = sqlite3.connect(str(path))
    db.execute("UPDATE record_names SET name=? WHERE kind=? AND id=?", (name, kind, rid))
    db.commit()
    db.close()


def spellings(name: str) -> list[str]:
    a, b, c = name.split("-")
    return [name, f"{a} {b} {c}", f"{a}_{b}_{c}", f"{a.upper()} {b.title()}-{c}"]


def all_names(path, kind: str) -> dict:
    db = sqlite3.connect(str(path))
    try:
        return dict(db.execute("SELECT id, name FROM record_names WHERE kind=?", (kind,)))
    finally:
        db.close()


def open_console(store: ConsoleStore, title="build the thing", worker="w1") -> dict:
    return store.open(title=title, worker=worker, worker_name="box", handle="h1",
                      cmd="make", pty=False, opened_by="agent:a")


# -- console rooms -----------------------------------------------------------------

@pytest.fixture
def console(tmp_path):
    s = ConsoleStore(str(tmp_path / "console.db"))
    yield s
    s.close()


def test_console_open_assigns_a_name_shown_everywhere(console):
    opened = open_console(console)
    rid, name = opened["room"], opened["name"]
    assert names.is_name(name)
    assert console.name_of(rid) == name and console.names_of([rid]) == {rid: name}
    assert console.get(rid)["name"] == name
    assert console.read(rid)["name"] == name
    assert console.list()["rooms"][0]["name"] == name
    assert console.search("thing")["results"][0]["name"] == name
    assert console.freeze(rid, summary="done")["name"] == name


def test_console_resolve_by_id_name_fuzzy(console, tmp_path):
    rid = open_console(console)["room"]
    other = open_console(console, "second")["room"]
    rename(tmp_path / "console.db", "console", rid, "brave-otter-lamp")
    rename(tmp_path / "console.db", "console", other, "quiet-river-stone")
    assert console.resolve_ref(rid, write=True).id == rid
    for ref in spellings("brave-otter-lamp"):
        got = console.resolve_ref(ref, write=True)
        assert (got.id, got.via) == (rid, "name")
    near = console.resolve_ref("brave otter lamps", write=False)
    assert (near.id, near.via) == (rid, "fuzzy")
    with pytest.raises(names.NameError_) as e:
        console.resolve_ref("brave otter lamps", write=True)
    assert e.value.payload()["candidates"] == [{"id": rid, "name": "brave-otter-lamp"}]


def test_console_ambiguous_near_miss_returns_candidates(console, tmp_path):
    a, b = open_console(console)["room"], open_console(console)["room"]
    rename(tmp_path / "console.db", "console", a, "brave-otter-lamp")
    rename(tmp_path / "console.db", "console", b, "brave-otter-lamb")
    with pytest.raises(names.NameError_) as e:
        console.resolve_ref("brave otter lame", write=False)
    assert {c["id"] for c in e.value.payload()["candidates"]} == {a, b}


def test_console_search_matches_names(console, tmp_path):
    rid = open_console(console, "unrelated title")["room"]
    open_console(console, "another")
    rename(tmp_path / "console.db", "console", rid, "brave-otter-lamp")
    for q in ("brave otter lamp", "Otter_Lamp", "brave"):
        res = console.search(q)
        assert res["ok"] and res["results"][0]["room"] == rid, (q, res)
        assert res["results"][0]["name"] == "brave-otter-lamp"
    json.dumps(res)   # no infinite scores leak out
    assert console.search("ter-lamp")["count"] == 0   # word starts only


def test_console_backfill_on_open(tmp_path):
    path = tmp_path / "console.db"
    s = ConsoleStore(str(path))
    ids = [open_console(s)["room"] for _ in range(3)]
    s.close()
    db = sqlite3.connect(str(path))
    names.drop(db)
    db.commit()
    db.close()
    s = ConsoleStore(str(path))
    got = all_names(path, "console")
    assert set(got) == set(ids) and all(names.is_name(n) for n in got.values())
    s.close()
    s = ConsoleStore(str(path))
    assert all_names(path, "console") == got
    s.close()


# -- chat rooms --------------------------------------------------------------------

@pytest.fixture
def chat(tmp_path):
    s = ChatStore(str(tmp_path / "chat.db"))
    yield s
    s.close()


def test_chat_start_assigns_a_name_shown_everywhere(chat):
    started = chat.start("planning", "agent:a", ["agent:b"])
    rid, name = started["room"], started["name"]
    assert names.is_name(name)
    assert chat.name_of(rid) == name and chat.names_of([rid]) == {rid: name}
    assert chat.room(rid)["name"] == name
    assert chat.send(rid, "agent:a", "hi", [], False)["name"] == name
    assert chat.read(rid, "agent:b")["name"] == name
    assert chat.rooms_for("agent:b")["rooms"][0]["name"] == name
    chat.send(rid, "agent:a", "again", [], False)
    assert chat.unread_summary("agent:b")[0]["name"] == name
    assert chat.join(rid, ["agent:c"])["name"] == name
    assert chat.delete(rid, None)["name"] == name


def test_chat_resolve_room(chat, tmp_path):
    rid, other, third = (chat.start(t, "agent:a", [])["room"] for t in "tuv")
    rename(tmp_path / "chat.db", "room", rid, "brave-otter-lamp")
    rename(tmp_path / "chat.db", "room", other, "brave-otter-lamb")
    rename(tmp_path / "chat.db", "room", third, "quiet-river-stone")
    assert chat.resolve_room(rid, write=True).id == rid
    for ref in spellings("brave-otter-lamp"):
        assert chat.resolve_room(ref, write=True).id == rid
    got = chat.resolve_ref("quiet river stones", write=False)
    assert (got.id, got.via) == (third, "fuzzy")
    with pytest.raises(names.NameError_):
        chat.resolve_room("quiet river stones", write=True)
    with pytest.raises(names.NameError_) as e:
        chat.resolve_room("brave otter lame", write=False)
    assert len(e.value.candidates) == 2


def test_chat_backfill_on_open(tmp_path):
    path = tmp_path / "chat.db"
    s = ChatStore(str(path))
    ids = [s.start(f"r{i}", "agent:a", [])["room"] for i in range(3)]
    s.close()
    db = sqlite3.connect(str(path))
    names.drop(db)
    db.commit()
    db.close()
    ChatStore(str(path)).close()
    got = all_names(path, "room")
    assert set(got) == set(ids)
    ChatStore(str(path)).close()
    assert all_names(path, "room") == got


# -- handoff threads ----------------------------------------------------------------

@pytest.fixture
def sessions(tmp_path):
    s = SessionStore(str(tmp_path / "sessions.db"))
    yield s
    s.close()


def test_handoff_threads_named_and_resolved(sessions, tmp_path):
    saved = sessions.save(thread_id=None, author="a", goal="ship it")
    tid, name = saved["thread_id"], saved["name"]
    assert names.is_name(name)
    # A second save on the thread keeps its name.
    assert sessions.save(thread_id=tid, author="a", goal="ship it", state="half")["name"] == name
    rename(tmp_path / "sessions.db", "handoff", tid, "brave-otter-lamp")
    for ref in spellings("brave-otter-lamp"):
        got = sessions.get(ref)
        assert got["ok"] and got["current"]["thread_id"] == tid, (ref, got)
        assert got["current"]["name"] == "brave-otter-lamp" and "resolved" not in got
        assert all(h["name"] == "brave-otter-lamp" for h in got["history"])
    fuzzy = sessions.get("brave otter lamps")
    assert fuzzy["ok"] and fuzzy["resolved"] == {"from": "brave otter lamps", "via": "fuzzy"}
    # Writes by name: exact only.
    upd = sessions.save(thread_id="Brave Otter Lamp", author="b", goal="ship it", state="done")
    assert upd["ok"] and upd["thread_id"] == tid
    bad = sessions.save(thread_id="brave otter lamps", author="b", goal="x")
    assert not bad["ok"] and bad["candidates"] == [{"id": tid, "name": "brave-otter-lamp"}]
    assert not sessions.close_thread("brave otter lamps")["ok"]
    assert sessions.list_recent()["threads"][0]["name"] == "brave-otter-lamp"
    closed = sessions.close_thread("brave_otter_lamp", "b", "finished")
    assert closed["ok"] and closed["thread_id"] == tid and closed["name"] == "brave-otter-lamp"
    assert sessions.close_thread(tid)["name"] == "brave-otter-lamp"


def test_handoff_custom_thread_ids_still_start_threads(sessions):
    # Any id of the caller's own (a chat room id, a three-word slug that is
    # not made of name words) starts a thread and gets a name.
    for custom in ("0123456789abcdef", "zzqx vvqk wwpj"):
        assert not names.is_name(custom)
        saved = sessions.save(thread_id=custom, author="a", goal="g")
        assert saved["ok"] and saved["thread_id"] == custom.strip() and names.is_name(saved["name"])
    # Name words that match nothing read as a mistyped name, not a new thread.
    word = names.adjectives()[0], names.nouns()[0], names.nouns()[1]
    bad = sessions.save(thread_id=" ".join(word), author="a", goal="g")
    assert not bad["ok"] and "named" in bad["error"]


def test_handoff_backfill_on_open(tmp_path):
    path = tmp_path / "sessions.db"
    s = SessionStore(str(path))
    for t in ("t1", "t2"):
        s.save(thread_id=t, author="a", goal="g")
        s.save(thread_id=t, author="a", goal="g2")
    s.close()
    db = sqlite3.connect(str(path))
    names.drop(db)
    db.commit()
    db.close()
    SessionStore(str(path)).close()
    got = all_names(path, "handoff")
    assert set(got) == {"t1", "t2"}
    SessionStore(str(path)).close()
    assert all_names(path, "handoff") == got


# -- MCP tools ------------------------------------------------------------------------

class Band:
    def __init__(self):
        self.workers = {"w1": {"worker_id": "w1", "name": "box", "band": "x",
                               "caps": ["proc.start", "proc.write", "proc.signal", "proc.close"],
                               "last_seen": 0}}
        self.calls = []

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None, **kw):
        self.calls.append((cap, args))
        return {"id": "c", "from": target, "ok": True, "result": {"ok": True}}


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    import rook.band_mcp.server as srv
    monkeypatch.setenv("ROOK_CHAT_NEW_ROOMS", "1")      # rooms are retired by default
    band = Band()
    m, _ = srv.build_server(band, public_url="https://mcp.example.com",
                            persist_path=str(tmp_path / "tokens.json"), static_token=STATIC,
                            journal_path=str(tmp_path / "journal.db"))

    async def tool(name, **args):
        out = await m._tool_manager._tools[name].fn(**args)
        return json.loads(out)
    return SimpleNamespace(tool=tool, band=band, console=m._rook_console, path=tmp_path)


@pytest.mark.asyncio
async def test_mcp_console_tools_take_names(mcp):
    rid = open_console(mcp.console)["room"]
    rename(mcp.path / "console.db", "console", rid, "brave-otter-lamp")
    listed = await mcp.tool("rook_console_list")
    assert listed["rooms"][0]["name"] == "brave-otter-lamp"
    read = await mcp.tool("rook_console_read", room="Brave Otter Lamp")
    assert read["room"] == rid and read["name"] == "brave-otter-lamp" and "resolved" not in read
    fuzzy = await mcp.tool("rook_console_read", room="brave otter lamps")
    assert fuzzy["room"] == rid and fuzzy["resolved"]["via"] == "fuzzy"
    bad = await mcp.tool("rook_console_write", room="brave otter lamps", text="y")
    assert not bad["ok"] and bad["candidates"] == [{"id": rid, "name": "brave-otter-lamp"}]
    assert not mcp.band.calls
    ok = await mcp.tool("rook_console_write", room="brave_otter_lamp", text="y")
    assert ok["ok"] and mcp.band.calls[-1][0] == "proc.write"
    assert not (await mcp.tool("rook_console_signal", room="brave otter lamps"))["ok"]
    assert (await mcp.tool("rook_console_signal", room="brave otter lamp"))["ok"]
    live = await mcp.tool("rook_console_close", room="brave otter lamp")
    assert not live["ok"] and "brave-otter-lamp (" in live["error"]
    assert not (await mcp.tool("rook_console_close", room="brave otter lamps", kill=True,
                               summary="s"))["ok"]
    frozen = await mcp.tool("rook_console_close", room="brave otter lamp", kill=True, summary="done")
    assert frozen["ok"] and frozen["room"] == rid and frozen["name"] == "brave-otter-lamp"
    found = await mcp.tool("rook_console_search", query="otter lamp")
    assert found["results"][0]["room"] == rid


@pytest.mark.asyncio
async def test_mcp_chat_tools_take_names(mcp):
    started = await mcp.tool("rook_chat_start", title="planning")
    rid, name = started["room"], started["name"]
    assert names.is_name(name)
    rename(mcp.path / "chat.db", "room", rid, "brave-otter-lamp")
    sent = await mcp.tool("rook_chat_send", room="Brave Otter Lamp", text="hello")
    assert sent["ok"] and sent["room"] == rid and sent["name"] == "brave-otter-lamp"
    bad = await mcp.tool("rook_chat_send", room="brave otter lamps", text="x")
    assert not bad["ok"] and bad["candidates"][0]["id"] == rid
    read = await mcp.tool("rook_chat_read", room="brave otter lamps")
    assert read["room"] == rid and read["resolved"]["via"] == "fuzzy"
    assert [m["text"] for m in read["messages"]] == ["hello"]
    rooms = await mcp.tool("rook_chat_rooms")
    assert rooms["rooms"][0]["name"] == "brave-otter-lamp"
    woke = await mcp.tool("rook_chat_wake", room="brave otter lamps", worker="box")
    assert not woke["ok"] and woke["candidates"]
    assert not (await mcp.tool("rook_chat_delete", room="brave otter lamps"))["ok"]
    gone = await mcp.tool("rook_chat_delete", room="brave-otter-lamp")
    assert gone["ok"] and gone["room"] == rid


@pytest.mark.asyncio
async def test_mcp_handoff_tools_take_names(mcp):
    saved = await mcp.tool("rook_handoff_save", goal="ship it", state="started")
    tid, name = saved["thread_id"], saved["name"]
    assert names.is_name(name)
    rename(mcp.path / "sessions.db", "handoff", tid, "brave-otter-lamp")
    got = await mcp.tool("rook_handoff_get", thread_id="BRAVE otter_lamp")
    assert got["current"]["thread_id"] == tid and got["current"]["name"] == "brave-otter-lamp"
    fuzzy = await mcp.tool("rook_handoff_get", thread_id="brave otter lamps")
    assert fuzzy["ok"] and fuzzy["resolved"]["via"] == "fuzzy"
    bad = await mcp.tool("rook_handoff_save", goal="g", thread_id="brave otter lamps")
    assert not bad["ok"] and bad["candidates"][0]["id"] == tid
    listed = await mcp.tool("rook_handoff_list")
    assert listed["threads"][0]["name"] == "brave-otter-lamp"
    assert not (await mcp.tool("rook_handoff_save", thread_id="brave otter lamps",
                               status="closed"))["ok"]
    closed = await mcp.tool("rook_handoff_save", thread_id="brave otter lamp", status="closed",
                            state="done")
    assert closed["ok"] and closed["thread_id"] == tid and closed["name"] == "brave-otter-lamp"


# -- chat.* caps on the hub ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_caps_take_names(tmp_path, monkeypatch):
    from rook.hub.node import HubNode
    monkeypatch.setenv("ROOK_CHAT_NEW_ROOMS", "1")
    node = HubNode(str(tmp_path), entry_points=False, band_max_risk="write",
                   chat=ChatStore(str(tmp_path / "chat.db")))
    started = await node.dispatch("chat.write", {"action": "start", "title": "t"}, "agent:a")
    rid = started["result"]["room"]
    assert names.is_name(started["result"]["name"])
    rename(tmp_path / "chat.db", "room", rid, "brave-otter-lamp")
    sent = await node.dispatch("chat.write", {"action": "send", "room": "Brave Otter Lamp",
                                              "text": "hi"}, "agent:a")
    assert sent["ok"] and sent["result"]["room"] == rid
    bad = await node.dispatch("chat.write", {"action": "send", "room": "brave otter lamps",
                                             "text": "x"}, "agent:a")
    assert bad["result"]["ok"] is False and bad["result"]["candidates"][0]["id"] == rid
    read = await node.dispatch("chat.read", {"action": "read", "room": "brave otter lamps"},
                               "agent:a")
    assert read["result"]["room"] == rid and read["result"]["resolved"]["via"] == "fuzzy"
    assert read["result"]["name"] == "brave-otter-lamp"
    rooms = await node.dispatch("chat.read", {"action": "rooms"}, "agent:a")
    assert rooms["result"]["rooms"][0]["name"] == "brave-otter-lamp"
    assert (await node.dispatch("chat.delete", {"room": "brave otter lamps"},
                                "agent:a"))["result"]["ok"] is False
    gone = await node.dispatch("chat.delete", {"room": "brave_otter_lamp"}, "agent:a")
    assert gone["ok"] and gone["result"]["room"] == rid


# -- dashboard chat routes ------------------------------------------------------------------

class Req:
    def __init__(self, query=None, body=None):
        self.query = query or {}
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_dashboard_chat_routes_take_names(chat, tmp_path):
    from rook.remote.bootstrap import CombinedServer
    srv = object.__new__(CombinedServer)
    srv._chat = chat

    async def mask(x):
        return x
    srv._bridge_mask = mask
    rid = chat.start("t", "user:operator", [])["room"]
    rename(tmp_path / "chat.db", "room", rid, "brave-otter-lamp")

    def body(resp):
        return json.loads(resp.text)
    sent = body(await srv._api_chat_send(Req(body={"room": "Brave Otter Lamp", "text": "hi"})))
    assert sent["ok"] and sent["room"] == rid
    bad = body(await srv._api_chat_send(Req(body={"room": "brave otter lamps", "text": "x"})))
    assert not bad["ok"] and bad["candidates"][0]["id"] == rid
    read = body(await srv._api_chat_read(Req(query={"room": "brave otter lamps"})))
    assert read["room"] == rid and read["resolved"]["via"] == "fuzzy" and read["name"]
    rooms = body(await srv._api_chat_rooms(Req()))
    assert rooms["rooms"][0]["name"] == "brave-otter-lamp"
    assert not body(await srv._api_chat_delete(Req(body={"room": "brave otter lamps"})))["ok"]
    assert body(await srv._api_chat_delete(Req(body={"room": "brave otter lamp"})))["ok"]


# -- rook rooms -------------------------------------------------------------------------------

def test_cli_rooms_takes_and_shows_names(monkeypatch, tmp_path, capsys):
    from rook.worker import localcli
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROOK_WORKER_HOME", str(tmp_path / "state"))
    seen = []
    replies = {
        "rooms": {"ok": True, "result": {"rooms": [{"room": "r1", "name": "brave-otter-lamp",
                                                    "title": "Kitchen", "unread": 0}]}},
        "read": {"ok": True, "result": {"ok": False, "error": "no room is named 'brave-otter-lamps'",
                                        "candidates": [{"id": "r1", "name": "brave-otter-lamp"}]}},
    }

    def hub_call(cap, args, timeout=20.0, via="auto"):
        seen.append(args)
        return replies[args["action"]], "token"
    monkeypatch.setattr(localcli, "hub_call", hub_call)
    assert localcli.main(["rooms"]) == 0
    assert "Kitchen  (brave-otter-lamp)" in capsys.readouterr().out
    code = localcli.main(["rooms", "brave", "otter", "lamps"])
    err = capsys.readouterr().err
    assert code == 1 and seen[-1]["room"] == "brave otter lamps"
    assert "brave-otter-lamp  (r1)" in err
