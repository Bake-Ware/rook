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


async def pump_until(pump, test, timeout=10):
    async with asyncio.timeout(timeout):
        while not test():
            await asyncio.sleep(0.05)


# -- task linking through the MCP ---------------------------------------------------------


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


# -- the hub side of a session's end ---------------------------------------------------------


