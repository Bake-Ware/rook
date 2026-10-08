"""Sessions workstream F (docs/design/sessions.md §4.F): console rooms run on
Rook terminals (work.stream.*) with proc.* as the fallback, and sessions
started for a task claim it, link to it and leave a note when they end."""
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from rook.band_mcp.console_pump import ConsolePump
from rook.band_mcp.console_rooms import ConsoleStore
from rook.hub.plugins.knowledge.hygiene import HygieneEngine
from rook.hub.plugins.knowledge.service import KnowledgeService
from rook.hub.plugins.knowledge.store import KnowledgeStore
from rook.worker import termwire
from rook.worker.plugins import sessions as sessions_plugin
from rook.worker.plugins.terminals import TerminalsPlugin

posix = pytest.mark.skipif(os.name != "posix", reason="PTYs are POSIX-only")
AGENT = {'id': 'codex.codex.gpubox', 'kind': 'agent', 'label': 'codex'}


def rid():
    return uuid.uuid4().hex


@pytest_asyncio.fixture
async def terms(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("ROOK_WORK_TERM_DIR", str(tmp_path / "terms"))
    p = TerminalsPlugin()
    yield p
    await p.stop()


async def output(p, tid, want, timeout=10):
    out, cursor = b"", 0
    async with asyncio.timeout(timeout):
        while want.encode() not in out:
            r = await p.read(tid, cursor, wait=2)
            out += termwire.decode(r["enc"], r["data"])
            cursor = r["next"]
            if r["eof"]:
                break
    return out.decode(errors="replace")


# -- the worker: commands in Rook terminals ----------------------------------------

@posix
@pytest.mark.asyncio
async def test_terminal_runs_a_command_with_task_and_room(terms, tmp_path):
    r = await terms.open(harness="shell", cwd=str(tmp_path), argv=["sh", "-c", "echo got-$ROOK_TASK pager=$PAGER x=$X"],
                         env={"X": "1"}, task="t_abc", room="r0123456789abcde", title="check the thing")
    assert r["task"] == "t_abc" and r["room"] == "r0123456789abcde" and r["cmd"].startswith("sh -c")
    assert r["title"] == "check the thing"
    out = await output(terms, r["id"], "x=1")
    assert "got-t_abc pager=cat x=1" in out
    # cmd goes through /bin/sh -c; the title defaults to the command.
    r2 = await terms.open(harness="shell", cwd=str(tmp_path), cmd="echo one && echo two")
    assert r2["title"] == "echo one && echo two" and "two" in await output(terms, r2["id"], "two")
    listed = {t["id"]: t for t in terms.list_terms()["terminals"]}
    assert listed[r["id"]]["task"] == "t_abc" and listed[r2["id"]]["task"] is None


@posix
@pytest.mark.asyncio
async def test_terminal_command_arguments_are_checked(terms, tmp_path):
    for bad in (dict(harness="claude", argv=["ls"]), dict(argv=["ls"], cmd="ls"),
                dict(argv=["ls"], resume="abc"), dict(argv="ls"), dict(argv=["ls"], task="no spaces"),
                dict(argv=["ls"], room="bad room")):
        with pytest.raises(ValueError):
            await terms.open(**{"harness": "shell", "cwd": str(tmp_path), **bad})
    # A plain shell still works as before.
    r = await terms.open(harness="shell", cwd=str(tmp_path))
    assert r["cmd"] is None and r["task"] is None


def test_session_record_links_task_and_console_room():
    term = {"id": "t1", "running": True, "session": "s1", "task": "t_abc", "room": "r1", "title": "x"}
    rec = sessions_plugin.record("shell", "t1", term=term)
    assert rec["links"] == {"work_session": "s1", "task": "t_abc", "console_room": "r1"}
    assert sessions_plugin.record("shell", "t2", term={"id": "t2", "running": True})["links"] == {}


# -- the pump: rooms on work.stream, proc.* fallback, watched terminals --------------------

class TermBand:
    """Routes band calls into a real TerminalsPlugin (and a scripted proc.*)."""

    def __init__(self, plugin, *, commands=True, proc=True):
        self.plugin = plugin
        caps = ["work.stream.open", "work.stream.read", "work.stream.write", "work.stream.signal",
                "work.stream.close", "work.stream.list", "shell.exec"]
        if proc:
            caps += ["proc.start", "proc.read", "proc.write", "proc.signal", "proc.close"]
        hb = {"work": {"harnesses": ["shell"], **({"commands": 1} if commands else {})}}
        self.workers = {"w1": {"worker_id": "w1", "name": "gpu-box", "band": "deadbeef", "last_seen": 0,
                               "caps": caps, "hb": hb}}
        self.sent = []
        self.refuse_open = None

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        args = dict(args or {})
        self.sent.append((cap, args))
        p = self.plugin
        fns = {} if p is None else {
            "work.stream.open": p.open, "work.stream.read": p.read, "work.stream.write": p.write,
            "work.stream.signal": p.signal, "work.stream.close": p.close, "work.stream.list": p.list_terms}
        if cap == "work.stream.open" and self.refuse_open:
            return {"ok": False, "from": target, "error": self.refuse_open}
        if cap == "proc.start":
            return {"ok": True, "from": target, "result": {"ok": True, "handle": "h1", "pid": 1,
                                                           "cmd": args.get("cmd") or " ".join(args.get("argv") or [])}}
        if cap.startswith("proc."):
            return {"ok": True, "from": target, "result": {"ok": True, "handle": args.get("handle")}}
        if cap not in fns:
            return {"ok": True, "from": target, "result": {}}
        try:
            result = fns[cap](**args)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:  # the worker's error reply
            return {"ok": False, "from": target, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "from": target, "result": result}


@pytest.fixture
def store(tmp_path):
    s = ConsoleStore(str(tmp_path / "console.db"))
    yield s
    s.close()


async def pump_until(pump, test, timeout=10):
    async with asyncio.timeout(timeout):
        while not test():
            await asyncio.sleep(0.05)


@posix
@pytest.mark.asyncio
async def test_pump_drains_a_terminal_room_and_reports_its_end(terms, store, tmp_path, monkeypatch):
    import rook.band_mcp.console_pump as cp
    monkeypatch.setattr(cp, "POLL_IDLE", 0.05)
    room = store.new_id()
    t = await terms.open(harness="shell", cwd=str(tmp_path), room=room,
                         argv=["sh", "-c", "printf 'caf\\303\\251 '; echo ready; sleep 0.2; echo done; exit 4"])
    store.open(title="terminal room", worker="w1", worker_name="gpu-box", handle=t["id"], cmd=t["cmd"],
               pty=True, opened_by="agent:test", transport="term", rid=room)
    ended = []
    pump = ConsolePump(TermBand(terms), store, on_end=lambda *a: ended.append(a))
    pump.start()
    try:
        await pump_until(pump, lambda: store.get(room)["state"] != "live")
    finally:
        await pump.stop()
    got = store.read(room)
    text = "\n".join(line["text"] for line in got["lines"])
    assert "café ready" in text and "done" in text
    assert got["state"] == "closing" and got["exit_code"] == 4
    assert got["transport"] == "term" and got["terminal"] == t["id"]
    assert ended == [("console", room, "process exited (code 4)", 4)]


@pytest.mark.asyncio
async def test_pump_closes_a_terminal_room_whose_terminal_is_gone(store):
    class Gone:
        async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
            assert cap == "work.stream.read"
            return {"ok": False, "error": "ValueError: no such terminal: t9"}
    room = store.open(title="lost", worker="w1", worker_name="gpu-box", handle="t9", cmd="x", pty=True,
                      opened_by="a", transport="term")["room"]
    ended = []
    pump = ConsolePump(Gone(), store, on_end=lambda *a: ended.append(a))
    await pump._drain(store.live_rooms()[0])
    assert store.get(room)["state"] == "closing" and ended[0][:2] == ("console", room)


@pytest.mark.asyncio
async def test_pump_still_reads_proc_rooms_and_old_databases(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    # A console.db from before transports existed.
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE rooms (id TEXT PRIMARY KEY, title TEXT, worker TEXT, worker_name TEXT, handle TEXT, "
               "cmd TEXT, pty INTEGER, state TEXT, opened_by TEXT, participants TEXT, created REAL, "
               "last_activity REAL, ended REAL, exit_code INTEGER, bytes INTEGER, cursor INTEGER, summary TEXT)")
    db.execute("INSERT INTO rooms VALUES ('r1','old','w1','gpu','h1','x',0,'live','a','[]',1,1,NULL,NULL,0,0,NULL)")
    db.commit()
    db.close()
    store = ConsoleStore(str(path))
    assert store.live_rooms() == [{"room": "r1", "worker": "w1", "handle": "h1", "cursor": 0, "transport": "proc"}]

    class Proc:
        async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
            assert cap == "proc.read" and args["handle"] == "h1"
            return {"ok": True, "result": {"ok": True, "chunk": "bye\n", "next_cursor": 4, "eof": True,
                                           "exit_code": 0}}
    ended = []
    await ConsolePump(Proc(), store, on_end=lambda *a: ended.append(a))._drain(store.live_rooms()[0])
    assert [line["text"] for line in store.read("r1")["lines"]][0] == "bye"
    assert store.get("r1")["state"] == "closing" and ended[0][:2] == ("console", "r1")
    store.close()


@posix
@pytest.mark.asyncio
async def test_pump_reports_watched_terminals_that_end(terms, store, tmp_path):
    t = await terms.open(harness="shell", cwd=str(tmp_path), argv=["sh", "-c", "sleep 30"])
    store.watch("w1", t["id"], ref=f"w1/shell/{t['id']}", task="t_1", title="x")
    store.watch("w1", "gone00000000", ref="w1/shell/gone00000000", task="t_1")
    ended = []
    pump = ConsolePump(TermBand(terms), store, on_end=lambda *a: ended.append(a))
    assert await pump.check_watched() == 1          # the unknown one is gone; the live one stays
    assert ended[0][:2] == ("session", "w1/shell/gone00000000")
    await terms.close(t["id"])
    assert await pump.check_watched() == 1
    assert ended[1][:2] == ("session", f"w1/shell/{t['id']}") and store.watched() == []


# -- task linking through the MCP ---------------------------------------------------------

@asynccontextmanager
async def mcp_session(tmp_path, monkeypatch, band):
    from rook.band_mcp.server import build_server
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    mcp, store = build_server(band, public_url="https://mcp.example.com", persist_path=str(tmp_path / "tokens.json"),
                              static_token="static-token-0123456789abcdef",
                              journal_path=str(tmp_path / "journal.db"))
    token = store.mint_api_token("codex")
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token["token"],
                     "X-Rook-Host": "gpubox"}) as http:
        r = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "codex-mcp-client", "version": "1"}}})
        http.headers["mcp-session-id"] = r.headers["mcp-session-id"]
        await http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        async def tool(name, **args):
            r = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": name, "arguments": args}})
            body = r.json() if r.headers["content-type"].startswith("application/json") else json.loads(
                next(l[5:] for l in r.text.splitlines() if l.startswith("data:")))
            text = body["result"]["content"][0]["text"]
            try:
                return json.loads(text)
            except ValueError:
                return text

        async def new_task(title="Set up the model"):
            c = (await tool("rook_concept", action="create", request_id=rid(), data={"title": "C " + rid()[:6]}))["result"]
            p = (await tool("rook_project", action="create", request_id=rid(),
                            data={"title": "P", "parent": c["id"]}))["result"]
            return (await tool("rook_task", action="create", request_id=rid(),
                               data={"title": title, "parent": p["id"]}))["result"]
        yield SimpleNamespace(tool=tool, mcp=mcp, band=band, new_task=new_task,
                              console=mcp._rook_console, knowledge=mcp._rook_knowledge)


def claims(env, task_id):
    with env.knowledge.store.db(False) as db:
        return [dict(r) for r in db.execute("SELECT actor, provider_session FROM claims WHERE task=? "
                                            "AND released IS NULL", (task_id,))]


def links(env, task_id, kind):
    with env.knowledge.store.db(False) as db:
        return [r["ref"] for r in db.execute("SELECT ref FROM links WHERE record=? AND kind=? AND retracts IS NULL",
                                             (task_id, kind))]


def notes(env, task_id):
    return [json.loads(e["data"]) if isinstance(e.get("data"), str) else e.get("data")
            for e in env.knowledge.store.get("default", task_id, events=50)["events"] if e["action"] == "note"]


@posix
@pytest.mark.asyncio
async def test_console_room_runs_on_a_terminal_and_task_id_claims_the_task(terms, tmp_path, monkeypatch):
    band = TermBand(terms)
    async with mcp_session(tmp_path, monkeypatch, band) as env:
        task = await env.new_task()
        opened = await env.tool("rook_console_open", worker="gpu-box", task="set up the model", task_id=task["slug"],
                                argv=["sh", "-c", "read x; echo got-$x; exit 2"], cwd=str(tmp_path))
        # Same reply shape as before, plus the terminal it runs in.
        assert opened["ok"] and opened["state"] == "live" and opened["task"] == task["id"]
        assert opened["handle"] == opened["terminal"] and opened["room"]
        sent = dict(band.sent)["work.stream.open"]
        assert sent["room"] == opened["room"] and sent["task"] == task["id"] and sent["title"] == "set up the model"
        assert not any(c == "proc.start" for c, _ in band.sent)
        # Claimed by the caller, and the room is linked to the task.
        assert len(claims(env, task["id"])) == 1
        assert links(env, task["id"], "console") == [opened["room"]]
        # The terminal shows on the worker's session list with its room and task.
        listed = next(t for t in terms.list_terms()["terminals"] if t["id"] == opened["terminal"])
        assert listed["room"] == opened["room"] and listed["task"] == task["id"]
        # Writes go to the terminal with Enter as CR; replies keep the proc shape.
        w = await env.tool("rook_console_write", room=opened["room"], text="hello")
        assert w["ok"] and w["handle"] == opened["terminal"]
        assert band.sent[-1] == ("work.stream.write", {"data": "hello\r", "id": opened["terminal"]})
        # The pump fills the room and reports the end: the task gets a note
        # and its claimant a request for a handoff.
        pump = ConsolePump(band, env.console, on_end=env.mcp._rook_session_end)
        pump.start()
        try:
            await pump_until(pump, lambda: env.console.get(opened["room"])["state"] != "live")
        finally:
            await pump.stop()
        read = await env.tool("rook_console_read", room=opened["room"])
        assert any("got-hello" in line["text"] for line in read["lines"]) and read["exit_code"] == 2
        noted = notes(env, task["id"])
        assert noted and "process exited (code 2)" in noted[-1]["text"]
        findings = (await env.tool("rook_task", action="hygiene", data={"mine": True}))["result"]["findings"]
        assert [f["kind"] for f in findings] == ["session_ended"] and "handoff" in findings[0]["text"]
        # The task is still in progress: nothing closes it.
        assert env.knowledge.store.get("default", task["id"])["state"] == "in_progress"
        assert (await env.tool("rook_console_close", room=opened["room"], summary="model set up"))["state"] == "frozen"


@posix
@pytest.mark.asyncio
async def test_console_signal_and_kill_go_to_the_terminal(terms, tmp_path, monkeypatch):
    band = TermBand(terms)
    async with mcp_session(tmp_path, monkeypatch, band) as env:
        opened = await env.tool("rook_console_open", worker="gpu-box", task="wait a while", cmd="sleep 30",
                                cwd=str(tmp_path))
        sig = await env.tool("rook_console_signal", room=opened["room"], sig="INT")
        assert sig["ok"] and band.sent[-1] == ("work.stream.signal", {"sig": "INT", "id": opened["terminal"]})
        await env.tool("rook_console_close", room=opened["room"], kill=True, summary="stopped it")
        assert band.sent[-1] == ("work.stream.close", {"id": opened["terminal"]})
        assert env.console.get(opened["room"])["state"] == "frozen"


@pytest.mark.asyncio
async def test_console_falls_back_to_proc(tmp_path, monkeypatch):
    # A worker whose terminals do not take commands (older build): proc.*.
    band = TermBand(None, commands=False)
    async with mcp_session(tmp_path, monkeypatch, band) as env:
        opened = await env.tool("rook_console_open", worker="gpu-box", task="old worker", cmd="make", pty=True)
        assert opened["ok"] and opened["handle"] == "h1" and "terminal" not in opened
        assert [c for c, _ in band.sent if c in ("work.stream.open", "proc.start")] == ["proc.start"]
        assert env.console.get(opened["room"])["transport"] == "proc"
        await env.tool("rook_console_write", room=opened["room"], text="y")
        assert band.sent[-1] == ("proc.write", {"data": "y", "newline": True, "handle": "h1"})
    # A worker whose terminals are all busy: proc.* too.
    band = TermBand(None)
    band.refuse_open = "ValueError: too many live terminals (max 8); close one first"
    async with mcp_session(tmp_path / "b", monkeypatch, band) as env:
        opened = await env.tool("rook_console_open", worker="gpu-box", task="busy worker", cmd="make")
        assert opened["ok"] and opened["handle"] == "h1"
        assert [c for c, _ in band.sent if c in ("work.stream.open", "proc.start")] == ["work.stream.open", "proc.start"]
    # Any other start failure is reported, not retried elsewhere.
    band = TermBand(None)
    band.refuse_open = "ValueError: working directory does not exist: /nope"
    async with mcp_session(tmp_path / "c", monkeypatch, band) as env:
        failed = await env.tool("rook_console_open", worker="gpu-box", task="bad dir", cmd="make", cwd="/nope")
        assert failed["ok"] is False and "does not exist" in failed["error"]


@pytest.mark.asyncio
async def test_console_task_id_must_be_a_task(tmp_path, monkeypatch):
    band = TermBand(None, commands=False)
    async with mcp_session(tmp_path, monkeypatch, band) as env:
        out = await env.tool("rook_console_open", worker="gpu-box", task="x", cmd="make", task_id="no-such-task")
        assert "could not claim" in str(out) and not band.sent


@posix
@pytest.mark.asyncio
async def test_rook_call_terminal_for_a_task_claims_links_and_notes_its_end(terms, tmp_path, monkeypatch):
    band = TermBand(terms)
    async with mcp_session(tmp_path, monkeypatch, band) as env:
        task = await env.new_task("Review the branch")
        reply = await env.tool("rook_call", cap="work.stream.open", worker="gpu-box",
                               args={"harness": "shell", "cwd": str(tmp_path), "task": task["slug"],
                                     "title": "review"})
        tid = reply["result"]["id"]
        assert reply["_task"] == task["id"] and reply["result"]["task"] == task["slug"]
        ref = f"w1/shell/{tid}"
        assert links(env, task["id"], "session") == [ref] and len(claims(env, task["id"])) == 1
        assert [w["term"] for w in env.console.watched()] == [tid]
        # It ends; the pump's watch notes it on the task.
        await terms.close(tid)
        pump = ConsolePump(band, env.console, on_end=env.mcp._rook_session_end)
        assert await pump.check_watched() == 1
        noted = notes(env, task["id"])
        assert noted[-1]["session"] == {"kind": "session", "ref": ref} and "ended" in noted[-1]["text"]
        # An unknown task is reported on the reply, and the terminal still runs.
        bad = await env.tool("rook_call", cap="work.stream.open", worker="gpu-box",
                             args={"harness": "shell", "cwd": str(tmp_path), "task": "no-such-task"})
        assert bad["ok"] and "could not claim" in bad["_task_error"]


# -- the hub side of a session's end ---------------------------------------------------------

@pytest.fixture
def kb(tmp_path):
    s = KnowledgeStore(tmp_path / "knowledge.db")
    engine = HygieneEngine(s, conf=lambda: {})

    def create(kind, title, parent=None):
        return s.mutate("default", AGENT, rid(), "create", {"kind": kind, "title": title, "parent": parent})
    c = create("concept", "C")
    p = create("project", "P", c["id"])
    t = create("task", "T", p["id"])
    return SimpleNamespace(s=s, h=engine, task=t, create=create)


def test_linked_session_end_notes_open_tasks_only(kb):
    kb.s.mutate("default", AGENT, rid(), "claim", {"id": kb.task["id"]})
    kb.s.auto_link(AGENT, "console", "room1", task=kb.task["id"])
    assert kb.h.on_linked_session_end("console", "room1", "process exited (code 0)") == [kb.task["id"]]
    assert [f["kind"] for f in kb.h.open([kb.task["id"]], AGENT["id"])] == ["session_ended"]
    assert kb.h.on_linked_session_end("console", "other-room", "x") == []
    assert kb.h.on_linked_session_end("journal", "room1", "x") == []   # only session kinds
    # A finished task gets no note.
    done = kb.create("task", "Done one", kb.task["parent"])
    kb.s.auto_link(AGENT, "session", "w1/shell/t1", task=done["id"])
    with kb.s.db() as db:
        db.execute("UPDATE records SET state='cancelled' WHERE id=?", (done["id"],))
    assert kb.h.on_linked_session_end("session", "w1/shell/t1", "x") == []


@pytest.mark.asyncio
async def test_a_session_end_note_asks_for_a_handoff(kb):
    human = {"id": "human:u1", "kind": "human", "label": "Bake"}
    svc = KnowledgeService(kb.s.path, lambda: None)
    svc.hygiene = kb.h
    await svc.dispatch("claim", None, "task", kb.task["id"], request_id=rid(), actor=human)
    await svc.dispatch("note", None, "task", kb.task["id"], data={"text": "Session ended", "session_end": "w1/claude/t1"},
                       request_id=rid(), actor=human)
    found = kb.h.open([kb.task["id"]], human["id"])
    assert [f["kind"] for f in found] == ["session_ended"] and found[0]["data"] == {"session": "w1/claude/t1"}
    # A plain note asks for nothing.
    other = kb.create("task", "Other", kb.task["parent"])
    await svc.dispatch("note", None, "task", other["id"], data={"text": "fyi"}, request_id=rid(), actor=human)
    assert kb.h.open([other["id"]]) == []
