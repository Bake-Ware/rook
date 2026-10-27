"""Access v2 item 8: jobs run as their creator and never exceed them.

Each run re-reads its person's role and access, the authorizer checks every
call it makes (and every member it acts for), a person with no access left
stops their jobs, a manual run by someone else is bounded by them too, and a
``tool`` step goes through the authorizer like an agent's tool call."""
import json

import pytest

from jobs_fakes import Clock, FakeRuntime, drain, job_doc, make, ok, shell
from rook.hub import access as A
from rook.hub.authz import build_authorizer, principal_for_user
from rook.hub.plugins.jobs.guardrails import Blocked
from rook.hub.plugins.jobs.identity import IdentityRevoked, acting_for, resolve_identity
from rook.hub.plugins.jobs.model import check
from rook.hub.plugins.jobs.principals import Directory
from rook.hub.plugins.jobs.runtime import Runtime

GUEST = {"id": "human:u_guest", "kind": "human", "role": "member", "groups": ["human:member"],
         "label": "guest", "verified": True}
BAKE = {"id": "human:u_bake", "kind": "human", "role": "owner", "groups": ["human:owner"],
        "label": "bake", "verified": True}
WORKERS = {"w1": {"name": "alpha", "caps": ["shell.exec"]},
           "w_pc": {"name": "kids-pc", "caps": ["shell.exec"]}}


class Accounts:
    def __init__(self, users):
        self.users = users

    def user(self, uid):
        return self.users.get(uid)


def accounts():
    return Accounts({"u_bake": {"id": "u_bake", "username": "bake"},
                     "u_guest": {"id": "u_guest", "username": "guest"}})


def authorizer(tmp_path):
    az = build_authorizer(str(tmp_path / "hub"), signer=False)
    az.signer = None
    az.access.set_role("u_bake", "owner", actor="test", words="Bake owns the hub")
    az.access.grant("u_guest", "worker", "kids-pc", actor="test", words="the guest gets the kids' pc")
    return az


class AuthzRuntime(FakeRuntime):
    """The band client's part: every call is checked by the hub's authorizer
    for the run's principal before it reaches a worker."""

    def __init__(self, tmp_path, az, **kw):
        super().__init__(tmp_path, workers=dict(WORKERS), caps={"shell.exec": shell()}, **kw)
        self.az = az
        self.decisions = []

    async def call(self, cap, args, target, timeout, identity):
        d = self.az.check(cap, target, self.roster().get(target), principal=identity.principal)
        self.decisions.append(d)
        if d.denied:
            return {"ok": False, "error": d.reason, "denied": {"rule": d.rule}}
        return await super().call(cap, args, target, timeout, identity)


def job_on(worker, **kw):
    job, _ = check(job_doc({"a": {"kind": "cap", "worker": worker, "cap": "shell.exec",
                                  "args": {"argv": ["true"]}}}, **kw))
    return job


def setup(tmp_path, az, **resolve_kw):
    clock = Clock()
    rt = AuthzRuntime(tmp_path, az, clock=clock)
    store, sched, rt, _ = make(tmp_path / "jobs", runtime=rt, clock=clock)
    directory = Directory(accounts=accounts(), access=az.access)
    sched.resolve = lambda j, o: resolve_identity(j, o, directory=directory, **resolve_kw)
    return store, sched, rt, clock


async def run(store, sched, row, clock, **kw):
    r = store.enqueue(row, "manual", clock(), **kw)
    await sched.tick()
    await drain(sched)
    return store.get_run(r["id"])


# -- the authorizer: every member in the chain ---------------------------------------------

def test_a_call_never_exceeds_a_member_it_acts_for(tmp_path):
    az = authorizer(tmp_path)
    bake = principal_for_user({"id": "u_bake", "name": "bake"}, True)
    pc, nas = {"name": "kids-pc"}, {"name": "nas"}
    assert not az.check("shell.exec", "w_nas", nas, principal=bake).denied
    # The owner acting for the guest (a run the guest started): the guest's grants bound it.
    for_guest = bake.__class__(**{**bake.__dict__, "via": ("job:j_1", "human:u_guest")})
    d = az.check("shell.exec", "w_nas", nas, principal=for_guest)
    assert d.denied and d.rule == "access:no-grant" and d.reason.startswith("acting for human:u_guest")
    assert not az.check("shell.exec", "w_pc", pc, principal=for_guest).denied
    # Owners in the chain, and accounts the access store does not know, are not limited here.
    for_owner = bake.__class__(**{**bake.__dict__, "via": ("job:j_1", "human:u_bake", "human:u_x")})
    assert not az.check("shell.exec", "w_nas", nas, principal=for_owner).denied
    # Once the guest's grant goes, nothing done for them gets through.
    az.access.revoke(az.access.grants("u_guest")[0]["id"], actor="test", words="no more")
    assert az.check("shell.exec", "w_pc", pc, principal=for_guest).denied


def test_a_broken_access_store_denies_calls_made_for_a_person(tmp_path):
    az = authorizer(tmp_path)

    class Broken(A.AccessStore):
        def role(self, uid):
            raise RuntimeError("disk")
    az.access.__class__ = Broken
    bake = principal_for_user({"id": "u_bake", "name": "bake"}, True)
    for_guest = bake.__class__(**{**bake.__dict__, "via": ("human:u_guest",)})
    assert az.check("shell.exec", "w_pc", {"name": "kids-pc"}, principal=for_guest).decision == "error_deny"
    assert not az.check("shell.exec", "w_pc", {"name": "kids-pc"}, principal=bake).denied


# -- the run's identity is read live -----------------------------------------------------------

def test_the_creators_role_is_read_at_every_run(tmp_path):
    az = authorizer(tmp_path)
    d = Directory(accounts=accounts(), access=az.access)
    job = {**job_on("alpha"), "id": "j_1"}
    # Saved while Bake was an owner; demoted since: this run is a member's.
    az.access.set_role("u_guest", "owner", actor="test", words="second owner")
    az.access.set_role("u_bake", "member", actor="test", words="step down")
    az.access.grant("u_bake", "worker", "alpha", actor="test", words="keeps alpha")
    ident = resolve_identity(job, BAKE, directory=d)
    assert ident.principal.role == "member" and A.restricted(ident.principal)
    # The guest was saved as a member, and is an owner now.
    ident = resolve_identity(job, GUEST, directory=d)
    assert ident.principal.role == "owner" and not A.restricted(ident.principal)


def test_a_person_with_no_access_left_stops_their_jobs_whatever_the_fallback(tmp_path):
    az = authorizer(tmp_path)
    d = Directory(accounts=accounts(), access=az.access)
    job = {**job_on("kids-pc", identity={"mode": "creator", "fallback": "token:static"}), "id": "j_1"}
    assert resolve_identity(job, GUEST, directory=d).id == "human:u_guest"
    az.access.revoke(az.access.grants("u_guest")[0]["id"], actor="test", words="guest left")
    with pytest.raises(IdentityRevoked, match="no access"):
        resolve_identity(job, GUEST, directory=d)
    # Nor does the hub-wide default fallback rescue it.
    plain = {**job_on("kids-pc"), "id": "j_2"}
    with pytest.raises(IdentityRevoked, match="owner human:u_guest was revoked"):
        resolve_identity(plain, GUEST, directory=d,
                         settings=lambda k: "token:static" if k == "default_fallback" else None)
    # A job of theirs that runs as another key stops too.
    keyed = {**job_on("kids-pc", identity={"mode": "key", "ref": "token:static"}), "id": "j_3"}
    with pytest.raises(IdentityRevoked, match="owner human:u_guest"):
        resolve_identity(keyed, GUEST, directory=d)
    # A deleted account is revoked even with no access store.
    gone = Directory(accounts=Accounts({}))
    with pytest.raises(IdentityRevoked, match="no longer exists"):
        resolve_identity(plain, GUEST, directory=gone)


def test_a_key_runs_jobs_as_its_person(tmp_path):
    """Access v2 item 7 meets item 8: a job made with a key runs as the
    key's person, a key in the chain is its person, and a key whose person
    has no access left stops its jobs."""
    az = authorizer(tmp_path)

    class Keys:
        def list_api_tokens(self):
            return [{"id": "k1", "agent_id": "agent_g", "name": "guest laptop"},
                    {"id": "k2", "agent_id": "agent_b", "name": "bake laptop"}]
    az.access.set_token_user("agent_g", "u_guest", actor="test", words="the guest's key")
    az.access.set_token_user("agent_b", "u_bake", actor="test", words="Bake's key")
    d = Directory(tokens=Keys(), accounts=accounts(), access=az.access)
    key = {"id": "token:agent_g", "kind": "token", "role": "agent", "groups": [], "label": "guest laptop",
           "verified": True}
    job = {**job_on("kids-pc"), "id": "j_1"}
    ident = resolve_identity(job, key, directory=d)
    assert ident.id == "token:agent_g" and ident.principal.role == "member"
    assert ident.principal.user == "u_guest" and A.restricted(ident.principal)
    assert az.check("shell.exec", "w_nas", {"name": "nas"}, principal=ident.principal).denied
    assert not az.check("shell.exec", "w_pc", {"name": "kids-pc"}, principal=ident.principal).denied
    # Bake's key is an owner's.
    mine = resolve_identity(job, {**key, "id": "token:agent_b", "label": "bake laptop"}, directory=d)
    assert mine.principal.role == "owner" and not A.restricted(mine.principal)
    # An owner acting for the guest's key is bounded by the guest's grants.
    bake = principal_for_user({"id": "u_bake", "name": "bake"}, True)
    for_key = bake.__class__(**{**bake.__dict__, "via": ("job:j_1", "token:agent_g")})
    d2 = az.check("shell.exec", "w_nas", {"name": "nas"}, principal=for_key)
    assert d2.denied and d2.reason.startswith("acting for token:agent_g")
    # The guest's access goes: the key's jobs stop.
    az.access.revoke(az.access.grants("u_guest")[0]["id"], actor="test", words="guest left")
    with pytest.raises(IdentityRevoked, match="no access"):
        resolve_identity(job, key, directory=d)


def test_acting_for_adds_the_starter_once():
    job = {**job_on("alpha"), "id": "j_1"}
    ident = resolve_identity(job, BAKE)
    assert acting_for(ident, None) is ident and acting_for(ident, BAKE) is ident
    other = acting_for(ident, GUEST)
    assert other.principal.via == ("job:j_1", "human:u_guest") and other.id == "human:u_bake"
    assert acting_for(other, GUEST).principal.via == other.principal.via


# -- through the scheduler ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_guest_cannot_make_a_job_that_escalates(tmp_path):
    """Bake's example: a guest's job does what the guest may, and nothing more."""
    az = authorizer(tmp_path)
    store, sched, rt, clock = setup(tmp_path, az)
    mine = store.create_job(job_on("kids-pc", name="mine"), GUEST, clock(), "UTC")
    got = await run(store, sched, mine, clock)
    assert got["state"] == "success" and got["identity_used"] == "human:u_guest"
    sneaky = store.create_job(job_on("alpha", name="sneaky"), GUEST, clock(), "UTC")
    got = await run(store, sched, sneaky, clock)
    assert got["state"] != "success"
    assert [c[2] for c in rt.calls] == ["w_pc"]          # alpha was never reached
    assert rt.decisions[-1].rule == "access:no-grant"


@pytest.mark.asyncio
async def test_revoking_a_guest_pauses_their_jobs(tmp_path):
    az = authorizer(tmp_path)
    store, sched, rt, clock = setup(tmp_path, az)
    row = store.create_job(job_on("kids-pc", name="nightly"), GUEST, clock(), "UTC")
    assert (await run(store, sched, row, clock))["state"] == "success"
    az.access.revoke(az.access.grants("u_guest")[0]["id"], actor="test", words="guest left")
    got = await run(store, sched, row, clock)
    assert got["state"] == "blocked" and "no access" in got["error"]
    row = store.get_job(row["id"])
    assert row["enabled"] is False and row["paused_reason"] == "identity_revoked"
    assert len(rt.calls) == 1


@pytest.mark.asyncio
async def test_running_an_owners_job_lends_the_runner_nothing(tmp_path):
    az = authorizer(tmp_path)
    store, sched, rt, clock = setup(tmp_path, az)
    row = store.create_job(job_on("alpha", name="bakes"), BAKE, clock(), "UTC")
    # The guest runs it (run access "*"): it acts for the guest too.
    got = await run(store, sched, row, clock, run_by=GUEST)
    assert got["state"] != "success" and got["run_by"]["id"] == "human:u_guest"
    assert rt.decisions[-1].denied and "acting for human:u_guest" in rt.decisions[-1].reason
    assert not rt.calls
    # Bake's own run (or a trigger) does what Bake may.
    assert (await run(store, sched, row, clock))["state"] == "success"
    assert [c[2] for c in rt.calls] == ["w1"]


# -- tool steps are authorized ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_steps_pass_the_authorizer(tmp_path):
    az = authorizer(tmp_path)
    seen = []

    class Plugin:
        def mcp_tools(self, invoke):
            async def rook_task(**kw):
                return json.dumps(await invoke("task.read", kw))
            async def rook_inner(**kw):
                return await invoke("task.write", kw)
            return [rook_task, rook_inner]

    class Node:
        worker_id = "hub"
        host = type("H", (), {"plugins": [Plugin()]})()
        client = type("C", (), {"authz": az})()

        async def invoke(self, cap, args, identity):
            seen.append(cap)
            return {"ok": True}

        def entry(self):
            return {"name": "rook", "roles": ["is_hub"]}

    rt = Runtime(Node())
    job = {**job_on("alpha"), "id": "j_t"}
    guest = resolve_identity(job, GUEST)
    with pytest.raises(Blocked, match="no grant covers task.read"):
        await rt.tool("rook_task", {"action": "deck"}, guest)
    assert not seen
    # With a grant on the hub's read caps the tool runs; a write it makes inside is still refused.
    az.access.grant("u_guest", "hub", caps=["task.read"], tier="read", actor="test", words="see the deck")
    assert json.loads(await rt.tool("rook_task", {"action": "deck"}, guest)) == {"ok": True}
    with pytest.raises(Blocked, match="task.write"):
        await rt.tool("rook_inner", {}, guest)
    assert seen == ["task.read"]
    # The owner's runs are not limited by grants.
    assert await rt.tool("rook_inner", {}, resolve_identity(job, BAKE)) == {"ok": True}
