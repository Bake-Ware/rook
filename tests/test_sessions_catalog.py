"""Sessions catalog (workstream A): sessions.* caps, cross-platform activity,
inbox policy, resume through work.stream, and the hub's merged list."""
import asyncio
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_band_management import portal  # noqa: F401 — fixture
from rook.worker import agent_activity as aa
from rook.worker.plugins import sessions as sp
from rook.worker.plugins.claude_history import ClaudeHistoryPlugin
from rook.worker.plugins.codex_history import CodexHistoryPlugin
from rook.worker.plugins.terminals import build_argv
from rook.remote.work_web import legacy_records

C1 = "11111111-1111-4111-8111-111111111111"   # live claude, idle, external
C2 = "22222222-2222-4222-8222-222222222222"   # closed claude
C3 = "33333333-3333-4333-8333-333333333333"   # live claude outside the history window
X1 = "44444444-4444-4444-8444-444444444444"   # codex resumed in a Rook terminal


def proc(root, pid, name, argv, ppid=1, state="S", start="500", logs=()):
    p = root / str(pid)
    (p / "fd").mkdir(parents=True)
    (p / "comm").write_text(name)
    (p / "cmdline").write_bytes("\0".join(argv).encode())
    fields = [state, str(ppid)] + ["0"] * 17 + [start]
    (p / "stat").write_text(f"{pid} ({name}) " + " ".join(fields))
    for i, log in enumerate(logs):
        (p / "fd" / str(10 + i)).symlink_to(log)


# -- activity on every platform ------------------------------------------------------

def test_linux_table_markers_owners_and_terminal_children(tmp_path):
    root = tmp_path / "proc"
    (root).mkdir()
    (root / "stat").write_text("cpu 1\nbtime 1000\n")
    rollout = tmp_path / f"rollout-2026-10-07T10-00-00-{X1}.jsonl"
    rollout.touch()
    proc(root, 100, "node", ["/usr/bin/node", "/usr/bin/codex"], ppid=50)        # npm shim
    proc(root, 101, "codex", ["/opt/codex"], ppid=100, logs=[rollout])          # native child
    proc(root, 200, "claude", ["claude", "--dangerously-skip-permissions"], start="777")
    proc(root, 300, "claude", ["claude"], start="900", state="Z")
    home = tmp_path / "claude"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "200.json").write_text(json.dumps(
        {"pid": 200, "sessionId": C1, "procStart": "777", "status": "idle", "cwd": "/srv"}))
    (home / "sessions" / "300.json").write_text(json.dumps({"pid": 300, "sessionId": C2}))
    table = aa.process_table(root)
    assert table[101]["ppid"] == 100 and table[200]["argv"][1] == "--dangerously-skip-permissions"
    assert table[300]["zombie"] and table[200]["started"] == 1000 + 777 / __import__("os").sysconf("SC_CLK_TCK")
    markers = aa.claude_markers(home, table, root)
    assert list(markers) == [C1] and markers[C1]["status"] == "idle" and markers[C1]["pid"] == 200
    # A marker from an earlier process with the same PID is not trusted.
    (home / "sessions" / "200.json").write_text(json.dumps({"pid": 200, "sessionId": C1, "procStart": "1"}))
    assert aa.claude_markers(home, table, root) == {}
    owners = aa.session_owners("codex", table, proc_root=root)
    assert owners == {101: X1}
    # A terminal that started the npm shim maps to the native child's session.
    assert aa.session_under(100, owners, table) == X1
    assert aa.session_under(50, owners, table) == X1
    assert aa.session_under(999, owners, table) is None
    # Linux behaviour of active_sessions is unchanged.
    assert aa.active_sessions("codex", root)[0] == {str(rollout.resolve())}


def test_markers_off_linux_use_start_time_against_pid_reuse(tmp_path):
    home = tmp_path / "claude"
    (home / "sessions").mkdir(parents=True)
    started = 1_791_000_000
    (home / "sessions" / "42.json").write_text(json.dumps(
        {"pid": 42, "sessionId": C1, "startedAt": started * 1000 + 2500, "status": "busy"}))
    win = {42: dict(name="node.exe", argv=None, ppid=1, zombie=False, started=started, proc_start=None)}
    assert C1 in aa.claude_markers(home, win, tmp_path / "missing")
    reused = {42: dict(win[42], started=started + 3600)}
    assert aa.claude_markers(home, reused, tmp_path / "missing") == {}
    other = {42: dict(win[42], name="explorer.exe")}
    assert aa.claude_markers(home, other, tmp_path / "missing") == {}


def test_ps_table_and_active_sessions_elsewhere(monkeypatch, tmp_path):
    out = ("  10     1 Ss        01:02:03 /usr/local/bin/codex resume " + X1 + "\n"
           "  11     1 Z            00:05 claude\n"
           "  12    10 S    2-00:00:01 /usr/bin/node /usr/local/bin/claude --resume " + C2 + "\n")
    run = lambda *a, **k: SimpleNamespace(stdout=out)
    table = aa._ps_table(run)
    assert table[10]["argv"][-1] == X1 and table[11]["zombie"] and table[12]["ppid"] == 10
    assert abs((time.time() - table[12]["started"]) - (2 * 86400 + 1)) < 5
    assert aa._etime("05:07") == 307
    monkeypatch.setattr(aa, "process_table", lambda *a, **k: table)
    monkeypatch.setattr(aa.sys, "platform", "win32")   # no lsof
    assert aa._active_elsewhere("codex") == (set(), {X1})
    assert aa._active_elsewhere("claude", tmp_path / "no-claude") == (set(), {C2})


def test_lsof_parsing():
    out = "p10\nn/dev/null\nn/srv/u/.codex/sessions/rollout-" + X1 + ".jsonl\np11\nn/tmp/x.jsonl\n"
    found = aa._open_logs_lsof([10, 11], run=lambda *a, **k: SimpleNamespace(stdout=out))
    if found:   # lsof may not be installed where tests run
        assert found[10] == {"/srv/u/.codex/sessions/rollout-" + X1 + ".jsonl"}


# -- inbox policy and the mirror spool -----------------------------------------------

def test_inbox_policy_layers(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    home.mkdir()
    managed = tmp_path / "managed.json"
    monkeypatch.setattr(sp, "managed_settings_path", lambda: managed)
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    write = lambda p, d: p.write_text(json.dumps(d))
    bypass = ["claude", "--dangerously-skip-permissions"]
    # No setting: the default holds while permissions are bypassed.
    assert sp.inbox_policy(str(project), bypass, home=home) == "hold"
    assert sp.inbox_policy(str(project), ["claude", "--permission-mode", "bypassPermissions"], home=home) == "hold"
    assert sp.inbox_policy(str(project), ["claude"], home=home) == "accept"
    assert sp.inbox_policy(str(project), None, home=home) == "unknown"     # Windows: no argv
    write(project / ".claude" / "settings.json", {"permissions": {"defaultMode": "bypassPermissions"}})
    assert sp.inbox_policy(str(project), ["claude"], home=home) == "hold"
    assert sp.inbox_policy(str(project), None, home=home) == "hold"
    write(project / ".claude" / "settings.local.json", {"permissions": {"defaultMode": "acceptEdits"}})
    assert sp.inbox_policy(str(project), ["claude"], home=home) == "accept"
    # An explicit user setting decides; managed settings override it.
    write(home / "settings.json", {"crossSessionInbound": "accept"})
    assert sp.inbox_policy(str(project), bypass, home=home) == "accept"
    write(managed, {"crossSessionInbound": "refuse"})
    assert sp.inbox_policy(str(project), bypass, home=home) == "refuse"
    # The mod's live value (mirror session.start) wins over files.
    assert sp.inbox_policy(str(project), bypass, inbound="hold", home=home) == "hold"
    assert sp.inbox_policy(str(project), bypass, inbound="default", home=home) == "refuse"


def test_mirror_spool_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_WORKER_HOME", str(tmp_path))
    assert sp.mirror_spool_path("claude", C1) == tmp_path / "mirror" / "claude" / f"{C1}.jsonl"
    assert sp.mirror_spool_path("claude", "../etc") is None
    assert sp.mirror_spool_path("claude", "..") is None
    assert sp.mirror_spool_path("bogus", C1) is None
    assert not sp.mirror_spool_exists("claude", C1)
    spool = sp.mirror_spool_path("claude", C1, 1)
    spool.parent.mkdir(parents=True)
    spool.write_text(json.dumps({"v": 1, "seq": 0, "type": "session.start", "inbound": "hold"}) + "\n")
    assert sp.mirror_spool_exists("claude", C1)
    assert sp.mirror_inbound("claude", C1) == "hold"


# -- the worker plugin ----------------------------------------------------------------

class Reg:
    """A worker registry with terminals, history and inbox caps faked."""

    def __init__(self, terminals=(), caps=None):
        self.terminals = [dict(t) for t in terminals]
        self.calls = []
        self.caps = set(caps or ("work.stream.list", "work.stream.write", "work.stream.close",
                                 "work.stream.open", "claude-history.pull", "codex-history.pull",
                                 "claude-history.send", "codex-history.send", "claude-history.follow",
                                 "claude-history.resumed", "proc.close"))
        self.resumed = []

    def has(self, cap):
        return cap in self.caps

    async def call(self, cap, **kw):
        self.calls.append((cap, kw))
        if cap == "work.stream.list":
            return {"ok": True, "terminals": self.terminals}
        if cap == "claude-history.pull":
            rows = [dict(session_id=C1, title="Live work", cwd="/srv/a", last_modified=300,
                         message_count=12, active=True, activity="ready", messageable=True),
                    dict(session_id=C2, title="Old work", cwd="/srv/b", last_modified=100,
                         message_count=3, active=False, activity="ready", messageable=False)]
            return {"ok": True, "total": 2, "sessions": rows[kw.get("offset", 0):][:kw["limit"]]}
        if cap == "codex-history.pull":
            return {"ok": True, "total": 1, "sessions": [dict(session_id=X1, title="Codex run", cwd="/srv/c",
                    last_modified=200, message_count=4, active=True, activity="working", messageable=False)]}
        if cap.endswith("-history.send"):
            return {"ok": True, "delivery": "forwarded", "note": "Message sent to the Claude inbox."}
        if cap.endswith("-history.follow"):
            return {"ok": True, "messages": [], "version": "1:2:3", "replace_from": kw["offset"]}
        if cap.endswith("-history.resumed"):
            return {"ok": True, "sessions": self.resumed}
        if cap in ("work.stream.write", "proc.close"):
            return {"ok": True}
        if cap == "work.stream.close":
            return {"ok": True, "id": kw["id"], "exit_code": 0}
        raise AssertionError(cap)


TERMS = [
    dict(id="t_codex", harness="codex", title="codex: run", cwd="/srv/c", resume=X1, pid=900,
         running=True, started=10, last_output=250, session="ws1"),
    dict(id="t_shell", harness="shell", title="shell in srv", cwd="/srv", resume=None, pid=901,
         running=True, started=20, last_output=150),
    dict(id="t_old", harness="shell", title="old shell", cwd="/srv", resume=None, pid=None,
         running=False, started=5, last_output=6),
]


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("ROOK_WORKER_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sp, "managed_settings_path", lambda: tmp_path / "managed.json")
    monkeypatch.setattr(sp, "_claude_home", lambda: tmp_path / ".claude")
    procs = {"table": {}, "owners": {"claude": {200: C1, 300: C3}, "codex": {901: X1}},
             "markers": {C1: {"pid": 200, "sessionId": C1, "status": "idle", "cwd": "/srv/a",
                              "argv": ["claude", "--dangerously-skip-permissions"]},
                         C3: {"pid": 300, "sessionId": C3, "status": "busy", "cwd": "/srv/d",
                              "argv": ["claude"], "name": "fresh-1", "updatedAt": 400_000}}}
    monkeypatch.setattr(sp.SessionsPlugin, "_processes", staticmethod(lambda: procs))
    monkeypatch.setattr(sp.SessionsPlugin, "_meta", staticmethod(lambda agent, sid: {}))
    import rook.worker.session_messages as sm
    monkeypatch.setattr(sm, "messageable", lambda agent, sid: agent == "claude")
    p = sp.SessionsPlugin()
    p.bind_worker(SimpleNamespace(registry=Reg(TERMS), worker_id="w1", name="host-a"))
    p.procs = procs
    return p


@pytest.mark.asyncio
async def test_list_builds_one_record_per_session(plugin):
    out = await plugin.list(limit=20)
    by = {(i["agent"], i["native_id"]): i for i in out["items"]}
    assert set(by) == {("claude", C1), ("claude", C2), ("claude", C3), ("codex", X1),
                       ("shell", "t_shell"), ("shell", "t_old")}
    live = by[("claude", C1)]
    assert live["key"] == f"w1/claude/{C1}" and live["worker"] == "host-a"
    assert live["state"] == "idle" and live["origin"] == "external"
    assert live["view"] == {"terminal": None, "mirror": False, "transcript": True}
    # Bypassed permissions + default inbound: the inbox holds; no terminal to type into.
    assert live["inbox_policy"] == "hold" and live["input"] == "inbox" and not live["resumable"]
    fresh = by[("claude", C3)]                      # live, outside the history window
    assert fresh["state"] == "live" and fresh["title"] == "fresh-1" and fresh["inbox_policy"] == "accept"
    codex = by[("codex", X1)]
    assert codex["origin"] == "rook" and codex["view"]["terminal"] == "t_codex"
    assert codex["input"] == "pty" and codex["links"] == {"work_session": "ws1"} and codex["state"] == "live"
    shell = by[("shell", "t_shell")]
    assert shell["view"] == {"terminal": "t_shell", "mirror": False, "transcript": False}
    assert shell["input"] == "pty" and shell["state"] == "live" and shell["inbox_policy"] == "unknown"
    old = by[("claude", C2)]
    assert old["state"] == "closed" and old["resumable"] and old["input"] == "none"
    assert by[("shell", "t_old")]["state"] == "closed" and not by[("shell", "t_old")]["resumable"]
    # Live and idle first, then closed, newest first within each.
    states = [i["state"] for i in out["items"]]
    assert states.index("closed") > max(i for i, s in enumerate(states) if s != "closed")
    assert plugin.heartbeat() == {"live": 3, "idle": 1}


@pytest.mark.asyncio
async def test_list_filters_and_pages(plugin, tmp_path):
    live = await plugin.list(live_only=True)
    assert all(i["state"] != "closed" for i in live["items"]) and live["total"] == 4
    found = await plugin.list(query="old work")
    assert [i["native_id"] for i in found["items"]] == [C2]
    page = await plugin.list(limit=2)
    assert len(page["items"]) == 2 and page["next_offset"] == 2
    spool = sp.mirror_spool_path("claude", C1)
    spool.parent.mkdir(parents=True)
    spool.write_text(json.dumps({"type": "session.start", "inbound": "accept"}) + "\n")
    rec = next(i for i in (await plugin.list())["items"] if i["native_id"] == C1)
    assert rec["view"]["mirror"] is True and rec["inbox_policy"] == "accept"


@pytest.mark.asyncio
async def test_send_routes_inbox_held_keys_and_refusals(plugin, tmp_path):
    reg = plugin._worker.registry
    held = await plugin.send("claude", C1, "status?", command_id="cmd-00000001")
    assert held["ok"] and held["delivery"] == "held" and "approval" in held["note"]
    assert reg.calls[-1] == ("claude-history.send", {"session_id": C1, "text": "status?",
                                                     "command_id": "cmd-00000001"})
    turn = await plugin.send("claude", C3, "go on")
    assert turn["delivery"] == "turn" and len(reg.calls[-1][1]["command_id"]) >= 8
    keys = await plugin.send("codex", X1, "run the tests")
    assert keys["delivery"] == "keys" and reg.calls[-1] == ("work.stream.write", {"id": "t_codex", "data": "run the tests\r"})
    multi = await plugin.send("codex", X1, "line one\nline two")
    assert reg.calls[-1][1]["data"] == "\x1b[200~line one\nline two\x1b[201~\r"
    shell = await plugin.send("shell", "t_shell", "ls")
    assert shell["delivery"] == "keys" and reg.calls[-1][1] == {"id": "t_shell", "data": "ls\r"}
    closed = await plugin.send("claude", C2, "hello")
    assert not closed["ok"] and "closed" in closed["error"]
    assert not (await plugin.send("claude", C1, ""))["ok"]
    (tmp_path / "managed.json").write_text(json.dumps({"crossSessionInbound": "refuse"}))
    refused = await plugin.send("claude", C1, "hello")
    assert not refused["ok"] and "refuse" in refused["error"]
    with pytest.raises(ValueError):
        await plugin.send("claude", "../x", "hi")


@pytest.mark.asyncio
async def test_stop_only_ends_what_rook_started(plugin):
    reg = plugin._worker.registry
    out = await plugin.stop_session("codex", X1)
    assert out["stopped"] == "terminal" and reg.calls[-1] == ("work.stream.close", {"id": "t_codex"})
    ext = await plugin.stop_session("claude", C1)
    assert not ext["ok"] and "outside Rook" in ext["error"]
    reg.resumed = [{"session_id": C1, "running": True, "handle": "h9"}]
    legacy = await plugin.stop_session("claude", C1)
    assert legacy["stopped"] == "process" and reg.calls[-1] == ("proc.close", {"handle": "h9"})
    done = await plugin.stop_session("claude", C2)
    assert done["ok"] and done["stopped"] is None


@pytest.mark.asyncio
async def test_follow_wraps_history_follow(plugin):
    out = await plugin.follow("claude", C1, offset=3, version="v")
    assert out["agent"] == "claude" and out["native_id"] == C1 and out["replace_from"] == 3
    with pytest.raises(ValueError):
        await plugin.follow("shell", "t_shell")
    no_codex = await plugin.follow("codex", X1)
    assert not no_codex["ok"]


@pytest.mark.asyncio
async def test_quick_counts_need_no_transcripts(plugin):
    assert await plugin._quick_counts() == {"live": 3, "idle": 1}
    assert not any(c[0].endswith(".pull") for c in plugin._worker.registry.calls)


# -- resume through work.stream ---------------------------------------------------------

@pytest.mark.asyncio
async def test_history_resume_opens_a_rook_terminal(tmp_path, monkeypatch):
    project = tmp_path / "projects" / "-srv"
    project.mkdir(parents=True)
    (project / f"{C2}.jsonl").write_text(json.dumps({"type": "user", "cwd": str(tmp_path),
        "message": {"role": "user", "content": "fix it"}}) + "\n")
    monkeypatch.setattr("rook.worker.plugins.claude_history._claude_bin", lambda: "/usr/bin/claude")
    reg = Reg(caps={"work.stream.open", "work.stream.list", "proc.start"})
    opened = []

    async def call(cap, **kw):
        reg.calls.append((cap, kw))
        if cap == "work.stream.open":
            opened.append(dict(id="t9", harness=kw["harness"], resume=kw["resume"], title=kw["title"],
                               running=True, started=time.time(), cwd=kw["cwd"], pid=7))
            return {"ok": True, **opened[-1]}
        if cap == "work.stream.list":
            return {"ok": True, "terminals": opened}
        raise AssertionError(cap)
    reg.call = call
    p = ClaudeHistoryPlugin()
    p.bind_worker(SimpleNamespace(registry=reg))
    out = await p._resume(C2, path=str(tmp_path / "projects"))
    assert out["ok"] and out["terminal"] == "t9" and out["remote_control"] is True
    cap, kw = reg.calls[0]
    assert cap == "work.stream.open" and kw["harness"] == "claude" and kw["resume"] == C2
    assert kw["cwd"] == str(tmp_path) and kw["remote_control"] == "fix it"
    assert not any(c[0] == "proc.start" for c in reg.calls)
    resumed = await p._resumed_list()
    assert resumed["sessions"][0]["terminal"] == "t9" and resumed["sessions"][0]["running"]

    async def refuse(cap, **kw):
        raise ValueError("that session is already active on this host")
    reg.call = refuse
    again = await p._resume(C2, path=str(tmp_path / "projects"))
    assert not again["ok"] and "already active" in again["error"]


@pytest.mark.asyncio
async def test_codex_resume_opens_a_rook_terminal(tmp_path, monkeypatch):
    path = tmp_path / f"rollout-2026-10-07T10-00-00-{X1}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": X1, "cwd": str(tmp_path)}}) + "\n")
    monkeypatch.setattr("rook.worker.plugins.codex_history.shutil.which", lambda _: "/usr/bin/codex")
    calls = []

    async def call(cap, **kw):
        calls.append((cap, kw))
        if cap == "work.stream.open":
            return {"ok": True, "id": "t7", "cwd": kw["cwd"], "pid": 8}
        return {"ok": True, "sessions": [], "terminals": []}
    p = CodexHistoryPlugin()
    p.bind_worker(SimpleNamespace(registry=SimpleNamespace(
        has=lambda cap: cap in ("work.stream.open", "work.stream.list", "proc.start", "proc.list"), call=call)))
    out = await p._resume(X1, path=str(tmp_path))
    assert out["ok"] and out["terminal"] == "t7" and not out["remote_control"]
    opened = next(kw for cap, kw in calls if cap == "work.stream.open")
    assert opened["harness"] == "codex" and opened["resume"] == X1 and "remote_control" not in opened


def test_remote_control_launch_template():
    argv = build_argv("claude", "/bin/claude", resume=C1, remote_control="fix it")
    assert argv == ["/bin/claude", "--resume", C1, "--remote-control", "fix it"]
    assert "--remote-control" not in build_argv("codex", "/bin/codex", remote_control="x")


# -- hub merged list ---------------------------------------------------------------------

def test_legacy_records_from_work_sessions_shape():
    items = [dict(agent="claude", session_id=C1, title="a", cwd="/a", updated=5, active=True,
                  activity="ready"),
             dict(agent="codex", session_id=X1, title="b", cwd="/b", updated=4, active=False)]
    live = [dict(id="t1", harness="codex", resume=X1, running=True, title="codex"),
            dict(id="t2", harness="shell", running=True, title="sh", session="ws")]
    recs = {(r["agent"], r["native_id"]): r for r in legacy_records(items, live)}
    assert recs[("claude", C1)]["state"] == "idle" and recs[("claude", C1)]["input"] == "none"
    assert recs[("codex", X1)]["state"] == "live" and recs[("codex", X1)]["view"]["terminal"] == "t1"
    assert recs[("codex", X1)]["input"] == "pty" and recs[("codex", X1)]["origin"] == "rook"
    assert recs[("shell", "t2")]["links"] == {"work_session": "ws"}


class CatalogBand:
    def __init__(self):
        now = time.time()
        common = dict(band="test", last_seen=now)
        self.workers = {
            "w1": dict(worker_id="w1", name="new-host", caps=["sessions.list"],
                       hb={"sessions": {"live": 1, "idle": 1}}, **common),
            "w2": dict(worker_id="w2", name="mid-host", caps=["work.sessions"], **common),
            "w3": dict(worker_id="w3", name="old-host", caps=["claude-history.pull"], **common),
            "w4": dict(worker_id="w4", name="flaky-host", caps=["sessions.list"], **common),
            "w5": dict(worker_id="w5", name="phone", caps=["battery.status"], **common),
        }
        self.calls = []
        self.flaky = False

    async def call(self, cap, args, target, timeout, identity=None):
        self.calls.append((target, cap, dict(args)))
        rec = lambda native, state, updated, **kw: dict(
            agent="claude", native_id=native, title=native[:4], cwd="/srv", state=state,
            origin="external", updated=updated, view={"terminal": None, "mirror": False, "transcript": True},
            input="none", inbox_policy="unknown", links={}, resumable=state == "closed", **kw)
        if target == "w1":
            items = [rec(C1, "idle", 50), rec(C2, "closed", 90)]
            if args.get("live_only"):
                items = [i for i in items if i["state"] != "closed"]
            if args.get("query"):
                items = [i for i in items if args["query"] in i["title"]]
            result = dict(ok=True, items=items, total=len(items), harnesses=["shell", "claude"])
        elif target == "w2":
            result = dict(ok=True, live=[dict(id="t1", harness="shell", running=True, title="sh",
                                              last_output=80)],
                          items=[dict(agent="codex", session_id=X1, title="codex job", cwd="/x",
                                      updated=70, active=False)], total=1, harnesses=["shell"])
        elif target == "w3":
            if cap != "claude-history.pull":
                raise AssertionError(cap)
            result = dict(ok=True, total=1, sessions=[dict(session_id=C3, title="legacy", cwd="/l",
                          last_modified=60, active=True, activity="working", messageable=True)])
        elif target == "w4":
            if self.flaky:
                raise asyncio.TimeoutError()
            result = dict(ok=True, items=[rec(C3, "live", 99)], total=1)
        else:
            raise AssertionError(target)
        return {"ok": True, "from": target, "result": result}


@pytest.mark.asyncio
async def test_hub_merges_every_worker_catalog(portal):  # noqa: F811
    p = portal
    p.server._band = band = CatalogBand()
    work = p.account.work_web
    sid = "c" * 32
    work.store.save(dict(id=sid, owner=p.uid, worker_id="w1", worker_name="new-host", band="test",
                         agent="claude", imported=True, source_id=C1, cwd="/srv", title="x",
                         status="pending", error=""))
    async with TestClient(TestServer(p.app)) as client:
        assert (await client.get("/account/work/sessions")).status == 401
        r = await client.get("/account/work/sessions", headers=p.headers)
        assert r.status == 200 and "no-store" in r.headers["Cache-Control"]
        body = await r.json()
        keys = [s["key"] for s in body["sessions"]]
        assert set(keys) == {f"w1/claude/{C1}", f"w1/claude/{C2}", "w2/shell/t1", f"w2/codex/{X1}",
                             f"w3/claude/{C3}", f"w4/claude/{C3}"}
        # Live and idle first (newest first), then closed.
        assert [s["state"] for s in body["sessions"]][:4] == ["live", "live", "live", "idle"]
        assert body["sessions"][0]["key"] == f"w4/claude/{C3}"
        by = {s["key"]: s for s in body["sessions"]}
        assert by[f"w1/claude/{C1}"]["worker"] == "new-host"
        assert by[f"w1/claude/{C1}"]["links"] == {"work_session": sid}
        assert by[f"w3/claude/{C3}"]["input"] == "inbox" and by[f"w3/claude/{C3}"]["state"] == "live"
        workers = {w["worker_id"]: w for w in body["workers"]}
        assert set(workers) == {"w1", "w2", "w3", "w4"}
        assert workers["w1"]["source"] == "sessions.list" and workers["w1"]["counts"] == {"live": 1, "idle": 1}
        assert workers["w2"]["source"] == "work.sessions" and workers["w3"]["source"] == "history"
        assert body["errors"] == []
        # A worker that stops answering is served from the cache, marked stale.
        band.flaky = True
        body = await (await client.get("/account/work/sessions", headers=p.headers)).json()
        assert {w["worker_id"]: w["stale"] for w in body["workers"]}["w4"] is True
        assert f"w4/claude/{C3}" in [s["key"] for s in body["sessions"]]
        assert body["errors"][0]["worker_id"] == "w4"
        # Filters and limits pass through to workers and apply to older ones here.
        band.calls.clear()
        body = await (await client.get("/account/work/sessions?live_only=1&query=legacy&limit=5",
                                       headers=p.headers)).json()
        assert [s["key"] for s in body["sessions"]] == [f"w3/claude/{C3}"]
        new = next(a for t, c, a in band.calls if t == "w1")
        assert new == {"limit": 5, "query": "legacy", "live_only": True}
        body = await (await client.get("/account/work/sessions?worker=mid-host", headers=p.headers)).json()
        assert [w["worker_id"] for w in body["workers"]] == ["w2"]
        assert (await client.get("/account/work/sessions?limit=x", headers=p.headers)).status == 400
        guest = p.store.create_local("guest", "long enough test password")
        token = p.store.new_session(guest)
        assert (await client.get("/account/work/sessions",
                                 headers={"Cookie": "rook_account=" + token})).status == 403


@pytest.mark.asyncio
async def test_hub_classic_resume_accepts_a_terminal_result(portal):  # noqa: F811
    p = portal
    work = p.account.work_web

    class Band:
        workers = {"host1": dict(worker_id="host1", name="h", band="test", last_seen=time.time(),
                                 caps=["claude-history.resume"])}

        async def call(self, cap, args, target, timeout, identity=None):
            assert cap == "claude-history.resume"
            return {"ok": True, "from": target, "result": {"ok": True, "terminal": "t5", "note": "starting"}}
    p.server._band = Band()
    sid = "d" * 32
    work.store.save(dict(id=sid, owner=p.uid, worker_id="host1", worker_name="h", band="test",
                         agent="claude", imported=True, source_id=C2, cwd="/srv", title="x",
                         status="pending", error=""))
    await work.command(sid, {"op": "resume", "id": "resume-terminal-1"})
    s = work.store.get(sid)
    assert s["term_id"] == "t5" and s["term_running"] and s["harness"] == "claude"
    assert not s.get("external_handle") and s["error"] == ""
