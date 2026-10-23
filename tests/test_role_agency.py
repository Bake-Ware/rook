"""Roles with agency inside their own space (docs/design/huddles.md 4.5, 5.5):
a workspace they use without approval, an interim action tier the hub keeps
per cap, and coding sessions a role starts on a branch that report back to
it and the huddle by themselves. Model turns run on the fake agent host of
test_huddle_chair, which calls the hub's real per-turn tool server."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from rook.hub.plugins.roles import sessions as rs, tiers, workspace as ws
from rook.worker import rolehome
from rook.worker.plugins import agenthost
from tests.test_huddle_chair import FakeBand, PERSON, _call, _settle, _texts, hub  # noqa: F401

LOCATION = ("app (the product): worker devbox, main checkout /srv/app; branch off origin/main; "
            "new worktrees go under /srv/wt.")
ROOTS = "/data/agenthost/roles"


class Host:
    """A fake agent host and code host: a file system for the role homes,
    and terminals whose output the test writes."""

    def __init__(self, band: FakeBand) -> None:
        self.band = band
        self.files: dict[str, str] = {}
        self.links: set[str] = set()
        self.terms: dict[str, dict] = {}
        self.opened: list[dict] = []
        band.workers["w-devbox"] = {"name": "devbox", "last_seen": time.time(),
                                    "caps": ["work.stream.open", "work.stream.read",
                                             "work.stream.close", "info.ping"],
                                    "tiers": {"work.stream.open": "exec", "info.ping": "read",
                                              "work.stream.read": "read",
                                              "work.stream.close": "exec"}}
        self.inner = band.call
        band.call = self.call

    def _kind(self, path: str) -> str | None:
        if path in self.links:
            return "symlink"
        if path in self.files:
            return "file"
        if any(k.startswith(path.rstrip("/") + "/") for k in self.files):
            return "dir"
        return None

    def output(self, tid: str, text: str, done: bool = False, code: int = 0) -> None:
        t = self.terms[tid]
        t["buf"] += text.encode()
        if done:
            t["running"], t["exit"] = False, code

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None, principal=None):
        args = args or {}
        ok = lambda r: {"ok": True, "result": {"ok": True, **r}}  # noqa: E731
        self.band.calls.append(type("C", (), {"cap": cap, "args": args, "target": target,
                                              "identity": identity, "principal": principal}))
        if target is not None and target == getattr(self.band.hub, "worker_id", None):
            return await self.band.hub.dispatch(cap, args, identity)   # a hub cap (a card)
        if cap == "agenthost.roles":
            return ok({"base": ROOTS, "roles": []})
        if cap == "file.exists":
            k = self._kind(args["path"])
            return ok({"exists": k is not None, **({"type": k} if k else {})})
        if cap == "file.write":
            p = args["path"]
            self.files[p] = (self.files.get(p, "") if args.get("append") else "") + args["content"]
            return ok({"written_bytes": len(args["content"]), "path": p})
        if cap == "file.read":
            c = self.files[args["path"]]
            return ok({"content": c, "size_bytes": len(c)})
        if cap == "file.list":
            root = args["path"].rstrip("/") + "/"
            return ok({"entries": [{"name": k.rsplit("/", 1)[1], "path": k[len(root):],
                                    "type": "file", "size": len(v), "modified": 1.0}
                                   for k, v in self.files.items() if k.startswith(root)]})
        if cap == "file.search":
            root = args["path"].rstrip("/") + "/"
            return ok({"matches": [{"path": k, "line_number": i + 1, "content": line}
                                   for k, v in self.files.items() if k.startswith(root)
                                   for i, line in enumerate(v.splitlines())
                                   if args["pattern"] in line]})
        if cap == "shell.exec" and args.get("argv", [None])[0] == "rm":
            self.files.pop(args["argv"][-1], None)
            return ok({"exit_code": 0})
        if cap == "work.stream.open":
            tid = f"t{len(self.terms) + 1}"
            self.terms[tid] = {"buf": b"", "running": True, "exit": None, "args": args}
            self.opened.append({"args": args, "identity": identity, "target": target})
            return ok({"id": tid})
        if cap == "work.stream.read":
            t = self.terms.get(args["id"])
            if t is None:
                return {"ok": False, "error": "no such terminal"}
            cur = int(args.get("cursor") or 0)
            if cur >= len(t["buf"]) and t["running"]:
                await asyncio.sleep(0.01)
            data = t["buf"][cur:]
            return ok({"enc": "t", "data": data.decode(), "next": cur + len(data),
                       "running": t["running"], "exit_code": t["exit"],
                       "eof": not t["running"] and cur + len(data) >= len(t["buf"])})
        if cap == "work.stream.close":
            self.terms[args["id"]]["running"] = False
            return ok({})
        return await self.inner(cap, args, target, timeout, identity, principal)


@pytest.fixture
def host(hub):  # noqa: F811
    return Host(hub.band)


async def _role(hub, rid="architect", autonomy="routine", **kw):  # noqa: F811
    await _call(hub, "role.set", {"role": {"id": rid, "name": rid.title(),
                                           "charter": "Owns the design.", "autonomy": autonomy,
                                           "tools": {"allow": ["info.*", "task.read", "*.merge",
                                                               "hub.deploy", "vault.get",
                                                               "file.write", "made.up"]},
                                           **kw}})


async def _until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


# -- the tier ---------------------------------------------------------------------------

def test_the_tier_is_the_hubs_per_cap():
    routine = {"id": "a", "autonomy": "routine"}
    acts = {"id": "a", "autonomy": "act"}
    assert tiers.cap_tier(routine, "info.ping", "read")[0] == tiers.FREE
    for cap, risk in (("git.merge", "exec"), ("hub.deploy", "admin"), ("file.write", "exec"),
                      ("job.run", "write"), ("settings.set", "admin"), ("made.up", "exec")):
        assert tiers.cap_tier(acts, cap, risk)[0] == tiers.APPROVAL, cap
    # Secrets are never lowerable: a read of the vault is still a card.
    for cap in ("vault.get", "settings.worker_secret", "secret.read", "x.token_show"):
        assert tiers.cap_tier(acts, cap, "read")[0] == tiers.APPROVAL, cap
    # A protected (family) device: even a read is a card.
    assert tiers.cap_tier(acts, "info.ping", "read", "Kids Tablet", ["kids tablet"])[0] == \
        tiers.APPROVAL
    assert tiers.tool_tier(routine, "start_session")[0] == tiers.ROUTINE
    assert tiers.tool_tier({"id": "p", "autonomy": "propose"}, "start_session")[0] == \
        tiers.APPROVAL
    assert tiers.tool_tier({"id": "p"}, "session_status")[0] == tiers.FREE


# -- the workspace ----------------------------------------------------------------------

def test_workspace_paths_stay_inside():
    assert ws.clean("notes/plan.md") == "notes/plan.md"
    assert ws.clean("./workspace/a.md") == "a.md"
    assert ws.clean("", allow_root=True) == ""
    for bad in ("../x", "/etc/passwd", "~/x", "a/../../b", "a//b", "", "a/./b", "a\x00b"):
        with pytest.raises(ws.WorkspaceError):
            ws.clean(bad)


@pytest.mark.asyncio
async def test_a_role_writes_in_its_workspace_with_no_card(hub, host):  # noqa: F811
    await _role(hub)
    h = await _call(hub, "huddle.start", {"title": "Design", "roles": ["architect"]})
    hub.band.script = {"huddle-architect": [{"text": "Drafted.", "tools": [
        ("workspace_write", {"path": "drafts/h6.md", "content": "# H6\nfront door\n"}),
        ("workspace_edit", {"path": "drafts/h6.md", "old": "front door", "new": "the front door"}),
        ("workspace_write", {"path": "notes.md", "content": "todo\n"}),
        ("workspace_list", {}),
        ("workspace_search", {"pattern": "front"}),
        ("workspace_read", {"path": "drafts/h6.md"}),
        ("workspace_delete", {"path": "notes.md"}),
        ("workspace_write", {"path": "../escape.md", "content": "x"}),
        ("workspace_write", {"path": "/etc/x", "content": "x"}),
    ]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@architect draft H6"})
    await _settle(hub)
    turn = next(t for t in hub.band.turns if t["kind"] == "huddle-architect")
    wr, ed, _, listed, found, read, deleted, esc, absolute = turn["seen"]
    assert wr["ok"] and ed["ok"] and deleted["ok"]
    assert [f["path"] for f in listed["files"]] == ["drafts/h6.md", "notes.md"]
    assert found["matches"][0]["path"] == "drafts/h6.md"
    assert read["content"] == "# H6\nthe front door\n"
    assert not esc["ok"] and not absolute["ok"]
    home = f"{ROOTS}/architect/workspace"
    assert set(host.files) == {f"{home}/drafts/h6.md"}               # nothing outside it
    view = await _call(hub, "huddle.read", {"room": h["room"]})
    assert view["cards"] == []                                        # no card for any of it
    # People see it read-only on the role's page.
    page = await _call(hub, "role.workspace", {"id": "architect"})
    assert [f["path"] for f in page["files"]] == ["drafts/h6.md"]
    one = await _call(hub, "role.workspace", {"id": "architect", "path": "drafts/h6.md"})
    assert one["content"].startswith("# H6")
    # A link someone planted is not followed.
    host.links.add(f"{home}/drafts/h6.md")
    res = await hub.node.dispatch("role.workspace", {"id": "architect", "path": "drafts/h6.md"},
                                  PERSON)
    assert not res["ok"] and "link" in res["error"]


# -- coding sessions on a branch -----------------------------------------------------------

def _stream(*events) -> str:
    return "".join(json.dumps(e) + "\r\n" for e in events)


@pytest.mark.asyncio
async def test_a_role_starts_a_branch_session_with_no_card_and_it_reports_back(hub, host):  # noqa: F811
    hub.node.settings.set("role.code_locations", [LOCATION], actor="human:op")
    await _role(hub)
    await _role(hub, "skeptic", autonomy="propose")
    h = await _call(hub, "huddle.start", {"title": "Design", "roles": ["architect", "skeptic"]})
    room = h["room"]
    hub.band.script = {"huddle-architect": [
        {"text": "Started the session.", "tools": [
            ("workspace_write", {"path": "h6.md", "content": "draft"}),
            ("start_session", {"location": "app", "branch": "main",
                               "instructions": "x"}),
            ("start_session", {"location": "app", "branch": "design-h6", "title": "H6 doc",
                               "instructions": "Put workspace file h6.md into docs/design."})]},
        {"text": "The PR is up; reviewers, please look."}],
        "huddle-chair": [{"tools": [("call_on", {"calls": [
            {"role": "skeptic", "ask": "Attack the PR."}]})]}]}
    await _call(hub, "huddle.post", {"huddle": room, "text": "@architect write H6 and PR it"})
    await _settle(hub)
    turn = next(t for t in hub.band.turns if t["kind"] == "huddle-architect")
    _, master, started = turn["seen"]
    assert not master["ok"] and "never work on main" in master["error"]
    assert started["ok"] and started["branch"] == "design-h6" and started["state"] == "running"
    (opened,) = host.opened
    a = opened["args"]
    assert opened["target"] == "w-devbox" and opened["identity"] == "agent:home/role:architect"
    assert a["harness"] == "shell" and a["argv"][:2] == ["/bin/sh", "-c"]
    assert a["argv"][4:8] == ["/srv/app", "/srv/wt/design-h6", "design-h6", "origin/main"]
    settings = json.loads(a["env"]["ROOK_SESSION_SETTINGS"])["permissions"]
    assert "Bash(git push -u origin design-h6)" in settings["allow"]
    assert "Bash(gh pr create:*)" in settings["allow"]
    assert "Bash(gh pr merge:*)" in settings["deny"]
    # The forms sessions reach for first: cd into the worktree, git -C, gh pr edit.
    assert "Bash(cd /srv/wt/design-h6:*)" in settings["allow"]
    assert "Bash(git -C /srv/wt/design-h6 commit:*)" in settings["allow"]
    assert "Bash(gh pr edit:*)" in settings["allow"]
    assert not any("push" in x and "main" in x for x in settings["allow"])
    assert f"Read(/{ROOTS}/architect/workspace/**)" in settings["allow"]
    prompt = a["env"]["ROOK_SESSION_PROMPT"]
    assert "Never merge" in prompt and "ROOK-STATUS: DONE" in prompt and "h6.md" in prompt
    view = await _call(hub, "huddle.read", {"room": room})
    assert view["cards"] == []                                        # no approval for any of it

    # It works, then says DONE with a PR and ends: the role and the huddle hear it.
    sid = started["id"]
    host.output("t1", "ROOK-STATUS: WORKING making the worktree\n" + _stream(
        {"type": "system", "subtype": "init", "session_id": "abc"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ROOK-STATUS: WORKING writing docs/design/huddles.md"}]}}))
    await _until(lambda: any("working: writing docs" in t for _, t in _texts(hub, room)))
    host.output("t1", _stream(
        {"type": "assistant", "message": {"content": [{"type": "text", "text":
            "Opened it.\nROOK-STATUS: DONE H6 section added, "
            "https://github.com/acme/app/pull/7"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "done",
         "total_cost_usd": 0.5, "permission_denials": []}) + "ROOK-EXIT: 0\n", done=True)
    await _until(lambda: (hub.roles.sessions.get(sid) or {}).get("state") == "done")
    await _until(lambda: any(s.startswith("session:") and "reports DONE" in t
                             for s, t in _texts(hub, room)))
    await _settle(hub)
    assert not [r for r in hub.node.journal.rows if str(r.get("cap", "")).startswith("job.")]
    rec = hub.roles.sessions.get(sid)
    assert rec["pr_url"] == "https://github.com/acme/app/pull/7" and rec["native_id"] == "abc"
    texts = _texts(hub, room)
    report = next(t for s, t in texts if s.startswith("session:") and "reports DONE" in t)
    assert "@architect" in report and "pull/7" in report
    # The chair woke the architect and called on the reviewer the thread named.
    kinds = [t["kind"] for t in hub.band.turns]
    assert kinds.count("huddle-architect") == 2 and "huddle-skeptic" in kinds
    woke = [t for t in hub.band.turns if t["kind"] == "huddle-architect"][1]
    assert "reports DONE" in woke["args"]["prompt"]


@pytest.mark.asyncio
async def test_a_session_that_ends_without_a_status_is_reported_as_unexpected(hub, host):  # noqa: F811
    hub.node.settings.set("role.code_locations", [LOCATION], actor="human:op")
    await _role(hub)
    out = await hub.roles.start_session(hub.roles.role("architect"), branch="fix-1",
                                        instructions="Fix it.")
    host.output("t1", _stream(
        {"type": "result", "subtype": "success", "is_error": False, "result": "I stopped.",
         "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "gh pr merge 7"}}]})
        + "ROOK-EXIT: 0\n", done=True)
    await _until(lambda: (hub.roles.sessions.get(out["id"]) or {}).get("state") == "failed")
    rec = hub.roles.sessions.get(out["id"])
    assert "never said DONE" in rec["status"] and "gh pr merge 7" in rec["status"]
    # No huddle: the role itself is woken with the report.
    await _until(lambda: any(t["kind"] == "session-architect" for t in hub.band.turns))
    woke = next(t for t in hub.band.turns if t["kind"] == "session-architect")
    assert "reports UNEXPECTED" in woke["args"]["prompt"]


@pytest.mark.asyncio
async def test_a_role_that_only_proposes_gets_a_card_for_a_session(hub, host):  # noqa: F811
    hub.node.settings.set("role.code_locations", [LOCATION], actor="human:op")
    await _role(hub, "qa", autonomy="propose")
    role = hub.roles.role("qa")
    names = [t["function"]["name"] for t in hub.roles.tools_for(role)]
    assert "start_session" not in names                       # not offered to it at all
    h = await _call(hub, "huddle.start", {"title": "Q", "roles": ["qa"]})
    made = []

    async def approval(card):
        out = await hub.svc.chair._propose(None, h["room"], "role:qa", card)
        made.append(out)
        return out
    out = await hub.roles.session_tool(role, "start_session",
                                       {"branch": "qa-fix", "instructions": "Fix flaky test."},
                                       room=h["room"], approval=approval)
    assert out["needs_approval"] and out["card"] and not host.opened
    card = hub.svc.store.card(out["card"])
    assert card["kind"] == "session.start" and card["call"]["cap"] == "role.session_start"
    # The person approves: the session starts, as the person, for the role.
    await _call(hub, "huddle.card", {"huddle": h["room"], "card": card["id"], "action": "approve"})
    assert len(host.opened) == 1
    assert hub.roles.sessions.list(role="qa")[0]["branch"] == "qa-fix"


@pytest.mark.asyncio
async def test_merges_deploys_secrets_writes_outside_and_unknown_caps_are_cards(hub, host):  # noqa: F811
    await _role(hub, autonomy="act")
    hub.band.workers["w-box"]["caps"] += ["git.merge", "hub.deploy", "vault.get", "file.write",
                                          "made.up"]
    hub.band.workers["w-box"]["tiers"].update({"git.merge": "exec", "hub.deploy": "admin",
                                               "vault.get": "read", "file.write": "exec"})
    hub.node.settings.set("role.protected_workers", ["devbox"], actor="human:op")
    h = await _call(hub, "huddle.start", {"title": "Ops", "roles": ["architect"]})
    calls = [("git.merge", {"pr": 7}), ("hub.deploy", {}), ("vault.get", {"name": "x"}),
             ("file.write", {"path": "/etc/motd", "content": "x"}), ("made.up", {})]
    hub.band.script = {"huddle-architect": [{"text": "Asked.", "tools": [
        ("rook_call", {"worker": "box", "cap": c, "args": a, "why": f"need {c}"}) for c, a in calls]
        + [("rook_call", {"worker": "devbox", "cap": "info.ping"}),
           ("rook_call", {"worker": "box", "cap": "info.ping"})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@architect ship it"})
    await _settle(hub)
    seen = next(t for t in hub.band.turns if t["kind"] == "huddle-architect")["seen"]
    *held, family, free = seen
    assert all(not r["ok"] and r["needs_approval"] and r["card"] for r in held)
    assert not family["ok"] and family["needs_approval"]               # protected device
    assert free["ok"]                                                  # a read: just done
    cards = (await _call(hub, "huddle.read", {"room": h["room"]}))["cards"]
    assert [c["call"]["cap"] for c in cards] == [c for c, _ in calls] + ["info.ping"]
    assert all(c["state"] == "proposed" and c["kind"] == "call" for c in cards)
    assert not [c for c in hub.band.calls if c.cap in ("git.merge", "hub.deploy", "vault.get",
                                                       "file.write", "made.up")]


# -- the pieces ---------------------------------------------------------------------------

def test_operator_test_commands_join_the_session_allowlist():
    from rook.hub.plugins.roles import sessions as rs
    ok = rs.allowed_test_commands([
        "python3 -m pytest", "  npm   test ", "python3 -m pytest",       # tidied, deduplicated
        "rook-pytest",                                                     # built in already
        "make test; rm -rf ~", "pytest && curl x", "echo $HOME",          # shell control
        "git push origin main", "gh pr merge 1", "sudo make test", "/bin/sh -c x", "bash t.sh",
        "x" * 81, "", None])
    assert ok == ("python3 -m pytest", "npm test")
    perms = rs.session_settings("/srv/wt/x", "fix/x", "", ok)["permissions"]
    assert "Bash(python3 -m pytest:*)" in perms["allow"] and "Bash(npm test:*)" in perms["allow"]
    assert "Bash(rook-pytest:*)" in perms["allow"]
    assert "Bash(python3 -m pytest:*)" not in rs.session_settings("/srv/wt/x", "fix/x")[
        "permissions"]["allow"]                                            # off by default
    prompt = rs.session_prompt({"id": "lead"}, {"name": "app", "base": "origin/main"}, "fix/x",
                               "/srv/wt/x", "do it", tests=ok)
    assert "(rook-pytest, python3 -m pytest, npm test)" in prompt


def test_code_locations_parse_into_where_a_session_runs():
    (loc,) = rs.locations([LOCATION, "notes: no worker here"])
    assert loc == {"name": "app", "worker": "devbox", "repo": "/srv/app", "worktrees": "/srv/wt",
                   "base": "origin/main", "line": LOCATION}
    with pytest.raises(rs.SessionError):
        rs.pick_location([loc], "other")
    assert rs.pick_location([loc], "")["repo"] == "/srv/app"
    for bad in ("master", "main", "HEAD", "-x", "a..b", "x.lock", "a b"):
        with pytest.raises(rs.SessionError):
            rs.check_branch(bad, "origin/main")
    assert rs.check_branch("design-h6") == "design-h6"


def test_follow_reads_claude_codes_stream():
    f = rs.Follow()
    ev = f.feed(b"ROOK-STATUS: BLOCKED could not make a worktree\r\nROOK-EXIT: 3\r\n")
    assert ev == [("BLOCKED", "could not make a worktree")]
    assert f.report() == ("BLOCKED", "could not make a worktree. Unexpected: it exited with code 3")
    f = rs.Follow()
    f.feed(_stream({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "ROOK-STATUS: DONE ok https://github.com/a/b/pull/1"}]}},
        {"type": "result", "is_error": False, "result": "x"}).encode()[:-5])
    assert f.result is None                                  # a line is only read once whole
    f.feed(b"}\r\n")
    assert f.report() == ("DONE", "ok https://github.com/a/b/pull/1")
    assert f.pr_urls == ["https://github.com/a/b/pull/1"]


def test_a_native_workspace_makes_workspace_the_only_writable_root(tmp_path):
    home = rolehome.ensure(tmp_path, "architect")
    assert (home / "workspace").is_dir()
    files = rolehome.files(home, role="architect", mcp_url="https://hub/x", tools=["rook_call"],
                           builtin=[], workspace=True)
    perms = json.loads(files[".claude/settings.json"])["permissions"]
    root = str(home.resolve())
    assert f"Write(/{root}/workspace/**)" in perms["allow"]
    assert f"Edit(/{root}/workspace/**)" in perms["allow"]
    assert not any(a.startswith(("Write(", "Edit(")) and "/workspace/" not in a
                   for a in perms["allow"])
    assert "Write" not in perms["deny"] and "Bash" in perms["deny"]
    assert perms["defaultMode"] == "dontAsk"
    assert json.loads(files["role.json"])["workspace"] is True
    assert rolehome.claude_tools([], True) == ["Read", "Write", "Edit", "Glob", "Grep"]
    assert rolehome.claude_tools(["read"], False) == ["Read"]
    off = json.loads(rolehome.files(home, role="architect", mcp_url="https://hub/x",
                                    tools=[], builtin=[])[".claude/settings.json"])["permissions"]
    assert "Write" in off["deny"] and not any("Write(" in a for a in off["allow"])
    argv, _ = agenthost.ClaudeHarness().prepare_role(tmp_path, home, model="", system="s",
                                                     effort=None, builtin=[], workspace=True)
    assert argv[argv.index("--tools") + 1] == "Read,Write,Edit,Glob,Grep"
    assert agenthost.AgentHost().heartbeat()["workspace"] == "native"


@pytest.mark.asyncio
async def test_a_report_the_role_already_answered_after_is_skipped(hub, host):  # noqa: F811
    """A report round queued behind another round, where the role already
    spoke after the report: it saw it in the thread; no second turn."""
    await _role(hub)
    h = await _call(hub, "huddle.start", {"title": "D", "roles": ["architect"]})
    room = h["room"]
    hb = hub.svc.store.get(room)
    lock = hub.svc.chair._locks.setdefault(room, asyncio.Lock())
    # 1. The role's turn was already running when the report came: it did
    # not see it, so the report round still wakes it.
    await lock.acquire()                                   # another round is running
    hub.svc.chair.on_report(hb, room, "architect", "session:x", "@architect DONE: PR 7", "DONE")
    hub.svc._send(room, "role:architect", "Waiting for my session's report.")
    lock.release()
    await _settle(hub)
    woke = [t for t in hub.band.turns if t["kind"] == "huddle-architect"]
    assert len(woke) == 1 and "DONE: PR 7" in woke[0]["args"]["prompt"]
    # 2. A report that a later turn of the role was shown (a follow-up pass
    # of the round that was running when it came): skipped.
    await lock.acquire()
    rid = hub.svc.chair.on_report(hb, room, "architect", "session:x", "@architect DONE: PR 8",
                                  "DONE")
    seq = hub.svc.store.round(rid)["trigger_seq"]
    hub.svc.store.round_add("r_running", room, seq - 1, PERSON, {})
    hub.svc.store.round_set("r_running", state="done", replies=[
        {"role": "architect", "ask": "follow-up", "state": "done", "seen": seq}])
    lock.release()
    await _settle(hub)
    assert len([t for t in hub.band.turns if t["kind"] == "huddle-architect"]) == 1
    skipped = [r for r in hub.svc.store.rounds(room) if "skipped" in (r.get("error") or "")]
    assert len(skipped) == 1


@pytest.mark.asyncio
async def test_a_bad_argument_tells_the_role_what_the_cap_takes(hub, host):  # noqa: F811
    await _role(hub, tools={"allow": ["huddle.read"]})
    role = hub.roles.role("architect")
    out = await hub.roles.role_call(role, "rook", "huddle.read", {"action": "read", "seq": 148})
    assert not out["ok"] and "huddle.read takes:" in out["error"]
    assert "since_seq=0" in out["error"] and "limit=30" in out["error"]
