"""The trust map (docs/design/trust.md, rook/hub/trust.py): rulings, hub
enforcement through the approval layer, exceptions, who may loosen or
tighten, proposals, the dashboard API, citations, and session isolation (the
push guard, the credential-free sandbox, minted tokens)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rook.hub import approvals as A
from rook.hub import trust as T
from rook.hub.authz import Authorizer, current_principal, principal_for_user
from rook.hub.policy import PolicyStore, Principal

pytestmark = pytest.mark.approvals

AGENT = Principal("token:claude", "token", "agent", label="claude code")
ROLE = Principal("home:rook/role:lead", "token", "agent", label="agent:rook/role:lead")
OWNER = principal_for_user({"id": "u1", "username": "bake"}, True)
FAST = A.Config(wait_secs=0, contact=())

BAKE_KAIJU = "kaiju changes are sacred, only ever under my direct direction"
BAKE_PIANO = "just ship it, worst case the music stops"
PERSON = T.Actor("person", "Bake", principal="human:u1", words="set on the dashboard")


def seeded(path=None, *, clock=time.time) -> T.TrustStore:
    st = T.TrustStore(path, clock=clock)
    st.change("set_entry", {"id": "kaiju", "title": "kaiju", "kind": "host", "level": "sacred",
                            "targets": ["worker:kaiju"]},
              T.Actor("person", "Bake", words=BAKE_KAIJU))
    st.change("set_entry", {"id": "family-devices", "kind": "devices", "level": "sacred",
                            "targets": ["worker:Logan's tablet", "worker:living-room-tv"]},
              T.Actor("person", "Bake", words="family devices are sacred"))
    st.change("set_entry", {"id": "pianobar-super", "kind": "project", "level": "ship-it",
                            "targets": ["worker:pianobar", "repo:Bake-Ware/pianobar-super",
                                        "project:pianobar-super"]},
              T.Actor("person", "Bake", words=BAKE_PIANO))
    st.change("set_entry", {"id": "rook-hub", "kind": "resource", "level": "guarded",
                            "targets": ["worker:bakenetcanada", "cap:hub.plugin.*",
                                        "cap:updates.rollout"]},
              T.Actor("person", "Bake", words="the live hub is guarded"))
    st.change("set_entry", {"id": "rook-master", "kind": "repo", "level": "guarded",
                            "targets": ["repo:Bake-Ware/rook#master"]},
              T.Actor("person", "Bake", words="rook master is guarded"))
    return st


def authorizer(tmp_path, store):
    ap = A.Approvals(str(tmp_path / "approvals.db"), FAST, trust=store)
    return Authorizer(PolicyStore(str(tmp_path / "policy.json")), None, approvals=ap)


def entry(name, caps=("shell.exec", "file.read", "worker.update")):
    return {"worker_id": name, "name": name, "caps": list(caps), "last_seen": time.time()}


def check(az, cap, target, principal=AGENT):
    if target == "rook":
        return az.check(cap, "rook", {"name": "rook", "roles": ["is_hub"]}, principal=principal,
                        local=True)
    return az.check(cap, target, entry(target), principal=principal)


# -- rulings ------------------------------------------------------------------------

def test_the_matrix_kind_against_level():
    st = seeded()
    r = st.rule("shell.exec", "exec", worker="kaiju")
    assert (r.tier, r.kind, r.level, r.entry["id"]) == ("person", "change", "sacred", "kaiju")
    assert st.rule("file.read", "read", worker="kaiju").tier == "go"        # agents may read
    assert st.rule("worker.update", "admin", worker="kaiju").tier == "person"
    assert st.rule("shell.exec", "exec", worker="pianobar").tier == "go"
    assert st.rule("worker.update", "admin", worker="pianobar").tier == "go"
    assert st.rule("shell.exec", "exec", worker="bakenetcanada").tier == "ask"
    assert st.rule("hub.plugin.disable", "admin", worker="rook", on_hub=True).tier == "ask"
    # Unlisted: balanced (the hub's old default): changes go, admin asks.
    gap = st.rule("shell.exec", "exec", worker="cachyrig")
    assert (gap.tier, gap.unlisted, gap.level, gap.rule) == ("go", True, "balanced",
                                                            f"trust:unlisted@v{gap.version}")
    assert st.rule("worker.update", "admin", worker="cachyrig").tier == "ask"
    # Preparing (the band's own records) is free even where changes are not.
    assert st.rule("task.write", "write", worker="rook", on_hub=True).kind == "prepare"
    assert st.rule("task.write", "write", worker="rook", on_hub=True).tier == "go"


def test_repo_and_branch_selectors():
    st = seeded()
    r = st.rule("", "admin", repo="https://github.com/Bake-Ware/rook.git", branch="master")
    assert (r.tier, r.entry["id"]) == ("ask", "rook-master")
    assert st.rule("", "admin", repo="Bake-Ware/rook", branch="trust-map").unlisted
    assert st.rule("", "admin", repo="bake-ware/pianobar-super", branch="main").tier == "go"


def test_strictest_entry_wins_and_gaps_can_be_tightened():
    st = seeded()
    st.change("set_entry", {"id": "toy", "level": "ship-it", "targets": ["worker:kaiju"]}, PERSON)
    assert st.rule("shell.exec", "exec", worker="kaiju").entry["id"] == "kaiju"
    st.change("set_unlisted", {"level": "guarded"}, PERSON)
    assert st.rule("shell.exec", "exec", worker="cachyrig").tier == "ask"


def test_every_ruling_cites_entry_version_and_the_persons_words():
    st = seeded()
    r = st.rule("shell.exec", "exec", worker="kaiju")
    assert r.rule == f"trust:kaiju@v{r.version}"
    assert '"kaiju" (sacred)' in r.cite and BAKE_KAIJU in r.cite and "Bake" in r.cite
    assert "direct instruction" in r.cite


# -- exceptions ---------------------------------------------------------------------

def test_an_exception_lowers_until_it_expires_and_never_covers_excluded_entries():
    now = [1_800_000_000.0]
    st = seeded(clock=lambda: now[0])
    st.change("add_exception", {"id": "overnight-20261023", "title": "Overnight push",
                                "applies_to": ["*"], "excludes": ["family-devices"],
                                "expires": now[0] + 3600},
              T.Actor("person", "Bake", words="go ahead and deploy whatever wherever"))
    r = st.rule("shell.exec", "exec", worker="kaiju")
    assert r.tier == "go" and r.base_tier == "person"
    assert r.rule.endswith("+overnight-20261023") and "deploy whatever wherever" in r.cite
    assert st.rule("shell.exec", "exec", worker="Logan's tablet").tier == "person"
    now[0] += 3601
    assert st.rule("shell.exec", "exec", worker="kaiju").tier == "person"


def test_a_scoped_exception_covers_only_its_own_footprint():
    st = seeded()
    st.change("add_exception", {"id": "toy-gpu1", "applies_to": ["kaiju"],
                                "targets": ["cap:docker.*"], "expires": time.time() + 600},
              T.Actor("person", "Bake", words="the toy can run its own containers on kaiju"))
    assert st.rule("docker.run", "exec", worker="kaiju").tier == "go"
    assert st.rule("shell.exec", "exec", worker="kaiju").tier == "person"
    assert st.rule("shell.exec", "exec", worker="bakenetcanada").tier == "ask"


def test_ending_an_exception_restores_the_entry():
    st = seeded()
    st.change("add_exception", {"id": "x1", "expires": time.time() + 600}, PERSON)
    assert st.rule("shell.exec", "exec", worker="kaiju").tier == "go"
    st.change("end_exception", {"id": "x1"}, PERSON)
    assert st.rule("shell.exec", "exec", worker="kaiju").tier == "person"


# -- the hub enforces it ------------------------------------------------------------

def test_sacred_action_is_held_for_the_person_with_the_citation(tmp_path):
    az = authorizer(tmp_path, seeded())
    d = check(az, "shell.exec", "kaiju")
    assert d.held and d.hold_kind == "trust" and d.rule.startswith("trust:kaiju@v")
    assert BAKE_KAIJU in d.hold_reason and d.trust_rule == d.rule
    assert check(az, "shell.exec", "kaiju", principal=ROLE).held        # roles too
    assert not check(az, "file.read", "kaiju").held                     # reads go
    # The person themselves (the dashboard) is never held: that is their direct instruction.
    assert not check(az, "shell.exec", "kaiju", principal=OWNER).held


@pytest.mark.asyncio
async def test_a_held_sacred_call_tells_the_agent_why_and_runs_once_approved(tmp_path):
    az = authorizer(tmp_path, seeded())
    held = await az.resolve(check(az, "shell.exec", "kaiju"), {"argv": ["reboot"]})
    assert held["held"] and '"kaiju" (sacred)' in held["error"] and BAKE_KAIJU in held["error"]
    az.approvals.decide(held["hold"], True, "human:u1")
    out = await az.resolve(check(az, "shell.exec", "kaiju"), {"argv": ["reboot"]})
    assert out.decision == "allow" and out.trust_rule.startswith("trust:kaiju@v")


def test_ship_it_runs_and_still_cites_its_entry(tmp_path):
    az = authorizer(tmp_path, seeded())
    d = check(az, "shell.exec", "pianobar")
    assert d.decision == "allow" and d.trust_rule.startswith("trust:pianobar-super@v")
    assert BAKE_PIANO in d.trust and d.journal()["trust"]["rule"] == d.trust_rule


def test_ship_it_never_lowers_the_bands_own_admin_hold(tmp_path):
    az = authorizer(tmp_path, seeded())
    d = check(az, "worker.update", "pianobar")
    assert d.held and d.hold_kind == "default" and d.trust_rule.startswith("trust:pianobar")


def test_guarded_asks_first_and_unlisted_is_unchanged(tmp_path):
    az = authorizer(tmp_path, seeded())
    d = check(az, "shell.exec", "bakenetcanada")
    assert d.held and d.hold_kind == "trust" and "rook-hub" in d.rule
    assert check(az, "hub.plugin.disable", "rook").hold_kind == "trust"
    assert not check(az, "task.write", "rook").held               # preparing is free
    assert not check(az, "shell.exec", "cachyrig").held           # as before the map
    gap = check(az, "worker.update", "cachyrig")
    assert gap.held and gap.hold_kind == "default" and gap.trust_rule.startswith("trust:unlisted")


def test_the_journal_row_carries_the_citation(tmp_path):
    from rook.band_mcp.journal import Journal
    j = Journal(str(tmp_path / "journal.db"))
    d = check(authorizer(tmp_path, seeded()), "shell.exec", "pianobar")
    j.record(cap="shell.exec", worker="pianobar", identity="token:claude", args=None,
             reply={"ok": True}, authz=d.journal())
    import sqlite3
    rule = sqlite3.connect(str(tmp_path / "journal.db")).execute(
        "SELECT rule FROM calls").fetchone()[0]
    assert "trust:pianobar-super@v" in rule


def test_no_map_file_is_the_old_behaviour(tmp_path):
    az = authorizer(tmp_path, T.TrustStore(str(tmp_path / "trust.json")))
    assert not check(az, "shell.exec", "kaiju").held
    assert check(az, "worker.update", "kaiju").hold_kind == "default"


def test_build_authorizer_reads_trust_json_beside_policy_json(tmp_path):
    from rook.hub.authz import build_authorizer
    seeded(str(tmp_path / "trust.json"))
    az = build_authorizer(str(tmp_path), signer=False)
    assert check(az, "shell.exec", "kaiju").hold_kind == "trust"


# -- who changes it -----------------------------------------------------------------

def test_an_agent_cannot_loosen_or_change_the_map():
    st = seeded()
    agent = T.Actor("agent", "claude code", words="Bake said so")
    with pytest.raises(T.TrustError, match="only a person"):
        st.change("set_entry", {"id": "kaiju", "level": "ship-it", "targets": ["worker:kaiju"]},
                  agent)
    with pytest.raises(T.TrustError, match="only a person"):
        st.change("add_exception", {"id": "x", "expires": time.time() + 60}, agent)
    assert st.rule("shell.exec", "exec", worker="kaiju").tier == "person"


def test_the_house_agent_tightens_but_never_loosens_and_it_is_surfaced():
    st = seeded()
    house = T.Actor("house", "rook", words="Bake: never touch the NAS without me")
    for op, data in (("set_entry", {"id": "kaiju", "level": "guarded", "targets": ["worker:kaiju"]}),
                     ("set_entry", {"id": "pianobar-super", "level": "ship-it",
                                    "targets": ["worker:pianobar"]}),          # drops targets
                     ("set_entry", {"id": "new", "level": "ship-it", "targets": ["worker:x"]}),
                     ("delete_entry", {"id": "rook-hub"}),
                     ("set_unlisted", {"level": "ship-it"}),
                     ("add_exception", {"id": "e", "expires": time.time() + 60})):
        with pytest.raises(T.TrustError):
            st.change(op, data, house)
    out = st.change("set_entry", {"id": "nas", "kind": "host", "level": "sacred",
                                  "targets": ["worker:nas"]}, house)
    assert st.rule("shell.exec", "exec", worker="nas").tier == "person"
    doc = st.current()
    assert doc["notices"][-1]["version"] == out["version"] and "NAS" in doc["notices"][-1]["words"]
    st.change("ack", {"version": out["version"]}, PERSON)
    assert not st.current()["notices"]
    assert st.history(1)[0]["op"] == "ack"


def test_words_are_required_and_recorded_with_who_and_when():
    st = seeded()
    with pytest.raises(T.TrustError, match="words are required"):
        st.change("set_unlisted", {"level": "guarded"}, T.Actor("person", "Bake", words=""))
    row = st.history(entry="kaiju")[0]
    assert row["actor"]["who"] == "Bake" and row["words"] == BAKE_KAIJU and row["after"]["level"] == "sacred"
    assert st.current()["entries"][0]["set_by"]["words"] == BAKE_KAIJU


def test_entries_relayed_by_someone_else_wait_for_confirmation():
    st = T.TrustStore(None)
    st.change("set_entry", {"id": "kaiju", "level": "sacred", "targets": ["worker:kaiju"]},
              T.Actor("system", "Bake", words=BAKE_KAIJU, via="overnight agent"))
    e = st.current()["entries"][0]
    assert e["confirmed"] is False and e["set_by"]["via"] == "overnight agent"
    st.change("confirm", {"id": "kaiju"}, PERSON)
    assert st.current()["entries"][0]["confirmed"] is True


def test_proposals_wait_for_the_person_who_accepts_or_rejects():
    st = seeded()
    with pytest.raises(T.TrustError, match="why is required"):
        st.propose({"op": "set_unlisted", "data": {"level": "ship-it"}}, by="claude")
    p = st.propose({"op": "set_entry", "data": {"id": "pianobar-super", "level": "guarded",
                                                "targets": ["worker:pianobar"]}},
                   by="claude", why="the music stopped twice this week")
    assert st.rule("shell.exec", "exec", worker="pianobar").tier == "go"   # nothing changed yet
    with pytest.raises(T.TrustError):
        st.change("decide_proposal", {"id": p["id"], "accept": True},
                  T.Actor("house", "rook", words="ok"))
    st.change("decide_proposal", {"id": p["id"], "accept": True},
              T.Actor("person", "Bake", words="fine, guard it"))
    assert st.rule("shell.exec", "exec", worker="pianobar").tier == "ask"
    assert st.current()["proposals"][-1]["state"] == "accepted"
    with pytest.raises(T.TrustError, match="no entry"):      # checked when proposed
        st.propose({"op": "delete_entry", "data": {"id": "nope"}}, by="c", why="tidy up")


def test_the_file_is_readable_versioned_and_shared_between_processes(tmp_path):
    path = str(tmp_path / "trust.json")
    a = seeded(path)
    b = T.TrustStore(path, check_every=0)
    assert b.rule("shell.exec", "exec", worker="kaiju").tier == "person"
    doc = json.loads(open(path).read())
    assert doc["format"] == T.FORMAT and doc["version"] == 5 and doc["entries"][0]["id"] == "kaiju"
    a.change("set_entry", {"id": "kaiju", "level": "guarded", "targets": ["worker:kaiju"]},
             T.Actor("person", "Bake", words="relax a bit"))
    assert b.rule("shell.exec", "exec", worker="kaiju").tier == "ask"
    assert len(open(tmp_path / "trust-history.jsonl").read().splitlines()) == 6


def test_a_broken_file_keeps_the_last_good_map_and_refuses_writes(tmp_path):
    path = tmp_path / "trust.json"
    st = seeded(str(path))
    st._check_every = 0
    assert st.rule("shell.exec", "exec", worker="kaiju").tier == "person"
    path.write_text("{not json")
    assert st.rule("shell.exec", "exec", worker="kaiju").tier == "person" and st.error
    with pytest.raises(T.TrustError, match="unreadable"):
        st.change("set_unlisted", {"level": "sacred"}, PERSON)


def test_summary_for_agents_is_short_and_names_the_levels():
    st = seeded()
    st.change("add_exception", {"id": "overnight", "excludes": ["family-devices"],
                                "expires": "2099-10-23T12:00:00-05:00"}, PERSON)
    s = st.summary()
    assert "sacred: kaiju, family-devices" in s and "ship-it: pianobar-super" in s
    assert "Exception overnight: everything except family-devices -> go until 2099-10-23T12:00:00-05:00" in s
    assert len(s) < 900


# -- the plugin (caps agents reach) ---------------------------------------------------

@pytest.fixture
def plugin():
    from rook.hub.plugins.trust import Trust
    p = Trust()
    p._store = seeded()
    p.bind_host(SimpleNamespace(client=SimpleNamespace(workers={}), plugin=lambda n: None))
    return p


def _as(principal, att=None):
    from rook.band_mcp import attribution
    t1 = current_principal.set(principal)
    t2 = attribution.current.set(att)
    return lambda: (attribution.current.reset(t2), current_principal.reset(t1))


def test_agents_read_check_and_propose_through_caps(plugin):
    undo = _as(AGENT)
    try:
        assert "kaiju" in plugin.get()["summary"]
        r = plugin.check(cap="shell.exec", worker="kaiju", tier="exec")
        assert r["tier"] == "person" and BAKE_KAIJU in r["cite"]
        assert plugin.check(repo="Bake-Ware/rook", branch="master")["tier"] == "ask"
        out = plugin.propose({"op": "set_unlisted", "data": {"level": "guarded"}},
                             why="more care please")
        assert out["state"] == "pending"
        with pytest.raises(PermissionError):
            plugin.tighten("set_unlisted", {"level": "sacred"}, words="x y")
    finally:
        undo()


def test_only_the_house_agent_itself_tightens_through_the_cap(plugin):
    from rook.band_mcp.attribution import Attribution
    role = Attribution(identity="agent:rook/role:lead", kind="home", agent_id="home:rook/role:lead")
    undo = _as(ROLE, role)
    try:
        with pytest.raises(PermissionError):
            plugin.tighten("set_unlisted", {"level": "guarded"}, words="x y")
    finally:
        undo()
    house = Attribution(identity="agent:rook", kind="home", label="rook", agent_id="home:rook")
    undo = _as(Principal("token:home:rook", "token", "agent"), house)
    try:
        with pytest.raises(ValueError, match="person's alone"):
            plugin.tighten("set_entry", {"id": "kaiju", "level": "ship-it",
                                         "targets": ["worker:kaiju"]}, words="Bake: relax")
        plugin.tighten("set_unlisted", {"level": "guarded"}, words="Bake: be careful today")
        assert plugin.store.current()["notices"][-1]["who"] == "rook"
    finally:
        undo()


def test_trust_changes_are_never_relayed():
    from rook.hub.relay import FLOOR, matches
    assert any(matches(s, {"cap": "trust.tighten"}) for s in FLOOR)


# -- the dashboard ------------------------------------------------------------------

def dashboard(tmp_path, principal, store):
    from rook.remote.trust_web import TrustWeb
    az = authorizer(tmp_path, store)

    async def credential_is_vaulted(*values):
        return False

    server = SimpleNamespace(_band=SimpleNamespace(authz=az), _authz=az,
                             _accounts=SimpleNamespace(store=SimpleNamespace(
                                 session_weak=lambda tok: False)),
                             credential_is_vaulted=credential_is_vaulted)

    @web.middleware
    async def who(request, handler):
        tok = current_principal.set(principal[0])
        try:
            return await handler(request)
        finally:
            current_principal.reset(tok)

    app = web.Application(middlewares=[who])
    TrustWeb(server).install(app)
    return app


@pytest.mark.asyncio
async def test_dashboard_person_views_and_changes_with_words(tmp_path):
    st = seeded(str(tmp_path / "trust.json"))
    st.propose({"op": "set_unlisted", "data": {"level": "guarded"}}, by="claude", why="be careful")
    principal = [OWNER]
    async with TestClient(TestServer(dashboard(tmp_path, principal, st))) as http:
        assert (await (await http.get("/api/trust?count=1")).json()) == {"waiting": 1}
        data = await (await http.get("/api/trust")).json()
        assert data["map"]["version"] == 5 and "kaiju" in data["summary"] and data["can_edit"]
        r = await http.post("/api/trust", json={"op": "set_entry", "data": {
            "id": "kaiju", "level": "guarded", "targets": ["worker:kaiju"]}})
        assert r.status == 409 and "words" in (await r.json())["error"]
        r = await http.post("/api/trust", json={"op": "set_entry", "words": "kaiju can relax", "data": {
            "id": "kaiju", "level": "guarded", "targets": ["worker:kaiju"]}})
        assert r.status == 200 and (await r.json())["version"] == 6
        assert st.history(1)[0]["actor"]["principal"] == "human:u1"
        r = await http.post("/api/trust", json={"op": "set_unlisted", "words": "x y",
                                                "data": {"level": "sacred"}},
                            headers={"Origin": "https://evil.example.org"})
        assert r.status == 403
        prop = data["map"]["proposals"][0]["id"]
        r = await http.post("/api/trust", json={"op": "decide_proposal", "words": "sure",
                                                "data": {"id": prop, "accept": True}})
        assert r.status == 200 and st.current()["unlisted"] == "guarded"


@pytest.mark.asyncio
async def test_dashboard_refuses_agents_and_members(tmp_path):
    st = seeded(str(tmp_path / "trust.json"))
    member = principal_for_user({"id": "u2", "username": "kid"}, False)
    principal = [AGENT]
    async with TestClient(TestServer(dashboard(tmp_path, principal, st))) as http:
        for p in (AGENT, ROLE, member, None):
            principal[0] = p
            assert (await http.get("/api/trust")).status == 403
            r = await http.post("/api/trust", json={"op": "set_unlisted", "words": "loosen it",
                                                    "data": {"level": "ship-it"}})
            assert r.status == 403
    assert st.current()["unlisted"] == "balanced"


# -- session isolation ----------------------------------------------------------------

def _git(*args, cwd=None, env=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=check, capture_output=True,
                          text=True)


@pytest.fixture
def session_repo(tmp_path):
    """A bare 'GitHub' remote and a main checkout of it, plus a fake HOME with
    a fake claude that tries to push master, pushes its branch, and reports
    what credentials it can see."""
    if not shutil.which("git"):
        pytest.skip("git not installed")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for k in [k for k in env if k.startswith("GIT_CONFIG")]:
        env.pop(k)
    remote, repo, home = tmp_path / "remote.git", tmp_path / "repo", tmp_path / "home"
    _git("init", "-q", "--bare", "-b", "master", str(remote), env=env)
    _git("clone", "-q", str(remote), str(repo), env=env)
    (repo / "README").write_text("x\n")
    _git("add", "README", cwd=repo, env=env)
    _git("commit", "-qm", "init", cwd=repo, env=env)
    _git("push", "-q", "origin", "HEAD:master", cwd=repo, env=env)
    (home / ".local/bin").mkdir(parents=True)
    (home / ".config/gh").mkdir(parents=True)
    (home / ".config/gh/hosts.yml").write_text("oauth_token: ADMIN-SECRET\n")
    claude = home / ".local/bin/claude"
    claude.write_text("""#!/bin/sh
cat >/dev/null
echo change > change.txt; git add change.txt; git commit -qm change
if git push -q origin HEAD:master 2>err.txt; then echo "MASTER: pushed"; else echo "MASTER: refused $(cat err.txt | head -1)"; fi
if git push -q origin "HEAD:$ROOK_SESSION_BRANCH" 2>/dev/null; then echo "BRANCH: pushed"; else echo "BRANCH: refused"; fi
if cat "$HOME/.config/gh/hosts.yml" 2>/dev/null | grep -q ADMIN; then echo "CREDS: visible"; else echo "CREDS: hidden"; fi
echo "TOKEN: ${GH_TOKEN:-none}"
""")
    claude.chmod(0o755)
    env.update(HOME=str(home), XDG_RUNTIME_DIR=str(tmp_path / "run"))
    return SimpleNamespace(remote=remote, repo=repo, home=home, env=env, tmp=tmp_path)


def _launch(s, branch="feature-x", token=""):
    from rook.hub.plugins.roles import sessions as rs
    env = {**s.env, "ROOK_SESSION_PROMPT": "do it", "ROOK_SESSION_SETTINGS": "{}"}
    if token:
        env["ROOK_SESSION_GH_TOKEN"] = token
    wt = s.tmp / "wt" / branch
    out = subprocess.run(["/bin/sh", "-c", rs.LAUNCH, "rook-session", str(s.repo), str(wt), branch,
                          "origin/master", "sid-1", "", ""], env=env, capture_output=True,
                         text=True, timeout=60)
    return out.stdout + out.stderr


def test_a_session_push_to_master_is_refused_by_the_guard(session_repo):
    out = _launch(session_repo)
    assert "MASTER: refused rook: refused: this session may push only refs/heads/feature-x" in out
    assert "BRANCH: pushed" in out and "ROOK-EXIT: 0" in out
    heads = _git("ls-remote", str(session_repo.remote), env=session_repo.env).stdout
    assert "refs/heads/feature-x" in heads
    master = _git("rev-parse", "master", cwd=session_repo.remote, env=session_repo.env).stdout
    assert _git("log", "--format=%s", "-1", master.strip(), cwd=session_repo.remote,
                env=session_repo.env).stdout.strip() == "init"          # master untouched


def _bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--dev-bind", "/", "/", "--proc", "/proc", "--unshare-pid",
                           "true"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _bwrap_works(), reason="bwrap cannot make namespaces here")
def test_a_session_with_a_minted_token_cannot_see_the_hosts_credentials(session_repo):
    out = _launch(session_repo, token="ghs_minted")
    assert "CREDS: hidden" in out and "TOKEN: ghs_minted" in out
    assert "MASTER: refused" in out and "BRANCH: pushed" in out
    without = _launch(session_repo, branch="feature-y")
    assert "CREDS: visible" in without and "TOKEN: none" in without     # the old path


def test_minting_a_token_scoped_to_one_repo_and_logged_without_the_token(tmp_path):
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from rook.hub import session_tokens as S
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    seen = []

    def http(url, headers, body, method="POST"):
        seen.append((method, url, headers, body))
        if method == "DELETE":
            return {}
        return {"token": "ghs_secret_value", "expires_at": "2026-10-23T08:00:00Z",
                "permissions": body["permissions"]}

    m = S.Minter(str(tmp_path / "session-tokens.jsonl"), http=http)
    cfg = S.AppConfig("12345", "678", pem)
    out = m.mint(cfg, repo="Bake-Ware/rook", session="s1", role="lead", why="fix the bug")
    method, url, headers, body = seen[0]
    assert url.endswith("/app/installations/678/access_tokens")
    assert body == {"repositories": ["rook"], "permissions": S.PERMISSIONS}
    claims = jwt.decode(headers["Authorization"][7:], key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600
    assert out["token"] == "ghs_secret_value" and out["repo"] == "Bake-Ware/rook"
    log_text = (tmp_path / "session-tokens.jsonl").read_text()
    assert "ghs_secret_value" not in log_text and out["fingerprint"] in log_text
    with pytest.raises(S.MintError, match="never carries administration"):
        m.mint(cfg, repo="Bake-Ware/rook", session="s2", role="lead", why="x",
               permissions={"administration": "write"})

    def greedy(url, headers, body, method="POST"):
        seen.append((method, url))
        return {} if method == "DELETE" else {"token": "ghs_x", "permissions": {
            **body["permissions"], "administration": "write"}}
    with pytest.raises(S.MintError, match="grants administration"):
        S.Minter(None, http=greedy).mint(cfg, repo="Bake-Ware/rook", session="s3", role="r", why="x")
    assert seen[-1][0] == "DELETE"                                  # revoked at once
    with pytest.raises(S.NotConfigured):
        S.AppConfig.from_settings({}.get)
