"""Jobs J2 (docs/design/jobs.md 7): run identities (creator, key, vault,
fallback) and revocation, the edit-resets-identity rule, access patterns, and
guardrails (the default deny list, inheritance, per-job allow/deny,
operator-only overrides, the hub policy, preview and set_guardrails)."""
import asyncio
import json

import pytest

from jobs_fakes import OWNER, Clock, FakeRuntime, drain, job_doc, make, ok, shell
from rook.band_mcp.tokens import TokenStore
from rook.band_mcp.vault import Vault
from rook.hub.authz import current_principal
from rook.hub.node import HubNode
from rook.hub.plugins.jobs.executor import Executor
from rook.hub.plugins.jobs.guardrails import (DEFAULT_GUARDRAILS, Blocked, Guardrails, JobGuard,
                                              check_defaults)
from rook.hub.plugins.jobs.identity import (IdentityRevoked, IdentityUnavailable, authorize, is_admin,
                                            resolve_identity)
from rook.hub.plugins.jobs.model import check
from rook.hub.plugins.jobs.principals import Directory
from rook.hub.plugins.jobs.runtime import Runtime
from rook.hub.policy import Policy, Principal
from rook.hub.settings_store import SettingsStore

OPERATOR = Principal("token:static", "token", "operator", label="static")
OP_INFO = {"id": "token:static", "kind": "token", "role": "operator", "groups": [], "label": "static"}


def info(p: Principal) -> dict:
    return {"id": p.id, "kind": p.kind, "role": p.role, "groups": list(p.groups), "label": p.label,
            "verified": p.verified}


class Accounts:
    def __init__(self, users):
        self.users = users

    def user(self, uid):
        return self.users.get(uid)


@pytest.fixture
def tokens(tmp_path):
    return TokenStore(persist_path=str(tmp_path / "tokens.json"), static_token="s" * 32)


def principal_of(entry) -> Principal:
    return Principal(f"token:{entry['agent_id']}", "token", entry.get("role", "agent"), label=entry["name"])


def jobdef(identity=None, **kw):
    job, _ = check(job_doc({"a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec"}},
                           **({"identity": identity} if identity else {}), **kw))
    job["id"] = "j_1"
    return job


# -- identity modes ---------------------------------------------------------------------

def test_creator_is_the_default_and_carries_the_job(tokens):
    alice = tokens.mint_api_token("alice")
    owner = info(principal_of(alice))
    ident = resolve_identity(jobdef(), owner, directory=Directory(tokens))
    assert ident.mode == "creator" and ident.id == owner["id"]
    assert ident.principal.via == ("job:j_1",) and ident.display == f"job:j_1/{owner['id']}"


def test_key_identity_by_name_id_and_principal(tokens):
    bot = tokens.mint_api_token("nightly-bot", role="operator")
    d = Directory(tokens)
    for ref in ("nightly-bot", bot["id"], bot["agent_id"], f"token:{bot['agent_id']}"):
        ident = resolve_identity(jobdef({"mode": "key", "ref": ref}), OWNER, directory=d)
        assert ident.mode == "key" and ident.id == f"token:{bot['agent_id']}"
        assert ident.principal.role == "operator" and ident.principal.label == "nightly-bot"


def test_key_identity_revoked_or_expired(tokens):
    bot = tokens.mint_api_token("bot")
    d = Directory(tokens)
    tokens.revoke_api_token(bot["id"])
    with pytest.raises(IdentityRevoked, match="revoked"):
        resolve_identity(jobdef({"mode": "key", "ref": f"token:{bot['agent_id']}"}), OWNER, directory=d)
    short = tokens.mint_api_token("short", ttl_seconds=60)
    d.clock = lambda: short["created_at"] + 3600
    with pytest.raises(IdentityRevoked, match="expired"):
        resolve_identity(jobdef({"mode": "key", "ref": "short"}), OWNER, directory=d)
    # No token store on this hub: a key identity cannot be checked (blocked, not paused).
    with pytest.raises(IdentityUnavailable):
        resolve_identity(jobdef({"mode": "key", "ref": "short"}), OWNER, directory=None)


def test_user_identity_and_deleted_user():
    d = Directory(accounts=Accounts({"u1": {"id": "u1", "username": "bake", "admin": 1}}))
    ident = resolve_identity(jobdef({"mode": "key", "ref": "human:u1"}), OWNER, directory=d)
    assert ident.id == "human:u1" and ident.principal.groups == ("human:owner",)
    d2 = Directory(accounts=Accounts({}))
    with pytest.raises(IdentityRevoked, match="no longer exists"):
        resolve_identity(jobdef({"mode": "key", "ref": "human:u1"}), OWNER, directory=d2)
    # A creator who was a since-deleted account is revoked too.
    with pytest.raises(IdentityRevoked):
        resolve_identity(jobdef(), {"id": "human:u1", "kind": "human", "role": "member"}, directory=d2)


def test_vault_identity_resolves_at_run_time_and_is_never_kept(tmp_path, tokens):
    vault = Vault(str(tmp_path / "vault.db"))
    bot = tokens.mint_api_token("vault-bot")
    vault.set("bot_key", bot["token"], "a job key", "test")
    d = Directory(tokens, vault=vault)
    for ref in ("bot_key", "{{secret:bot_key}}"):
        ident = resolve_identity(jobdef({"mode": "vault", "ref": ref}), OWNER, directory=d)
        assert ident.mode == "vault" and ident.id == f"token:{bot['agent_id']}"
        assert bot["token"] not in repr(ident) and bot["token"] not in json.dumps(ident.principal.__dict__)
    assert vault.access_log("bot_key")[0]["actor"] == "job:j_1"
    tokens.revoke_api_token(bot["id"])
    with pytest.raises(IdentityRevoked, match="revoked or expired"):
        resolve_identity(jobdef({"mode": "vault", "ref": "bot_key"}), OWNER, directory=d)
    with pytest.raises(IdentityRevoked, match="no longer exists"):
        resolve_identity(jobdef({"mode": "vault", "ref": "missing"}), OWNER, directory=d)
    with pytest.raises(IdentityUnavailable, match="vault is unavailable"):
        resolve_identity(jobdef({"mode": "vault", "ref": "bot_key"}), OWNER, directory=Directory(tokens))


def test_fallback_on_revocation_then_nothing_left(tokens):
    alice, backup = tokens.mint_api_token("alice"), tokens.mint_api_token("backup")
    owner = info(principal_of(alice))
    d = Directory(tokens)
    job = jobdef({"mode": "creator", "fallback": "backup"})
    assert resolve_identity(job, owner, directory=d).mode == "creator"
    tokens.revoke_api_token(alice["id"])
    ident = resolve_identity(job, owner, directory=d)
    assert ident.mode == "fallback" and ident.id == f"token:{backup['agent_id']}" and "fallback" in ident.note
    tokens.revoke_api_token(backup["id"])
    with pytest.raises(IdentityRevoked, match="fallback"):
        resolve_identity(job, owner, directory=d)
    # The hub default fallback applies when the job names none.
    keeper = tokens.mint_api_token("keeper")
    ident = resolve_identity(jobdef(), owner, directory=d,
                             settings=lambda k: "keeper" if k == "default_fallback" else None)
    assert ident.mode == "fallback" and ident.id == f"token:{keeper['agent_id']}"
    # mode fallback: always the fallback identity.
    ident = resolve_identity(jobdef({"mode": "fallback", "fallback": {"mode": "key", "ref": "keeper"}}),
                             OWNER, directory=d)
    assert ident.mode == "fallback" and ident.id == f"token:{keeper['agent_id']}"
    with pytest.raises(IdentityUnavailable, match="needs identity.fallback"):
        resolve_identity(jobdef({"mode": "fallback"}), OWNER, directory=d)


def test_who_may_set_an_identity(tmp_path, tokens):
    alice, bob = tokens.mint_api_token("alice"), tokens.mint_api_token("bob")
    a, b = info(principal_of(alice)), info(principal_of(bob))
    d = Directory(tokens, vault=Vault(str(tmp_path / "v.db")))
    authorize({"mode": "key", "ref": "alice"}, a, d)                     # it is them
    with pytest.raises(PermissionError, match="only the operator"):
        authorize({"mode": "key", "ref": "alice"}, b, d)                 # someone else's key
    with pytest.raises(PermissionError):
        authorize({"mode": "creator", "fallback": "alice"}, b, d)        # ... as a fallback too
    authorize({"mode": "key", "ref": "alice"}, OP_INFO, d)               # the operator may
    d.vault.set("akey", alice["token"], "", "t")
    authorize({"mode": "vault", "ref": "akey"}, a, d)
    with pytest.raises(PermissionError, match="vault secret 'akey'"):
        authorize({"mode": "vault", "ref": "akey"}, b, d)
    # Unchanged identities are not re-checked (an owner edit keeps the operator's choice).
    authorize({"mode": "key", "ref": "alice"}, b, d, previous={"mode": "key", "ref": "alice"})
    assert is_admin(OP_INFO) and not is_admin(b)


@pytest.mark.asyncio
async def test_scheduler_pauses_a_job_whose_identity_was_revoked(tmp_path, tokens):
    alice = tokens.mint_api_token("alice")
    clock = Clock()
    rt = FakeRuntime(tmp_path, clock=clock, caps={"shell.exec": shell()})
    store, sched, rt, _ = make(tmp_path, runtime=rt, clock=clock)
    job, _ = check(job_doc({"a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec"}}, name="nightly"))
    row = store.create_job(job, info(principal_of(alice)), clock(), "UTC")
    sched.resolve = lambda j, o: resolve_identity(j, o, directory=Directory(tokens))
    tokens.revoke_api_token(alice["id"])
    run = store.enqueue(row, "manual", clock())
    await sched.tick()
    await drain(sched)
    assert store.get_run(run["id"])["state"] == "blocked" and "revoked" in store.get_run(run["id"])["error"]
    row = store.get_job(row["id"])
    assert row["enabled"] is False and row["paused_reason"] == "identity_revoked"
    assert store.history(row["id"])[0]["action"] == "disable" and not rt.calls


@pytest.mark.asyncio
async def test_scheduler_switches_to_the_fallback(tmp_path, tokens):
    alice, backup = tokens.mint_api_token("alice"), tokens.mint_api_token("backup")
    clock = Clock()
    rt = FakeRuntime(tmp_path, clock=clock, caps={"shell.exec": shell()})
    store, sched, rt, _ = make(tmp_path, runtime=rt, clock=clock)
    job, _ = check(job_doc({"a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec"}},
                           identity={"mode": "creator", "fallback": f"token:{backup['agent_id']}"}))
    row = store.create_job(job, info(principal_of(alice)), clock(), "UTC")
    sched.resolve = lambda j, o: resolve_identity(j, o, directory=Directory(tokens))
    tokens.revoke_api_token(alice["id"])
    run = store.enqueue(row, "manual", clock())
    await sched.tick()
    await drain(sched)
    got = store.get_run(run["id"])
    assert got["state"] == "success" and got["identity_used"] == f"token:{backup['agent_id']} (fallback)"
    assert rt.calls[0][3] == f"job:{row['id']}/token:{backup['agent_id']}"
    assert store.get_job(row["id"])["enabled"] is True


# -- the guardrail engine ------------------------------------------------------------------

def engine(setting=None, **kw):
    return Guardrails(lambda k: setting if k == "guardrails" else None, **kw)


def job_with(**g):
    return {"id": "j_g", "name": "g", "guardrails": {"inherit": True, "allow": [], "deny": [], **g}}


@pytest.mark.parametrize("cap,args,blocked", [
    ("worker.update", None, True),          # worker updates
    ("worker.deauth", None, True),          # deauth
    ("worker.reconfigure", None, True),     # re-band
    ("worker.enrollment_finish", None, True),
    ("selfupdate.apply", None, True),       # selfupdate.*
    ("worker.restart", None, True),
    ("policy.set", None, True),             # policy edits
    ("settings.set", None, True),
    ("job.write", {"action": "set_guardrails"}, True),   # guardrail edits
    ("job.write", {"action": "delete"}, True),            # permanent deletes
    ("secret.delete", None, True),
    ("chat.delete", None, True),
    ("secret.get", None, True),             # admin tier
    ("persona.assign", None, True),         # admin by declared risk (hub registry)
    ("shell.exec", None, False),            # exec is allowed
    ("file.write", None, False),
    ("cmd.backup", None, False),
    ("secret.set", None, False),            # vault writes are allowed
    ("job.write", {"action": "create"}, False),
    ("hub.info", None, False),
    ("task.write", {"action": "update"}, False),
])
def test_default_deny_list(cap, args, blocked):
    g = engine(hub_tier=lambda c: "admin" if c == "persona.assign" else None)
    v = g.check_call(job_with(), cap, "rook" if cap.split(".")[0] in ("job", "secret", "persona") else "w1",
                     None, args)
    assert (not v.allow) == blocked, v
    if blocked:
        assert v.rule.startswith("default-deny:") and "blocked by job guardrail" in v.reason


def test_declared_tier_from_the_roster_counts():
    roster = {"w1": {"name": "alpha", "caps": ["fleet.wipe"], "tiers": {"fleet.wipe": "a"}}}
    g = engine(roster=lambda: roster)
    assert not g.check_call(job_with(), "fleet.wipe", "w1", roster["w1"]).allow
    assert g.check_call(job_with(), "fleet.look", "w1", roster["w1"]).allow


def test_default_list_is_valid_and_configurable():
    assert check_defaults(DEFAULT_GUARDRAILS) == []
    assert check_defaults({"deny": ["tier:nope"]})
    custom = {"deny": ["shell.exec"], "allow": []}
    g = engine(custom)
    assert not g.check_call(job_with(), "shell.exec", "w1").allow
    assert g.check_call(job_with(), "worker.update", "w1").allow          # no longer in the list
    # A broken setting never fails open: the built-in list applies.
    assert not engine({"deny": "nope"}).check_call(job_with(), "worker.update", "w1").allow


def test_inherit_follows_the_defaults_and_base_does_not():
    setting = {"deny": ["shell.exec"], "allow": []}
    g = Guardrails(lambda k: setting if k == "guardrails" else None)
    inherits = job_with()
    pinned = job_with(inherit=False, base={"deny": ["worker.update"], "allow": []})
    assert not g.check_call(inherits, "shell.exec", "w1").allow
    assert g.check_call(pinned, "shell.exec", "w1").allow
    setting["deny"] = ["file.write"]
    assert g.check_call(inherits, "shell.exec", "w1").allow and not g.check_call(inherits, "file.write", "w1").allow
    assert not g.check_call(pinned, "worker.update", "w1").allow and g.check_call(pinned, "file.write", "w1").allow


def test_per_job_deny_and_allow_layer_on_top():
    g = engine()
    assert not g.check_call(job_with(deny=["shell.*"]), "shell.exec", "w1").allow
    v = g.check_call(job_with(deny=[{"cap": "shell.exec", "on": "beta"}]), "shell.exec", "w2", {"name": "beta"})
    assert not v.allow and v.rule.startswith("job-deny:")
    assert g.check_call(job_with(deny=[{"cap": "shell.exec", "on": "beta"}]), "shell.exec", "w1",
                        {"name": "alpha"}).allow
    # An allow beats a default deny; a job deny beats the job's own allow.
    assert g.check_call(job_with(allow=["worker.update"]), "worker.update", "w1").allow
    assert not g.check_call(job_with(allow=["worker.update"], deny=["worker.*"]), "worker.update", "w1").allow
    assert g.check_call(job_with(allow=["job.write:delete"]), "job.write", "rook", None, {"action": "delete"}).allow


def test_hub_policy_rules_naming_jobs_apply():
    pol = Policy({"version": 1, "mode": "enforce", "defaults": {"read": "allow", "write": "allow",
                                                               "exec": "deny", "admin": "deny"},
                  "rules": [{"id": "no-jobs-on-nas", "who": "job:*", "deny": "*", "on": "nas"}]})
    g = engine(hub_policy=lambda: pol)
    v = g.check_call(job_with(), "shell.exec", "w9", {"name": "nas"})
    assert not v.allow and v.rule == "policy:no-jobs-on-nas"
    # Tier defaults of the hub policy are not job guardrails: exec stays allowed.
    assert g.check_call(job_with(), "shell.exec", "w1", {"name": "alpha"}).allow


def test_static_scan_of_steps():
    roster = {"w1": {"name": "alpha", "caps": ["shell.exec", "worker.update"]},
              "w2": {"name": "beta", "caps": ["shell.exec", "worker.update"]},
              "hub": {"name": "rook", "caps": ["job.write"]}}
    g = engine(roster=lambda: roster, hub_id=lambda: "hub")
    job = {"id": "j", "guardrails": {"inherit": True, "allow": [], "deny": []}, "steps": {
        "a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec"},
        "b": {"kind": "fanout", "cap": "worker.update", "filter": {}},
        "c": {"kind": "tool", "tool": "rook_jobs", "args": {"action": "delete", "id": "x"}},
        "d": {"kind": "cap", "worker": "offline-box", "cap": "selfupdate.go"},
        "e": {"kind": "notify", "text": "hi"}}}
    blocks = g.scan(job)
    assert sorted((b["step"], b["target"]) for b in blocks) == [
        ("b", "alpha"), ("b", "beta"), ("c", "rook"), ("d", "offline-box")]
    assert {b["cap"] for b in blocks if b["step"] == "c"} == {"job.write"}
    assert g.warnings(job)[0].startswith("steps.b: worker.update on alpha would be blocked")


# -- execution -------------------------------------------------------------------------------

def guarded_identity(job, g=None):
    return resolve_identity(job, OWNER, guardrails=g or engine())


@pytest.mark.asyncio
async def test_a_blocked_cap_step_is_never_called_or_retried(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"worker.update": lambda a, w: ok()})
    job, _ = check(job_doc({"a": {"kind": "cap", "worker": "alpha", "cap": "worker.update",
                                  "retry": {"max": 3}, "on": {"failure": ["b"]}}, "b": {"kind": "noop"}}))
    job["id"] = "j_b"
    res = await Executor(rt).execute(job, {"id": "r", "vars": {}}, guarded_identity(job))
    assert res["state"] == "blocked" and res["steps"]["a"]["rule"] == "default-deny:worker.update"
    assert res["steps"]["a"]["attempts"] == 0 and not rt.calls and "b" not in res["steps"]


@pytest.mark.asyncio
async def test_calls_are_checked_with_the_resolved_worker(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": shell()})
    job, _ = check(job_doc({"a": {"kind": "fanout", "cap": "shell.exec", "filter": {}, "join": "any"}},
                           guardrails={"deny": [{"cap": "shell.exec", "on": "beta"}]}))
    job["id"] = "j_f"
    res = await Executor(rt).execute(job, {"id": "r", "vars": {}}, guarded_identity(job))
    per = res["steps"]["a"]["output"]
    assert per["alpha"]["state"] == "success" and per["beta"]["state"] == "blocked"
    assert [c[2] for c in rt.calls] == ["w1"] and res["state"] == "success"
    # Every worker refused: the step is blocked.
    job["guardrails"]["deny"] = ["shell.exec"]
    res = await Executor(rt).execute(job, {"id": "r2", "vars": {}}, guarded_identity(job))
    assert res["state"] == "blocked"
    # A worker picked at run time ({"any_with_cap": true}) is checked when it is picked.
    rt.calls.clear()
    job2, _ = check(job_doc({"a": {"kind": "cap", "worker": {"any_with_cap": True}, "cap": "shell.exec"}},
                            guardrails={"deny": [{"cap": "shell.exec", "on": "alpha"}]}))
    job2["id"] = "j_any"
    res = await Executor(rt).execute(job2, {"id": "r3", "vars": {}}, guarded_identity(job2))
    assert res["state"] == "blocked" and not rt.calls
    assert rt.node.journal.rows[-1]["reply"]["denied"]["guardrail"].startswith("job-deny:")


@pytest.mark.asyncio
async def test_hub_tool_calls_are_checked_per_cap(tmp_path):
    seen = []

    class Plugin:
        @staticmethod
        def mcp_tools(invoke):
            async def rook_sneaky(**kw):
                try:
                    return await invoke("worker.update", {})
                except PermissionError as e:  # a tool that swallows errors
                    return json.dumps({"ok": False, "error": str(e)})

            async def rook_fine(**kw):
                return await invoke("hub.info", {})
            return [rook_sneaky, rook_fine]

    class Node:
        worker_id = "hub"
        host = type("H", (), {"plugins": [Plugin()]})()

        async def invoke(self, cap, args, identity):
            seen.append(cap)
            return {"ok": True}

        def entry(self):
            return {"name": "rook", "caps": ["hub.info"]}

    rt = Runtime(Node())
    job = {"id": "j_t", "guardrails": {"inherit": True, "allow": [], "deny": []}}
    ident = guarded_identity(job)
    with pytest.raises(Blocked, match="worker.update"):
        await rt.tool("rook_sneaky", {}, ident)
    assert await rt.tool("rook_fine", {}, ident) == {"ok": True} and seen == ["hub.info"]


# -- the service, through a real hub node ------------------------------------------------------

def node(tmp_path, tokens=None, **kw):
    n = HubNode(str(tmp_path), entry_points=False, build_version="1.test.node",
                settings_store=SettingsStore(tmp_path / "settings.db"), **kw)
    n.tokens = tokens
    return n


def as_(n, principal):
    async def invoke(cap, args):
        tok = current_principal.set(principal)
        try:
            return await n.invoke(cap, args, principal.id)
        finally:
            current_principal.reset(tok)
    tools = {t.__name__: t for t in n.plugin("job").mcp_tools(invoke)}

    async def call(**kw):
        return json.loads(await tools["rook_jobs"](**kw))
    return call


SAFE = {"name": "safe", "entry": "a", "steps": {"a": {"kind": "cap", "worker": "rook", "cap": "hub.info"}}}


async def run_now(n, call, jid):
    started = await call(action="run", id=jid)
    assert started["ok"], started
    p = n.plugin("job")
    await p.scheduler.tick()
    while p.scheduler.active:
        await asyncio.gather(*list(p.scheduler.active.values()))
    return (await call(action="run_get", id=started["result"]["id"]))["result"]


@pytest.mark.asyncio
async def test_edit_resets_identity_unless_owner_or_operator(tmp_path, tokens):
    alice, bob = tokens.mint_api_token("alice"), tokens.mint_api_token("bob")
    A, B = principal_of(alice), principal_of(bob)
    n = node(tmp_path, tokens)
    a, b, op = as_(n, A), as_(n, B), as_(n, OPERATOR)
    made = await a(action="create", data={**SAFE, "identity": {"mode": "key", "ref": "alice"}})
    assert made["ok"], made
    jid = made["result"]["id"]
    # Bob may not create a job that runs as Alice's key.
    denied = await b(action="create", data={**SAFE, "name": "x", "identity": {"mode": "key", "ref": "alice"}})
    assert denied["code"] == "PermissionError" and "only the operator" in denied["error"]
    # The owner edits: nothing changes hands.
    same = (await a(action="update", id=jid, data={"description": "mine"}))["result"]
    assert same["owner"] == A.id and same["definition"]["identity"]["mode"] == "key"
    # The operator edits: the owner and identity stay.
    kept = (await op(action="update", id=jid, data={"description": "ops"}))["result"]
    assert kept["owner"] == A.id and kept["definition"]["identity"] == {"mode": "key", "ref": "alice"}
    # Bob edits (access edit "*"): the job becomes Bob's and runs as Bob.
    took = (await b(action="update", id=jid, data={"description": "bob's now"}))["result"]
    assert took["owner"] == B.id and took["definition"]["identity"] == {"mode": "creator"}
    assert took["identity_reset"] == {"from": A.id, "to": B.id}
    assert took["history"][0]["detail"]["identity_reset"] == {"from": A.id, "to": B.id}
    run = await run_now(n, b, jid)
    assert run["state"] == "success" and run["identity_used"] == B.id
    # Enabling someone else's job is an edit too.
    await b(action="disable", id=jid)
    back = (await a(action="enable", id=jid))["result"]
    assert back["owner"] == A.id and back["identity_reset"]["to"] == A.id


@pytest.mark.asyncio
async def test_access_patterns(tmp_path, tokens):
    alice, bob = tokens.mint_api_token("alice"), tokens.mint_api_token("bob")
    A, B = principal_of(alice), principal_of(bob)
    n = node(tmp_path, tokens)
    a, b, op = as_(n, A), as_(n, B), as_(n, OPERATOR)
    private = {**SAFE, "name": "private", "access": {"read": A.id, "edit": A.id, "run": A.id}}
    jid = (await a(action="create", data=private))["result"]["id"]
    shared = (await a(action="create", data={**SAFE, "name": "shared",
                                             "access": {"read": "token:*", "edit": A.id, "run": [B.id]}}))["result"]
    assert [j["name"] for j in (await b())["result"]["jobs"]] == ["shared"]
    assert {j["name"] for j in (await op())["result"]["jobs"]} == {"private", "shared"}
    for action in ("get", "runs", "next", "update", "run", "delete", "enable", "disable"):
        out = await b(action=action, id=jid, data={"description": "x"} if action == "update" else None)
        assert out["code"] == "PermissionError", (action, out)
    run = await run_now(n, a, jid)
    assert (await b(action="run_get", id=run["id"]))["code"] == "PermissionError"
    assert (await op(action="run_get", id=run["id"]))["ok"]
    assert [r["job"] for r in (await b(action="runs"))["result"]["runs"]] == []
    assert len((await a(action="runs"))["result"]["runs"]) == 1
    # Shared: Bob reads and runs but cannot edit (or change access).
    assert (await b(action="get", id="shared"))["result"]["can"] == {"edit": False, "run": True}
    assert (await b(action="run", id="shared"))["ok"]
    assert (await b(action="update", id="shared", data={"access": {"edit": "*"}}))["code"] == "PermissionError"
    # The operator always may.
    assert (await op(action="update", id=jid, data={"description": "ops"}))["ok"]
    # job.default_access for new jobs.
    assert (await op(action="settings", data={"default_access": {"read": "role:operator", "edit": "role:operator",
                                                                  "run": "role:operator"}}))["ok"]
    mine = (await a(action="create", data={**SAFE, "name": "later"}))["result"]
    assert mine["definition"]["access"]["read"] == "role:operator"
    assert (await b(action="get", id="later"))["code"] == "PermissionError"
    assert (await a(action="get", id="later"))["ok"]            # the owner always may
    bad = await op(action="settings", data={"default_access": {"read": 5}})
    assert bad["code"] == "ValidationError"


@pytest.mark.asyncio
async def test_guardrails_end_to_end(tmp_path, tokens):
    alice = tokens.mint_api_token("alice")
    A = principal_of(alice)
    n = node(tmp_path, tokens)
    a, op = as_(n, A), as_(n, OPERATOR)
    deleter = {"name": "deleter", "entry": "a", "steps": {
        "a": {"kind": "tool", "tool": "rook_jobs", "args": {"action": "delete", "id": "safe"}}}}
    # Saving a job whose step is blocked now: a warning, not an error.
    made = await a(action="create", data=deleter)
    assert made["ok"] and any("would be blocked (default-deny:job.write:delete)" in w
                              for w in made["result"]["warnings"])
    assert made["result"]["blocked_by_guardrail"] is True
    v = await a(action="validate", data={**deleter, "name": "d2"})
    assert v["result"]["valid"] and v["result"]["warnings"]
    assert (await a(action="create", data=SAFE))["ok"]
    run = await run_now(n, a, "deleter")
    assert run["state"] == "blocked" and run["steps"]["a"]["rule"] == "default-deny:job.write:delete"
    assert (await a(action="get", id="safe"))["ok"]              # nothing was deleted
    # Only the operator may set a per-job allow.
    no = await a(action="set_guardrails", id="deleter", data={"guardrails": {"allow": ["job.write:delete"]}})
    assert no["code"] == "PermissionError" and "only the operator" in no["error"]
    yes = await op(action="set_guardrails", id="deleter", data={"guardrails": {"allow": ["job.write:delete"]}})
    assert yes["ok"] and yes["result"]["blocked_by_guardrail"] is False
    assert yes["result"]["owner"] == A.id                         # the operator does not take it over
    # The owner may edit other fields; the operator's allow stays.
    kept = (await a(action="update", id="deleter", data={"description": "ok"}))["result"]
    assert kept["definition"]["guardrails"]["allow"] == ["job.write:delete"]
    # Someone else editing takes it over and drops the allow.
    bob = principal_of(tokens.mint_api_token("bob"))
    took = (await as_(n, bob)(action="update", id="deleter", data={"description": "mine"}))["result"]
    assert took["definition"]["guardrails"]["allow"] == [] and took["blocked_by_guardrail"] is True
    # Per-job deny: anyone with edit access.
    d = (await a(action="set_guardrails", id="safe", data={"guardrails": {"deny": ["hub.*"]}}))["result"]
    assert d["blocked_by_guardrail"] and d["guardrail_blocks"][0]["rule"] == "job-deny:hub.*"
    assert (await a(action="run_get", id=(await run_now(n, a, "safe"))["id"]))["result"]["state"] == "blocked"
    # Stop inheriting: the defaults are copied (base) and later default changes skip the job.
    pinned = (await a(action="set_guardrails", id="safe",
                      data={"guardrails": {"deny": [], "inherit": False, "base": {"deny": []}}}))["result"]
    base = pinned["definition"]["guardrails"]["base"]
    assert base["deny"] == DEFAULT_GUARDRAILS["deny"]              # a non-operator cannot choose the base


@pytest.mark.asyncio
async def test_preview_and_set_default_guardrails(tmp_path, tokens):
    alice = tokens.mint_api_token("alice")
    A = principal_of(alice)
    n = node(tmp_path, tokens)
    a, op = as_(n, A), as_(n, OPERATOR)
    assert (await a(action="create", data=SAFE))["ok"]
    assert (await a(action="create", data={**SAFE, "name": "pinned",
                                          "guardrails": {"inherit": False}}))["ok"]
    proposed = {"deny": DEFAULT_GUARDRAILS["deny"] + ["hub.info"], "allow": ["secret.set"]}
    pv = (await a(action="guardrails_preview", data={"defaults": proposed}))["result"]
    assert pv["scope"] == "defaults" and pv["checked"] == 2
    assert [(b["job"], b["step"], b["cap"], b["rule"]) for b in pv["newly_blocked"]] == [
        ("safe", "a", "hub.info", "default-deny:hub.info")]
    assert pv["jobs_newly_blocked"] == [{"id": pv["newly_blocked"][0]["job_id"], "name": "safe",
                                         "enabled": True, "steps": ["a"]}]
    assert (await a(action="list", data={"blocked": True}))["result"]["jobs"] == []
    # Saving defaults is the operator's; the reply carries the preview.
    assert (await a(action="set_guardrails", data={"defaults": proposed}))["code"] == "PermissionError"
    bad = await op(action="set_guardrails", data={"defaults": {"deny": ["tier:bogus"]}})
    assert bad["code"] == "ValidationError" and bad["errors"]
    saved = (await op(action="set_guardrails", data={"defaults": proposed}))["result"]
    assert saved["defaults"]["deny"][-1] == "hub.info" and saved["preview"]["newly_blocked"]
    flagged = (await a(action="list", data={"blocked": True}))["result"]["jobs"]
    assert [j["name"] for j in flagged] == ["safe"] and flagged[0]["guardrail_blocks"][0]["cap"] == "hub.info"
    hist = (await a(action="get", id="safe"))["result"]["history"]
    assert hist[0]["action"] == "guardrail_blocked" and hist[0]["detail"]["steps"] == ["a"]
    run = await run_now(n, a, "safe")
    assert run["state"] == "blocked"
    # Reverting shows the job unblocked; the pinned job was never affected.
    back = (await op(action="guardrails_preview", data={"reset": True}))["result"]
    assert [b["job"] for b in back["unblocked"]] == ["safe"] and back["newly_blocked"] == []
    assert (await op(action="set_guardrails", data={"reset": True}))["ok"]
    assert (await op(action="settings"))["result"]["settings"]["guardrails"] == DEFAULT_GUARDRAILS
    # Per-job preview.
    pj = (await a(action="guardrails_preview", id="safe", data={"guardrails": {"deny": ["hub.info"]}}))["result"]
    assert pj["scope"] == "job" and pj["newly_blocked"][0]["rule"] == "job-deny:hub.info"
    assert (await a(action="guardrails_preview", id="safe", data={}))["code"] == "ValidationError"
