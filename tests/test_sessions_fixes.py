"""Sessions page fixes after the first live deploy: the hub's cached list,
follow(tail=), the cached transcript scans behind a fast sessions.list, the
two-minute "may still be running" rule and unreadable Claude PID markers."""
import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_band_management import portal  # noqa: F401 — fixture
from rook.worker import agent_activity as aa
from rook.worker.plugins import claude_history as ch
from rook.worker.plugins import sessions as sp
from rook.worker.plugins import terminals
from rook.worker.plugins.claude_history import ClaudeHistoryPlugin

C1 = "11111111-1111-4111-8111-111111111111"
C2 = "22222222-2222-4222-8222-222222222222"
C3 = "33333333-3333-4333-8333-333333333333"


def rec_user(text, cwd="/srv/app"):
    return {"type": "user", "cwd": cwd, "timestamp": "2026-10-08T10:00:00Z",
            "message": {"role": "user", "content": text}}


def rec_call(n):
    return {"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": f"tu{n}", "name": "Bash", "input": {"command": f"make {n}"}}]}}


def rec_result(n, text, error=False):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"tu{n}", "content": text, "is_error": error}]}}


def rec_reply(text):
    return {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn",
                                             "content": [{"type": "text", "text": text}]}}


def write(path, records, age=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    if age is not None:
        then = time.time() - age
        os.utime(path, (then, then))
    return path


@pytest.fixture
def claude_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(ch, "_default_root", lambda: root)
    monkeypatch.setattr(ClaudeHistoryPlugin, "_default_root", staticmethod(lambda: root))
    return root


# -- follow(tail=) -----------------------------------------------------------------------

def test_follow_tail_starts_at_the_end_in_one_page(claude_root):
    records = []
    for n in range(100):
        records += [rec_user(f"step {n}"), rec_call(n), rec_result(n, "x" * (9000 if n == 98 else 20), error=n == 97),
                    rec_reply(f"done {n}")]
    path = write(claude_root / "-srv-app" / f"{C1}.jsonl", records, age=3600)
    plugin = ClaudeHistoryPlugin()
    page = plugin._follow(C1, tail=40)
    assert page["ok"] and page["tail_from"] == page["replace_from"] == 360 and page["total_messages"] == 400
    msgs = page["messages"]
    assert [m["index"] for m in msgs] == list(range(360, 400)) and not page["truncated"]
    assert msgs[-1]["content"] == "done 99" and msgs[-1]["role"] == "assistant"
    # Tool results are marked, failures flagged, long ones clipped (one request, no paging).
    results = [m for m in msgs if m.get("kind") == "tool_result"]
    assert len(results) == 10 and all(m["role"] == "user" for m in results)
    assert any(m.get("error") for m in results)
    big = next(m for m in results if m.get("clipped"))
    assert len(big["content"]) == ch.TAIL_CLIP and big["clipped"] == 9000 - ch.TAIL_CLIP
    assert "[tool_use: Bash]" in msgs[1]["content"] and '"command": "make 90"' in msgs[1]["content"]
    # Then only what is new.
    assert plugin._follow(C1, offset=400, version=page["version"]) == {"ok": True, "unchanged": True,
                                                                       "version": page["version"]}
    with path.open("a") as f:
        f.write(json.dumps(rec_user("one more")) + "\n")
    more = plugin._follow(C1, offset=400, version=page["version"])
    assert more["replace_from"] == 400 and [m["content"] for m in more["messages"]] == ["one more"]
    # A short transcript: the tail is all of it.
    write(claude_root / "-srv-app" / f"{C2}.jsonl", [rec_user("hi"), rec_reply("hello")])
    short = plugin._follow(C2, tail=40)
    assert short["tail_from"] == 0 and [m["content"] for m in short["messages"]] == ["hi", "hello"]


def test_tail_page_budget_keeps_whole_messages(claude_root):
    records = [rec_reply("y" * 3999) for _ in range(30)]
    write(claude_root / "-srv-app" / f"{C1}.jsonl", records)
    page = ClaudeHistoryPlugin()._follow(C1, tail=30)
    # 64,000 characters: 16 whole messages, the rest on the next (ordinary) page.
    assert len(page["messages"]) == 16 and page["truncated"] and page["next_offset"] == 16
    assert page["next_content_offset"] == 0 and not any(m.get("clipped") for m in page["messages"])


@pytest.mark.asyncio
async def test_sessions_follow_passes_tail_only_when_asked():
    calls = []

    class Reg:
        def has(self, cap):
            return True

        async def call(self, cap, **kw):
            calls.append((cap, kw))
            return {"ok": True, "messages": []}
    p = sp.SessionsPlugin()
    p.bind_worker(SimpleNamespace(registry=Reg(), worker_id="w1", name="h"))
    await p.follow("claude", C1, offset=2, version="v")
    await p.follow("claude", C1, tail=500)
    assert calls == [("claude-history.follow", {"session_id": C1, "offset": 2, "version": "v"}),
                     ("claude-history.follow", {"session_id": C1, "offset": 0, "version": "", "tail": 200})]


# -- cached scans (why sessions.list was slow) ---------------------------------------------

def test_scan_reads_only_what_a_log_appended(claude_root, monkeypatch):
    path = write(claude_root / "-srv-app" / f"{C1}.jsonl",
                 [rec_user("Fix the build"), rec_call(1), rec_result(1, "ok")])
    first, activity = ch.scan_session(path)
    assert first == ch._session_meta(path) and activity == "working"
    reads = []
    real = ch._complete_lines
    monkeypatch.setattr(ch, "_complete_lines", lambda p, pos: (reads.append(pos), real(p, pos))[1])
    assert ch.scan_session(path)[0] == first and reads == []        # unchanged: no read at all
    size = path.stat().st_size
    with path.open("a") as f:
        f.write(json.dumps(rec_reply("Fixed.")) + "\n")
    meta, activity = ch.scan_session(path)
    assert reads == [size] and activity == "ready"                  # read on from the old end
    assert meta == ch._session_meta(path) and meta["message_count"] == 4 and meta["title"] == "Fix the build"
    # A half-written last line counts once it parses, and is read again next time.
    with path.open("a") as f:
        f.write(json.dumps(rec_user("and the docs")))
    meta, activity = ch.scan_session(path)
    assert meta["message_count"] == 5 and activity == "working"
    with path.open("a") as f:
        f.write("\n" + json.dumps(rec_reply("Done.")) + "\n")
    meta, activity = ch.scan_session(path)
    assert meta == ch._session_meta(path) and meta["message_count"] == 6 and activity == "ready"
    # Rewritten in place: read again from the start.
    write(path, [rec_user("Fresh start")])
    meta, _ = ch.scan_session(path)
    assert meta["message_count"] == 1 and meta["title"] == "Fresh start"


def test_pull_reads_unchanged_logs_once(claude_root, monkeypatch):
    for n, sid in enumerate((C1, C2, C3)):
        write(claude_root / "-srv-app" / f"{sid}.jsonl", [rec_user(f"task {n}"), rec_reply("ok")], age=600 + n)
    plugin = ClaudeHistoryPlugin()
    opened = []
    real_open = open

    def counting_open(file, *a, **kw):
        if str(file).endswith(".jsonl"):
            opened.append(str(file))
        return real_open(file, *a, **kw)
    monkeypatch.setattr("builtins.open", counting_open)
    first = plugin._pull(limit=50)
    assert first["total"] == 3 and len(opened) == 3      # one pass each (was two: metadata, then activity)
    assert [s["title"] for s in first["sessions"]] == ["task 0", "task 1", "task 2"]
    assert all(s["activity"] == "ready" for s in first["sessions"])
    opened.clear()
    assert plugin._pull(limit=50)["sessions"] == first["sessions"] and opened == []


# -- "may still be running" ---------------------------------------------------------------

class Reg:
    def __init__(self, sessions):
        self.sessions = sessions

    def has(self, cap):
        return cap in ("claude-history.pull",)

    async def call(self, cap, **kw):
        return {"ok": True, "total": len(self.sessions), "sessions": self.sessions}


@pytest.fixture
def catalog(monkeypatch, tmp_path, claude_root):
    monkeypatch.setenv("ROOK_WORKER_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sp, "managed_settings_path", lambda: tmp_path / "managed.json")
    monkeypatch.setattr(sp, "_claude_home", lambda: tmp_path / ".claude")
    procs = {"table": {}, "owners": {"claude": {}, "codex": {}}, "markers": {}, "orphans": []}
    monkeypatch.setattr(sp.SessionsPlugin, "_processes", staticmethod(lambda: procs))

    def make(sessions):
        p = sp.SessionsPlugin()
        p.bind_worker(SimpleNamespace(registry=Reg(sessions), worker_id="w1", name="host-a"))
        p.procs = procs
        return p
    return make


@pytest.mark.asyncio
async def test_a_log_written_in_the_last_two_minutes_is_not_closed(catalog):
    now = time.time()
    p = catalog([dict(session_id=C1, title="Busy", cwd="/srv/app", last_modified=now - 30, message_count=9,
                      active=False, activity="working", messageable=False),
                 dict(session_id=C2, title="Quiet", cwd="/srv/app", last_modified=now - 600, message_count=4,
                      active=False, activity="ready", messageable=False)])
    by = {i["native_id"]: i for i in (await p.list())["items"]}
    busy, quiet = by[C1], by[C2]
    assert busy["state"] == "live" and busy["possibly_live"] == "recent_write"
    assert busy["resumable"] is False and busy["input"] == "none" and "pid" not in busy
    assert quiet["state"] == "closed" and quiet["resumable"] is True and "possibly_live" not in quiet
    assert [i["native_id"] for i in (await p.list(live_only=True))["items"]] == [C1]


@pytest.mark.asyncio
async def test_resume_refuses_a_session_written_in_the_last_two_minutes(claude_root, tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_WORK_TERM_DIR", str(tmp_path / "terms"))
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\necho "STARTED $*"\nsleep 5\n')
    fake.chmod(0o755)
    monkeypatch.setattr(terminals, "_binary", lambda h: str(fake))
    monkeypatch.setattr(aa, "process_table", lambda *a, **k: {})
    path = write(claude_root / "-srv-app" / f"{C1}.jsonl", [rec_user("hi")])
    plugin = terminals.TerminalsPlugin()
    try:
        with pytest.raises(ValueError, match="may still be running.*last 2 minutes.*force=true"):
            await plugin.open(harness="claude", cwd=str(tmp_path), resume=C1)
        assert not plugin.terms
        forced = await plugin.open(harness="claude", cwd=str(tmp_path), resume=C1, force=True)
        assert forced["resume"] == C1
        await plugin.close(forced["id"])
        old = time.time() - 300
        os.utime(path, (old, old))
        quiet = await plugin.open(harness="claude", cwd=str(tmp_path), resume=C1)
        assert quiet["resume"] == C1
        await plugin.close(quiet["id"])
    finally:
        await plugin.stop()


# -- unreadable PID markers --------------------------------------------------------------

def proc(root, pid, name, argv, cwd, state="S", start="500"):
    p = root / str(pid)
    (p / "fd").mkdir(parents=True)
    (p / "comm").write_text(name)
    (p / "cmdline").write_bytes("\0".join(argv).encode())
    (p / "stat").write_text(f"{pid} ({name}) " + " ".join([state, "1"] + ["0"] * 17 + [start]))
    (p / "cwd").symlink_to(cwd)


def test_empty_marker_of_a_live_claude_is_evidence(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "stat").write_text("cpu 1\nbtime 1000\n")
    project = tmp_path / "srv" / "app"
    project.mkdir(parents=True)
    proc(root, 400, "claude", ["claude", "-c"], project)                 # marker cut to 0 bytes
    proc(root, 401, "claude", ["claude"], project, start="900")          # a readable marker
    proc(root, 402, "bash", ["bash"], project)                           # not claude
    home = tmp_path / "claude"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "400.json").write_text("")
    (home / "sessions" / "401.json").write_text(json.dumps({"pid": 401, "sessionId": C2, "procStart": "900"}))
    (home / "sessions" / "402.json").write_text("{not json")
    (home / "sessions" / "403.json").write_text("")                      # no such process
    table = aa.process_table(root)
    # Nothing crashes, and the readable marker still counts.
    assert list(aa.claude_markers(home, table, root)) == [C2]
    assert aa.claude_unreadable_markers(home, table, root) == [{"pid": 400, "cwd": str(project)}]
    # The newest log in that folder no other evidence explains is the one it holds.
    projects = tmp_path / "projects"
    folder = projects / sp._project_dir(projects, str(project)).name
    write(folder / f"{C1}.jsonl", [rec_user("a")], age=900)
    write(folder / f"{C2}.jsonl", [rec_user("b")], age=10)               # held by pid 401
    write(folder / f"{C3}.jsonl", [rec_user("c")], age=5000)
    procs = {"markers": {C2: {}}, "owners": {"claude": {}},
             "orphans": aa.claude_unreadable_markers(home, table, root)}
    assert sp.orphan_sessions(procs, projects) == {C1}
    assert sp.orphan_sessions(dict(procs, orphans=[]), projects) == set()


@pytest.mark.asyncio
async def test_unreadable_marker_keeps_its_session_live_and_unresumable(catalog, claude_root, monkeypatch):
    cwd = "/srv/app"
    write(claude_root / sp._project_dir(claude_root, cwd).name / f"{C1}.jsonl", [rec_user("long job", cwd)], age=900)
    write(claude_root / sp._project_dir(claude_root, cwd).name / f"{C2}.jsonl", [rec_user("older", cwd)], age=5000)
    p = catalog([dict(session_id=C1, title="long job", cwd=cwd, last_modified=time.time() - 900,
                      message_count=1, active=False, activity="ready", messageable=False),
                 dict(session_id=C2, title="older", cwd=cwd, last_modified=time.time() - 5000,
                      message_count=1, active=False, activity="ready", messageable=False)])
    p.procs["orphans"] = [{"pid": 400, "cwd": cwd}, {"pid": 401, "cwd": None}]
    by = {i["native_id"]: i for i in (await p.list())["items"]}
    assert by[C1]["state"] == "idle" and by[C1]["possibly_live"] == "unreadable_marker"
    assert by[C1]["resumable"] is False
    assert by[C2]["state"] == "closed" and by[C2]["resumable"] is True
    assert await p._quick_counts() == {"live": 2, "idle": 0}
    # Resuming it is refused (and the other one is not).
    why = sp.resume_guard("claude", C1, p.procs)
    assert why and "unreadable session marker" in why
    assert sp.resume_guard("claude", C2, p.procs) is None
    sent = await p.send("claude", C1, "hello")
    assert not sent["ok"] and "may still be running" in sent["error"]


# -- the hub: cached list and follow(tail=) ------------------------------------------------

class SlowBand:
    def __init__(self):
        now = time.time()
        common = dict(band="test", last_seen=now)
        self.workers = {
            "w1": dict(worker_id="w1", name="fast", caps=["sessions.list", "sessions.follow"], **common),
            "w2": dict(worker_id="w2", name="slow", caps=["sessions.list"], **common),
            "w3": dict(worker_id="w3", name="phone", caps=["battery.status"], **common),
        }
        self.calls = []
        self.fail = False
        self.old_follow = False

    async def call(self, cap, args, target, timeout, identity=None):
        self.calls.append((target, cap, dict(args)))
        if cap == "sessions.follow":
            if self.old_follow and "tail" in args:
                return {"ok": False, "error": "bad args: SessionsPlugin.follow() got an unexpected keyword argument 'tail'"}
            return {"ok": True, "from": target, "result": {"ok": True, "version": "v", "replace_from": 7,
                    "tail_from": 7 if "tail" in args else None, "messages": [], "total_messages": 47}}
        if target == "w2" and self.fail:
            raise asyncio.TimeoutError()
        rec = dict(agent="claude", native_id=C1 if target == "w1" else C2, title=target, cwd="/srv",
                   state="closed", origin="external", updated=1, view={"terminal": None, "mirror": False,
                   "transcript": True}, input="none", inbox_policy="unknown", links={}, resumable=True)
        return {"ok": True, "from": target, "result": {"ok": True, "items": [rec], "total": 1}}


@pytest.mark.asyncio
async def test_hub_serves_its_cached_list_at_once(portal):  # noqa: F811
    p = portal
    p.server._band = band = SlowBand()
    async with TestClient(TestServer(p.app)) as client:
        get = lambda q: client.get("/account/work/sessions" + q, headers=p.headers)
        # Nothing read yet: every host listed, none asked, nothing fetched.
        body = await (await get("?cached=1")).json()
        assert body["cached"] is True and band.calls == [] and body["sessions"] == []
        assert {w["worker_id"]: (w["fetched"], w["stale"], w["cached"]) for w in body["workers"]} == {
            "w1": (None, False, True), "w2": (None, False, True)}
        # Each host asked on its own fills the cache.
        for wid in ("w1", "w2"):
            one = await (await get("?worker=" + wid)).json()
            assert [w["worker_id"] for w in one["workers"]] == [wid] and one["cached"] is False
        band.calls.clear()
        body = await (await get("?cached=1")).json()
        assert band.calls == [] and {s["key"] for s in body["sessions"]} == {f"w1/claude/{C1}", f"w2/claude/{C2}"}
        assert all(w["fetched"] and not w["stale"] for w in body["workers"])
        # A host whose last read failed is stale in the cached list too, with its error.
        band.fail = True
        failed = await (await get("?worker=w2")).json()
        assert failed["workers"][0]["stale"] and failed["errors"][0]["worker_id"] == "w2"
        body = await (await get("?cached=1")).json()
        assert {w["worker_id"]: w["stale"] for w in body["workers"]} == {"w1": False, "w2": True}
        assert [e["worker_id"] for e in body["errors"]] == ["w2"]
        assert f"w2/claude/{C2}" in {s["key"] for s in body["sessions"]}
        # Filters apply to the cached copy.
        body = await (await get("?cached=1&live_only=1")).json()
        assert body["sessions"] == []
        band.fail = False
        await get("?worker=w2")
        body = await (await get("?cached=1")).json()
        assert not any(w["stale"] for w in body["workers"]) and body["errors"] == []


@pytest.mark.asyncio
async def test_hub_follow_asks_for_the_tail_and_falls_back(portal):  # noqa: F811
    p = portal
    p.server._band = band = SlowBand()
    body = {"csrf": p.csrf, "worker": "w1", "agent": "claude", "native_id": C1, "offset": 0, "version": ""}
    async with TestClient(TestServer(p.app)) as client:
        out = await (await client.post("/account/work/session/follow", json=dict(body, tail=40),
                                        headers=p.headers)).json()
        assert out["tail_from"] == 7 and band.calls[-1][2]["tail"] == 40
        out = await (await client.post("/account/work/session/follow", json=dict(body, tail=999),
                                        headers=p.headers)).json()
        assert band.calls[-1][2]["tail"] == 200
        await client.post("/account/work/session/follow", json=body, headers=p.headers)
        assert "tail" not in band.calls[-1][2]
        # A worker from before tail=: asked again without it.
        band.old_follow = True
        band.calls.clear()
        out = await (await client.post("/account/work/session/follow", json=dict(body, tail=40),
                                        headers=p.headers)).json()
        assert out["ok"] and out.get("tail_from") is None and out["total_messages"] == 47
        assert [("tail" in c[2]) for c in band.calls] == [True, False]
