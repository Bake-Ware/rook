"""Approval asks in plain words (rook/hub/plain.py): every ask reaching the
person reads Title / For / Who / I want to / Why / Risk, with the technical
detail behind Explain; note cards never wait; rook_call's why= reaches the
hold (Bake, 2026-10-23)."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from rook.hub import approvals as A
from rook.hub import plain
from rook.hub.authz import Authorizer
from rook.hub.policy import PolicyStore
from rook.hub.trust import classify
from tests.test_approvals import AGENT, check, pending_hold  # noqa: F401
from tests.test_huddle_chair import _call, _settle
from tests.test_huddle_chair import hub as chair_hub  # noqa: F401
from tests.test_relay import CLAUDE, cap, card_and_ask, hold, hub  # noqa: F401

TRUST = ('trust map v15, entry "rook-hub" (guarded), set by Bake 2026-10-23: "the Rook hub/live '
         'band = guarded". change -> ask: ask first (until votes exist, a person approves)')


def held(**kw) -> dict:
    """A hold row as approvals.db keeps it."""
    row = {"id": "hce0a0fa3ccac0c7d93f10342", "name": "brave-otter-lamp",
           "principal": "token:agent_47a3f4d0409fb20ef6037c49dda64f5f",
           "principal_label": "Claude web", "cap": "shell.exec", "target_name": "bakenetcanada",
           "tier": "exec", "tags": [], "kind": "trust", "reason": TRUST,
           "rule": "trust:rook-hub@v15", "via": [], "is_hub": False,
           "args": {"argv": ["sudo", "sqlite3", "/opt/rook-hub/state/approvals.db",
                             "SELECT id, cap FROM holds WHERE created > 1792780000"]},
           "context": {}}
    return {**row, **kw}


# -- the shape ------------------------------------------------------------------------

def test_a_read_only_query_on_the_guarded_hub_reads_plainly():
    p = plain.for_hold(held(context={"task": "t_1", "task_title": "Approval cards in plain words",
                                     "project": "Rook", "why": "find which asks were notes"}))
    assert {k: p[k] for k in ("title", "for", "who", "want", "why", "risk")} == {
        "title": "Read approvals database",
        "for": "Approval cards in plain words (Rook)",
        "who": "Claude web",
        "want": "Run a read-only query on the approvals database on bakenetcanada",
        "why": "find which asks were notes",
        "risk": "Low: looks read-only, but rook-hub is guarded, so changes there ask you."}
    # The technical detail only behind Explain: cap, command, trust entry, hold id + name.
    for bit in ("shell.exec on bakenetcanada", "sqlite3 /opt/rook-hub/state/approvals.db",
                'entry "rook-hub" (guarded)', "rule trust:rook-hub@v15",
                "id hce0a0fa3ccac0c7d93f10342 (brave-otter-lamp)"):
        assert bit in p["explain"]
    for field in ("title", "for", "who", "want", "risk"):
        assert "shell.exec" not in p[field] and "trust map v15" not in p[field]
    assert plain.lines(p) == [
        "For: Approval cards in plain words (Rook)", "Who: Claude web",
        "I want to: Run a read-only query on the approvals database on bakenetcanada",
        "Why: find which asks were notes",
        "Risk: Low: looks read-only, but rook-hub is guarded, so changes there ask you."]


def test_without_a_task_or_why_it_says_no_task():
    p = plain.for_hold(held())
    assert p["for"] == "no task" and "why" not in p and p["who"] == "Claude web"
    unnamed = plain.for_hold(held(principal_label="", principal="token:agent_47a3f4d0409fb20e"))
    assert unnamed["who"] == "an agent"


@pytest.mark.parametrize("argv, kind, title", [
    (["sqlite3", "/var/lib/x.db", "UPDATE t SET a=1"], "change", "Change x database"),
    (["sqlite3", "-readonly", "/var/lib/x.db", "SELECT 1"], "read", "Read x database"),
    (["bash", "-lc", "cd /opt/rook && git status && ls -la | head"], "read", "Look at a git checkout"),
    (["sudo", "systemctl", "restart", "rook-hub"], "change", "Restart rook-hub"),
    (["systemctl", "status", "nginx"], "read", "Check a service"),
    (["journalctl", "-u", "rook-hub", "-n", "50"], "read", "Read logs"),
    (["rm", "-rf", "/tmp/build"], "destroy", "Delete files"),
    (["sh", "-c", "echo on > /etc/flag"], "change", "Write flag"),
    (["sh", "-c", "uptime > /dev/null 2>&1"], "read", "Check the machine"),
    (["git", "push", "origin", "master"], "change", "Push code"),
    (["git", "reset", "--hard", "HEAD~1"], "destroy", "Discard code changes"),
    (["bash", "deploy-local.sh", "abc123"], "change", "Deploy"),
    (["reboot"], "destroy", "Reboot"),
    (["find", "/tmp", "-name", "*.log", "-delete"], "destroy", "Find and delete files"),
    (["curl", "-s", "https://example.org/health"], "read", "Fetch a web page"),
    (["timeout", "60", "rook-pytest", "-q"], "change", "Run tests"),
])
def test_shell_commands_are_said_by_what_they_do(argv, kind, title):
    s = plain.shell_summary({"argv": argv})
    assert (s["kind"], s["title"]) == (kind, title)


@pytest.mark.parametrize("argv", [
    # From the review of vote high-inch-gallery: none of these may read as "looks read-only".
    ["sqlite3", "x.db", "SELECT 1", "DELETE FROM t"],
    ["sqlite3", "x.db", "WITH x AS (SELECT 1) DELETE FROM t"],
    ["sqlite3", "x.db", ".output /tmp/o"], ["sqlite3", "x.db", ".shell rm -rf /"], ["sqlite3", "x.db"],
    ["sed", "-Ei", "s/a/b/", "f"], ["sed", "-ni", "p", "f"], ["sed", "--in-place", "s/a/b/", "f"],
    ["sh", "-c", "cat a 1>/etc/x"], ["sh", "-c", "cmd &>/srv/log"], ["sh", "-c", "uptime 2>/etc/x"],
    ["sh", "-c", "echo $(rm -rf ~)"], ["sh", "-c", "uptime & rm -rf ~/x"], ["sh", "-c", "cat a >| b"],
    ["sh", "-c", "echo `id`"], ["sh", "-c", "cat <<EOF > f"], ["sh", "-c", "ls | xargs rm"],
    ["git", "config", "user.name", "x"], ["git", "remote", "set-url", "origin", "u"],
    ["git", "remote", "add", "o", "u"], ["git", "branch", "new"], ["git", "tag", "v1"],
    ["git", "stash"], ["git", "fetch"], ["git", "log", "--output=/etc/x"],
    ["ip", "link", "set", "eth0", "down"], ["hostnamectl", "set-hostname", "x"],
    ["timedatectl", "set-time", "x"], ["loginctl", "terminate-user", "x"],
    ["journalctl", "--vacuum-time=1s"], ["dmesg", "-C"], ["hostname", "newname"],
    ["date", "-s", "2020-01-01"], ["nvidia-smi", "-pl", "100"],
    ["curl", "-XPOST", "u"], ["curl", "--request", "DELETE", "u"], ["curl", "--json", "{}", "u"],
    ["curl", "-d@f", "u"], ["curl", "-sSo", "f", "u"], ["wget", "u"],
    ["gh", "api", "--method=DELETE", "x"], ["gh", "api", "--input", "f", "x"], ["gh", "api", "-XPOST", "x"],
    ["sort", "-o", "f", "g"], ["awk", "{system(\"rm x\")}"], ["find", ".", "-fprint", "x"],
    ["uniq", "a", "b"], ["tree", "-o", "f"], ["rg", "--pre", "x", "y"],
    ["PGPASSWORD=x", "psql", "-c", "drop table t"],
    # From the review of vote gifted-organ-marigold.
    ["sqlite3", "x.db", "select writefile('/p','x')"], ["sqlite3", "-readonly", "x.db", "select edit('a')"],
    ["sqlite3", "x.db", "select load_extension('/tmp/x.so')"], ["sqlite3", "x.db", "pragma user_version(5)"],
    ["sqlite3", "x.db", "pragma journal_mode(wal)"], ["sqlite3", "x.db", "pragma user_version = 5"],
    ["sqlite3", "-cmd", ".shell id", "x.db", "select 1"],
    ["sh", "-c", "echo x >&/etc/x"], ["sh", "-c", "cat a#; rm x"], ["sh", "-c", "echo '#'; rm x"],
    ["sh", "-c", "(rm x)"], ["sh", "-c", "cat a\nrm b"],
    ["git", "reflog", "expire", "--all"], ["git", "reflog", "delete", "x"],
    ["git", "-c", "core.fsmonitor=rm -rf ~", "status"], ["git", "-C", "/x", "status"],
    ["git", "ls-remote", "--upload-pack=cmd", "x"], ["git", "grep", "-Ocmd", "x"], ["git", "diff", "--ext-diff"],
    ["GIT_EXTERNAL_DIFF=cmd", "git", "diff"], ["LD_PRELOAD=x.so", "cat", "f"], ["LESSOPEN=|cmd", "less", "f"],
    ["env", "X=1", "cat", "f"], ["ssh", "-o", "ProxyCommand=cmd", "host", "cat", "x"],
    ["ssh", "-oProxyCommand=touch /tmp/p", "h", "uptime"], ["ssh", "h", "find", "/tmp", "-delete"],
    ["ip", "netns", "exec", "x", "sh"], ["journalctl", "--rotate"], ["date", "0101"],
    ["curl", "gopher://x/_a"], ["gh", "api", "graphql", "-f", "q=x"], ["sort", "-of", "g"],
    ["find", ".", "-exec", "rm", "{}", ";"], ["fd", "-x", "rm"], ["ss", "-K"], ["ss", "-tK"],
    ["top", "-b"], ["less", "f"], ["sudo", "-u", "x", "cat", "f"], ["sudo", "-E", "cat", "f"],
    ["timeout", "--signal=9", "5", "rm", "x"], ["docker", "-H", "tcp://x", "ps"],
    ["kubectl", "--kubeconfig", "k", "get", "po"], ["systemctl", "-H", "host", "status"],
])
def test_commands_that_can_write_never_read_as_read_only(argv):
    assert plain.shell_summary({"argv": argv})["kind"] != "read"
    p = plain.for_hold(held(args={"argv": argv}))
    assert not p["risk"].startswith("Low")


def test_a_cmd_string_is_read_like_argv():
    assert plain.shell_summary({"cmd": "cat /etc/hostname"})["title"] == "Read hostname"
    for argv in (["git", "log", "-3", "--oneline"], ["git", "config", "--get", "user.name"],
                 ["ip", "-br", "a"], ["nvidia-smi", "--query-gpu=name"], ["date", "+%s"],
                 ["sqlite3", "/x.db", "SELECT a FROM t; PRAGMA table_info(t)"],
                 ["sqlite3", "-readonly", "/x.db", "SELECT count(*) FROM t"],
                 ["journalctl", "-fu", "x"], ["curl", "-sSL", "-H", "A: b", "https://x"],
                 ["sudo", "-n", "systemctl", "status", "x"], ["timeout", "10", "cat", "f"],
                 ["head", "-20", "f"], ["find", ".", "-name", "*.py", "-type", "f"]):
        assert plain.shell_summary({"argv": argv})["kind"] == "read", argv


def test_what_leaves_the_dashboard_never_carries_the_args():
    from rook.hub.approval_contact import message, question
    typed = held(cap="proc.write", args={"data": "hunter2"}, name="calm-otter-lamp")
    run = held(args={"argv": ["sh", "-c", "PGPASSWORD=s3cret psql -c 'select 1'"]})
    for h in (typed, run):
        for text in (message([h]), question(h), message([h, h])):
            assert "hunter2" not in text and "s3cret" not in text and "psql" not in text
    assert message([run]).startswith("Rook: Run a command on bakenetcanada. For: no task.")


# -- each hold kind -----------------------------------------------------------------------

def test_each_hold_kind_says_its_risk_plainly():
    sacred = plain.for_hold(held(reason=TRUST.replace("(guarded)", "(sacred)").replace(
        "rook-hub", "master"), args={"argv": ["git", "push", "origin", "master"]}))
    assert sacred["title"] == "Push code"
    assert sacred["risk"] == ("Medium: it runs a command that can change things; master is "
                              "sacred, so only you change it.")
    hub_host = plain.for_hold(held(kind="hub-host", reason="runs on the hub's machine",
                                   rule=None, args={"argv": ["systemctl", "restart", "nginx"]}))
    assert hub_host["risk"].startswith("Medium: it runs a command that can change things; "
                                       "bakenetcanada runs on the hub's own machine")
    spread = plain.for_hold(held(kind="spread", reason="spread: 6th machine in 10 minutes",
                                 rule=None, args={"argv": ["uptime"]}))
    assert spread["risk"] == ("Low: looks read-only, but it would be the 6th machine this agent "
                              "touched in 10 minutes.")
    admin = plain.for_hold(held(cap="worker.update", tier="admin", kind="default",
                                reason="tier admin", rule=None, target_name="nas", args={}))
    assert (admin["title"], admin["want"]) == ("Update nas", "Install a new Rook build on nas")
    assert admin["risk"].startswith("High: it changes who can do what")
    job = plain.for_hold(held(via=["job:j_1"], principal_label="jobs",
                              context={"job": "nightly-backup", "why": "keep a nightly copy"},
                              cap="file.write", tier="write", kind="default", reason="rule",
                              args={"path": "/srv/backup/today.tar"}))
    assert (job["who"], job["for"], job["why"]) == ("job nightly-backup", "job nightly-backup",
                                                    "keep a nightly copy")
    assert job["title"] == "Write today.tar" and job["risk"].startswith("Medium")
    console = plain.for_hold(held(cap="proc.write", args={"data": "hunter2", "newline": True},
                                  context={"room": "r1", "room_title": "Kernel upgrade on nas"}))
    # Typed text may answer a password prompt: never in the title or sentence.
    assert console["for"] == "console Kernel upgrade on nas"
    assert (console["title"], console["want"]) == ("Type into a console",
                                                   "Type a line into a console on bakenetcanada")
    assert "hunter2" not in console["title"] + console["want"] + console["risk"]
    role = plain.for_hold(held(principal="home:home/role:qa", principal_label="role:qa"))
    assert role["who"] == "qa (huddle role)"
    note = plain.for_hold(held(cap="home.session_note", tier="write", target_name="rook",
                               is_hub=True, args={"text": "PR #190 merged", "role": "house"}))
    assert note["title"] == "Leave a note for the house agent"
    assert note["risk"].startswith("Low: it only adds or edits a record")


@pytest.mark.asyncio
async def test_the_relay_list_carries_the_plain_ask_for_every_kind(hub):  # noqa: F811
    await card_and_ask(hub)
    h = hold(hub.ap)
    job = hold(hub.ap, target="nas", via=("job:j_nightly",), principal="token:agent_jobs",
               label="jobs", kind="rule", reason="hold rule")
    items = {i["kind"]: i for i in hub.relay.items()}
    assert items["hold"]["id"] == h["id"]
    assert {k: items["hold"]["plain"][k] for k in ("title", "for", "who")} == {
        "title": "Restart nginx", "for": "no task", "who": "Claude web"}
    assert items["job"]["id"] == job["id"] and items["job"]["plain"]["who"] == "job j_nightly"
    card = items["card"]["plain"]
    assert (card["for"], card["who"], card["why"]) == ("huddle Ops", "qa (huddle role)",
                                                       "Rerun the nightly")
    assert card["title"] == "Check box" and "info.ping on box" in card["explain"]
    ask = items["ask"]["plain"]
    assert ask["title"] == "Ship on Friday?" and ask["for"] == "huddle Ops"
    assert plain.for_text({}, {"huddle_title": "Huddle: bakenet"}) == "huddle bakenet"
    assert ask["risk"].startswith("None by itself")
    # rook_approvals and the spoken line read the plain ask.
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    res = await cap(hub, "approvals.list", {})
    assert all(i["plain"]["title"] for i in res["result"]["items"])
    # Spoken: titles from the cap and machine only, never the args; no Explain.
    assert "Run a command on box (Claude web, for no task; " in res["result"]["summary"]
    assert "nginx" not in res["result"]["summary"]
    assert all("explain" not in i["plain"] for i in res["result"]["items"])


# -- notes never wait -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_note_card_is_recorded_not_put_to_the_person(hub):  # noqa: F811
    h = await card_and_ask(hub)
    chair = hub.svc.chair
    out = await chair._propose("r_9", h["room"], "role:qa",
                               {"kind": "note", "summary": "Decision: page the deck"})
    assert out["ok"] and out["state"] == "done"
    assert hub.svc.store.card(out["card"])["result"] == {"ok": True, "noted": True}
    assert not [i for i in hub.relay.items() if i["id"] == out["card"]]
    asked = await chair._propose("r_9", h["room"], "role:qa",
                                 {"kind": "ask_bake", "summary": "Ship the deck paging today?"})
    waiting = next(i for i in hub.relay.items() if i["id"] == asked["card"])
    assert waiting["plain"]["title"] == "Ship the deck paging today?"


def test_notes_waiting_from_before_are_settled_by_the_migration(tmp_path):
    db = sqlite3.connect(tmp_path / "chat.db")
    db.execute("CREATE TABLE huddle_cards(id TEXT, room TEXT, kind TEXT, state TEXT, result TEXT,"
               " decided_by TEXT, decided REAL)")
    db.executemany("INSERT INTO huddle_cards(id, room, kind, state) VALUES(?,?,?,?)",
                   [("c1", "r", "note", "proposed"), ("c2", "r", "task.create", "proposed"),
                    ("c3", "r", "note", "dismissed")])
    sql = (Path(plain.__file__).parent / "plugins/huddle/migrations/004_notes_never_wait.sql")
    db.executescript(sql.read_text())
    rows = dict(db.execute("SELECT id, state FROM huddle_cards").fetchall())
    assert rows == {"c1": "done", "c2": "proposed", "c3": "dismissed"}


@pytest.mark.asyncio
async def test_the_huddle_page_shows_waiting_cards_plainly(hub):  # noqa: F811
    h = await card_and_ask(hub)
    card, = (await _call(hub, "huddle.read", {"room": h["room"]}))["cards"]
    assert card["state"] == "proposed" and card["plain"]["for"] == "huddle Ops"
    assert card["plain"]["who"] == "qa (huddle role)"


def test_a_session_note_on_the_hub_is_prepare_not_a_change():
    assert classify("home.session_note", "write", (), on_hub=True) == "prepare"


# -- the context reaches the hold ------------------------------------------------------

@pytest.mark.asyncio
async def test_the_callers_task_and_why_are_kept_with_the_hold(tmp_path):
    ap = A.Approvals(str(tmp_path / "approvals.db"), A.Config(wait_secs=5, contact=()))
    d = check(Authorizer(PolicyStore(None), None, approvals=ap), "worker.update")
    tok = A.hold_context.set({"task": "t_1", "task_title": "Kernel upgrades", "project": "Fleet",
                              "why": "nas needs build 402 for the OTA fix", "bogus": "dropped"})
    try:
        waiting = asyncio.create_task(ap.resolve(d, {"build": 1}, label="claude code"))
        row = (await pending_hold(ap.queue))[0]
    finally:
        A.hold_context.reset(tok)
    assert row["context"] == {"task": "t_1", "task_title": "Kernel upgrades", "project": "Fleet",
                              "why": "nas needs build 402 for the OTA fix"}
    pub = A.public(row)
    assert pub["plain"]["for"] == "Kernel upgrades (Fleet)"
    assert pub["plain"]["why"] == "nas needs build 402 for the OTA fix"
    assert pub["plain"]["who"] == "claude code" and pub["plain"]["title"] == "Update nas"
    ap.decide(row["id"], False, "human:u1", scope="call")
    await waiting


def test_an_older_approvals_db_gets_the_context_column(tmp_path):
    path = tmp_path / "approvals.db"
    db = sqlite3.connect(path)
    db.executescript(A._SCHEMA.replace(", context_json TEXT", ""))
    db.execute("INSERT INTO holds(id, state, kind, principal, cap, tier, args_hash, created, "
               "expires) VALUES('h1','pending','default','token:x','worker.update','admin','x',1,"
               "9999999999)")
    db.commit()
    db.close()
    q = A.ApprovalQueue(str(path))
    old, = q.pending()
    assert old["context"] == {} and A.public(old)["plain"]["for"] == "no task"


@pytest.mark.approvals
@pytest.mark.asyncio
async def test_rook_call_why_reaches_the_hold(tmp_path, monkeypatch):
    from test_audit_attribution import Session, STATIC
    from test_permissions_mcp import _text, mcp
    from rook.hub.policy import DEFAULT_POLICY
    monkeypatch.setenv("ROOK_APPROVALS_WAIT_SECS", "0")
    monkeypatch.setenv("ROOK_APPROVALS_CONTACT", "off")
    doc = {**DEFAULT_POLICY, "rules": [*DEFAULT_POLICY["rules"],
                                       {"id": "hold-host", "who": "token:*", "hold": "info.host",
                                        "on": "gpu-box"}]}
    async with mcp(tmp_path, doc) as env:
        s = await Session(env.http, STATIC).open()
        out = _text(await s.result("rook_call", {"cap": "info.host", "worker": "gpu-box",
                                                 "args": {"build": 1},
                                                 "why": "check the GPU box's build"}))
        assert out.get("held"), out
        row, = A.ApprovalQueue(str(tmp_path / "approvals.db")).pending()
        assert row["context"]["why"] == "check the GPU box's build"
        assert A.public(row)["plain"]["why"] == "check the GPU box's build"


def test_a_task_card_with_one_criterion_as_text_is_fixed_when_proposed():
    from rook.hub.plugins.huddle.chair import CardError, validate_card
    card = validate_card({"kind": "task.create", "summary": "Check session merges",
                          "worker": "rook", "cap": "task.write",
                          "args": {"data": {"title": "Check merges", "parent": "rook",
                                            "attrs": {"criteria": "no push to master",
                                                      "tags": ["security"]}}}})
    assert card["call"]["args"]["data"]["attrs"] == {"criteria": ["no push to master"],
                                                     "tags": ["security"]}
    p = plain.describe(plain.card_item({**card, "id": "c_1", "by": "role:security"}, "H6"))
    assert p["title"] == 'Create task "Check merges"' and p["who"] == "security (huddle role)"
    assert p["want"] == 'Create the task "Check merges" under rook' and p["for"] == "huddle H6"
    with pytest.raises(CardError, match="attrs.criteria is a list of strings"):
        validate_card({"kind": "task.create", "summary": "x", "worker": "rook",
                       "cap": "task.write", "args": {"data": {"title": "t", "parent": "rook",
                                                              "attrs": {"criteria": [1, 2]}}}})
