"""Jobs that end themselves and jobs only a person may change
(docs/design/jobs.md 4.1, 4.2, 7.1).

* limits: a step's ``complete``, ``complete_on``, ``max_runs``, ``until``
  and ``max_failures`` (the job's own and the hub default) switch a job off,
  cancel its waiting runs and say why in its history;
* an agent step's ``finish(complete=true)`` ends the job, except on a
  locked job whose step does not say ``complete: "agent"``;
* the same message to the same target goes out once per
  ``limits.notify_window``, on every path (notify, ask, alerts, cap steps,
  fanout, the agent's notify_bake / rook_call / rook_tool);
* locks: only a person (dashboard sign-in or the ``rook band`` panel) may
  lock, unlock or change a locked job; agents, the static/operator token and
  job runs are refused with a ``locked:`` error, over MCP, the web API and
  the panel.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from rook.cli import band_tui as tui
from rook.hub.authz import current_principal
from rook.hub.node import HubNode
from rook.hub.plugins.jobs import web as jobs_web
from rook.hub.plugins.jobs.executor import Executor
from rook.hub.plugins.jobs.identity import resolve_identity
from rook.hub.plugins.jobs.model import ValidationError, check, schema
from rook.hub.plugins.jobs.notices import Notices, normalize
from rook.hub.policy import Principal
from rook.hub.settings_store import SettingsStore
from tests.jobs_fakes import OWNER, T0, Clock, drain, job_doc, make, ok
from tests.test_jobs_agents import agent_step, call, env, finish, runtime  # noqa: F401 - env is a fixture

AGENT = Principal("token:agent_j", "token", "agent", label="jobs-test")
STATIC = Principal("token:static", "token", "operator", label="static")
PERSON = Principal("human:u1", "human", "owner", ("human:owner",), label="Pat")
NOTIFY = {"kind": "notify", "via": "notify", "worker": "alpha", "text": "Deploy finished after 3 tries"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("ROOK_JOBS", "ROOK_JOBS_DB", "ROOK_HUB_BAND_MAX_RISK", "ROOK_JOB_TIMEZONE"):
        monkeypatch.delenv(k, raising=False)


def setup(tmp_path, steps, *, settings=None, caps=None, **kw):
    """(store, scheduler, runtime, row) with the dedupe wired in."""
    clock = Clock()
    store, sched, rt, row = make(tmp_path, steps, clock=clock, **kw)
    sched.settings = settings or (lambda k: None)
    sched.executor.settings = sched.settings
    sched.executor.notices = Notices(store, sched.settings, clock)
    rt.caps.update(caps or {"notify.post": lambda a, w: ok({"posted": True}),
                            "voice.speak": lambda a, w: ok({"spoken": True}),
                            "shell.exec": lambda a, w: ok({"exit_code": 0, "stdout": "fine"})})
    return store, sched, rt, row


async def run_now(store, sched, row):
    run = store.enqueue(store.get_job(row["id"]), "manual", sched.clock())
    await sched.tick()
    await drain(sched)
    return store.get_run(run["id"])


def sends(rt, cap="notify.post"):
    return [c for c in rt.calls if c[0] == cap]


# -- validation and schema -------------------------------------------------------------------

@pytest.mark.parametrize("doc,needle", [
    ({"limits": {"max_runs": 0}}, "limits.max_runs"),
    ({"limits": {"max_failures": -1}}, "limits.max_failures"),
    ({"limits": {"until": "tomorrow"}}, "limits.until"),
    ({"limits": {"notify_window": "forever"}}, "limits.notify_window"),
    ({"limits": {"complete_on": "maybe"}}, "limits.complete_on"),
    ({"limits": {"forever": True}}, "limits.forever: unknown field"),
    ({"limits": "soon"}, "limits: an object"),
    ({"steps": {"a": {"kind": "noop", "complete": "sometimes"}}}, "steps.a.complete"),
    ({"steps": {"a": {"kind": "noop", "complete": "agent"}}}, '"agent" is for agent steps'),
    ({"locked": True}, "locked: unknown field"),
])
def test_limits_validation(doc, needle):
    base = job_doc({"a": {"kind": "noop"}})
    with pytest.raises(ValidationError) as e:
        check({**base, **doc})
    assert any(needle in x for x in e.value.errors), e.value.errors


def test_limits_defaults_and_schema():
    job, warns = check(job_doc({"a": {"kind": "noop", "complete": True}},
                               limits={"until": "2020-01-01T00:00:00+00:00"}), now=T0)
    assert job["limits"] == {"max_runs": None, "until": "2020-01-01T00:00:00+00:00", "max_failures": None,
                             "notify_window": None, "complete_on": None}
    assert job["steps"]["a"]["complete"] == "success"
    assert any("limits.until: in the past" in w for w in warns)
    s = schema()
    assert set(s["properties"]["limits"]["properties"]) == {"max_runs", "until", "max_failures",
                                                            "notify_window", "complete_on"}
    assert "agent" in s["$defs"]["step"]["properties"]["complete"]["enum"]


# -- limits ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_step_that_completes_switches_the_job_off(tmp_path):
    store, sched, rt, row = setup(tmp_path, {
        "check": {"kind": "cap", "worker": "alpha", "cap": "shell.exec", "on": {"success": ["tell"]}},
        "tell": {**NOTIFY, "complete": "success"}}, triggers=[{"kind": "cron", "expr": "*/10 * * * *"}],
        overlap={"mode": "queue"})
    waiting = store.enqueue(store.get_job(row["id"]), "manual", sched.clock())
    store.enqueue(store.get_job(row["id"]), "manual", sched.clock())   # queued behind the first
    await sched.tick()
    await drain(sched)
    first = store.get_run(waiting["id"])
    assert first["state"] == "success" and first["steps"]["tell"]["complete"] == "step complete: success"
    job = store.get_job(row["id"])
    assert job["enabled"] is False and job["paused_reason"] == "completed"
    h = store.history(row["id"])[0]
    assert h["action"] == "disable" and h["actor"] == "system:jobs"
    assert h["detail"]["limit"] == "completed" and h["detail"]["step"] == "tell"
    left = [r for r in store.runs(row["id"]) if r["id"] != waiting["id"]]
    assert [r["state"] for r in left] == ["cancelled"] and "switched off" in left[0]["error"]
    assert len(sends(rt)) == 1


@pytest.mark.asyncio
async def test_complete_needs_the_outcome_it_names(tmp_path):
    store, sched, rt, row = setup(tmp_path, {
        "check": {"kind": "cap", "worker": "alpha", "cap": "shell.exec", "complete": "success"}},
        caps={"shell.exec": lambda a, w: ok({"exit_code": 1})})
    assert (await run_now(store, sched, row))["state"] == "failure"
    assert store.get_job(row["id"])["enabled"] is True        # not yet: the step failed


@pytest.mark.asyncio
async def test_complete_on_and_max_runs(tmp_path):
    store, sched, rt, row = setup(tmp_path / "a", {"a": {"kind": "noop"}}, limits={"complete_on": "success"})
    await run_now(store, sched, row)
    assert store.get_job(row["id"])["paused_reason"] == "completed"

    store, sched, rt, row = setup(tmp_path / "b", {"a": {"kind": "noop"}}, limits={"max_runs": 2})
    await run_now(store, sched, row)
    assert store.get_job(row["id"])["enabled"] is True
    await run_now(store, sched, row)
    job = store.get_job(row["id"])
    assert job["enabled"] is False and job["paused_reason"] == "max_runs" and job["run_count"] == 2
    # Enabling it again starts the count over.
    store.set_enabled(row["id"], True, "human:u1", sched.clock(), "UTC")
    assert store.get_job(row["id"])["run_count"] == 0
    await run_now(store, sched, row)
    assert store.get_job(row["id"])["enabled"] is True


@pytest.mark.asyncio
async def test_max_failures_own_and_hub_default(tmp_path):
    fail = {"shell.exec": lambda a, w: ok({"exit_code": 2})}
    step = {"a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec"}}
    store, sched, rt, row = setup(tmp_path / "own", step, caps=fail, limits={"max_failures": 3})
    for _ in range(2):
        await run_now(store, sched, row)
    assert store.get_job(row["id"])["enabled"] is True and store.get_job(row["id"])["fail_streak"] == 2
    await run_now(store, sched, row)
    job = store.get_job(row["id"])
    assert job["enabled"] is False and job["paused_reason"] == "max_failures"
    assert store.history(row["id"])[0]["detail"]["failed_in_a_row"] == 3

    hub = {"max_failures": 2}.get
    store, sched, rt, row = setup(tmp_path / "hub", step, caps=fail, settings=hub)
    await run_now(store, sched, row)
    rt.caps["shell.exec"] = lambda a, w: ok({"exit_code": 0})
    await run_now(store, sched, row)                         # a success ends the streak
    rt.caps.update(fail)
    await run_now(store, sched, row)
    assert store.get_job(row["id"])["enabled"] is True
    await run_now(store, sched, row)
    assert store.get_job(row["id"])["paused_reason"] == "max_failures"

    # A locked job ignores the hub default (a person locked it to keep it on)...
    store, sched, rt, row = setup(tmp_path / "locked", step, caps=fail, settings=hub)
    store.set_locked(row["id"], True, "human:u1", sched.clock())
    for _ in range(4):
        await run_now(store, sched, row)
    assert store.get_job(row["id"])["enabled"] is True
    # ...but not its own limit.
    store, sched, rt, row = setup(tmp_path / "locked-own", step, caps=fail, settings=hub,
                                  limits={"max_failures": 1})
    store.set_locked(row["id"], True, "human:u1", sched.clock())
    await run_now(store, sched, row)
    assert store.get_job(row["id"])["paused_reason"] == "max_failures"


@pytest.mark.asyncio
async def test_until_expires_the_job(tmp_path):
    clock = Clock()
    store, sched, rt, row = make(tmp_path, {"a": {"kind": "noop"}}, clock=clock,
                                 triggers=[{"kind": "cron", "expr": "* * * * *"}],
                                 limits={"until": "2026-10-10T12:30:00+00:00"})
    run = store.enqueue(store.get_job(row["id"]), "manual", clock())
    clock.advance(3600)                                      # past until
    store.claim("hubA", clock(), 60)
    res = await sched.execute(store.get_run(run["id"]))
    assert res["state"] == "cancelled" and "expired" in store.get_run(run["id"])["error"]
    await sched.tick()
    job = store.get_job(row["id"])
    assert job["enabled"] is False and job["paused_reason"] == "expired"
    assert [r["state"] for r in store.runs(row["id"])] == ["cancelled"]   # nothing new fired


@pytest.mark.asyncio
async def test_service_refuses_to_run_or_enable_an_expired_job(tmp_path):
    n = node(tmp_path)
    jobs = tool_for(n, AGENT)
    doc = {**job_doc({"a": {"kind": "noop"}}, name="once"), "limits": {"until": "2026-01-01T00:00:00+00:00"}}
    made = await jobs(action="create", data=doc)
    assert made["ok"] and any("in the past" in w for w in made["result"]["warnings"])
    assert "expired" in (await jobs(action="run", id="once"))["error"]
    await jobs(action="disable", id="once")
    assert "expired" in (await jobs(action="enable", id="once"))["error"]


# -- notify dedupe --------------------------------------------------------------------------------

def test_texts_compare_loosely():
    assert normalize("Failed 68 times!") == normalize("failed  69 TIMES.")
    assert normalize("Disk full") != normalize("Disk fine")


@pytest.mark.asyncio
async def test_the_same_message_goes_out_once_per_window(tmp_path):
    store, sched, rt, row = setup(tmp_path, {
        "a": {**NOTIFY, "on": {"success": ["b"]}},
        "b": {**NOTIFY, "text": "deploy finished after 4 tries!"}})       # same news, other numbers
    run = await run_now(store, sched, row)
    assert run["state"] == "success" and len(sends(rt)) == 1
    assert run["steps"]["b"]["output"]["suppressed"] is True
    await run_now(store, sched, row)
    assert len(sends(rt)) == 1                                 # the next run is quiet too
    sched.clock.advance(6 * 3600 + 1)                          # the default window is 6h
    await run_now(store, sched, row)
    assert len(sends(rt)) == 2


@pytest.mark.asyncio
async def test_window_targets_and_off(tmp_path):
    store, sched, rt, row = setup(tmp_path / "t", {
        "a": {**NOTIFY, "on": {"success": ["b"]}},
        "b": {**NOTIFY, "worker": {"any_with_cap": True}, "text": "other news"}})
    await run_now(store, sched, row)
    assert len(sends(rt)) == 2                                 # different text: both go
    store, sched, rt, row = setup(tmp_path / "off", {"a": {**NOTIFY, "on": {"success": ["b"]}}, "b": NOTIFY},
                                  limits={"notify_window": 0})
    await run_now(store, sched, row)
    assert len(sends(rt)) == 2
    store, sched, rt, row = setup(tmp_path / "hub", {"a": {**NOTIFY, "on": {"success": ["b"]}}, "b": NOTIFY},
                                  settings={"notify_window": "0"}.get)
    await run_now(store, sched, row)
    assert len(sends(rt)) == 2


@pytest.mark.asyncio
async def test_failed_sends_retry_and_questions_fail_when_held_back(tmp_path):
    flaky = iter([{"ok": False, "error": "phone asleep"}, ok({"posted": True})])
    store, sched, rt, row = setup(tmp_path, {"a": {**NOTIFY, "on": {"failure": ["b"]}}, "b": NOTIFY},
                                  caps={"notify.post": lambda a, w: next(flaky),
                                        "voice.speak": lambda a, w: ok({"reply": {"text": "yes"}})})
    await run_now(store, sched, row)
    assert len(sends(rt)) == 2                                 # a failed send is not remembered
    ask = {"kind": "ask", "text": "Disable the watcher?", "worker": "alpha"}
    store, sched, rt, row = setup(tmp_path / "ask", {"a": {**ask, "on": {"success": ["b"]}}, "b": ask},
                                  caps={"voice.speak": lambda a, w: ok({"reply": {"text": "yes"}})})
    run = await run_now(store, sched, row)
    assert len(sends(rt, "voice.speak")) == 1
    assert run["steps"]["b"]["state"] == "failure" and "not asked again" in run["steps"]["b"]["error"]


@pytest.mark.asyncio
async def test_cap_fanout_and_alerts_are_deduped(tmp_path):
    speak = {"kind": "cap", "worker": "alpha", "cap": "voice.speak", "args": {"text": "Huddles are live"}}
    fan = {"kind": "fanout", "cap": "voice.speak", "args": {"text": "huddles are LIVE"}}
    store, sched, rt, row = setup(tmp_path, {"a": {**speak, "on": {"success": ["b"]}}, "b": fan})
    run = await run_now(store, sched, row)
    assert len(sends(rt, "voice.speak")) == 1 and run["state"] == "success"
    assert run["steps"]["b"]["output"]["alpha"]["output"]["suppressed"] is True

    from rook.hub.plugins.jobs.service import JobService
    store, sched, rt, row = setup(tmp_path / "alerts", {"a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec"}},
                                  caps={"shell.exec": lambda a, w: ok({"exit_code": 1}),
                                        "notify.post": lambda a, w: ok({})},
                                  alerts={"on_failure": [{"via": "notify", "text": "nightly failed", "worker": "alpha"}],
                                          "on_success": []})
    svc = JobService(store, sched, settings=lambda k: None, clock=sched.clock)
    sched.alerts = svc.alerts
    for _ in range(3):
        await run_now(store, sched, row)
    assert len(sends(rt)) == 1


# -- agents: finish(complete) and notify_bake ---------------------------------------------------

async def agent_run(env, rt, steps, *, locked=False, store=None):
    job, _ = check(job_doc(steps))
    job["id"] = "j_agent"
    notices = Notices(store, lambda k: None, rt.clock) if store is not None else None
    ex = Executor(rt, notices=notices)
    return await ex.execute(job, {"id": "r_a", "started": rt.clock(), "missed": False, "vars": {},
                                  "locked": locked}, resolve_identity(job, OWNER))


def done(text="reported", complete=True):
    return call("finish", 99, verdict="ok", text=text, complete=complete)


@pytest.mark.asyncio
async def test_agent_finish_complete_ends_the_job(env):
    rt = runtime(env)
    env.llm.script = [done()]
    res = await agent_run(env, rt, {"a": agent_step()})
    assert res["complete"] == {"step": "a", "why": "the agent finished with complete"}
    finish_tool = [t for t in env.llm.chats()[0][2]["tools"] if t["function"]["name"] == "finish"][0]
    assert "complete" in finish_tool["function"]["parameters"]["properties"]
    env.llm.script = [done(complete=False)]
    assert "complete" not in await agent_run(env, rt, {"a": agent_step()})


@pytest.mark.asyncio
async def test_a_locked_job_needs_complete_agent_from_a_person(env):
    rt = runtime(env)
    env.llm.script = [done()]
    res = await agent_run(env, rt, {"a": agent_step()}, locked=True)
    assert "complete" not in res and "locked" in res["steps"]["a"]["complete_ignored"]
    env.llm.script = [done()]
    res = await agent_run(env, rt, {"a": agent_step(complete="agent")}, locked=True)
    assert res["complete"]["step"] == "a"


@pytest.mark.asyncio
async def test_agent_messages_are_deduped_on_every_path(env, tmp_path):
    from rook.hub.plugins.jobs.store import JobStore
    store = JobStore(tmp_path / "dedupe.db")
    rt = runtime(env, caps={"voice.speak": lambda a, w: ok({"spoken": True}),
                            "notify.post": lambda a, w: ok({"posted": True})})
    env.llm.script = [call("notify_bake", 1, text="The console tool is missing (run 41)"),
                      call("notify_bake", 2, text="the console tool is missing (run 42)"),
                      call("rook_call", 3, worker="alpha", cap="voice.speak", args={"text": "Tests fixed"}),
                      call("rook_call", 4, worker="alpha", cap="voice.speak", args={"text": "tests fixed."}),
                      finish()]
    res = await agent_run(env, rt, {"a": agent_step()}, store=store)
    assert res["state"] == "success"
    assert len(sends(rt, "notify.post")) == 1 and len(sends(rt, "voice.speak")) == 1
    results = [json.loads(m["content"]) for m in env.llm.chats()[-1][2]["messages"] if m.get("role") == "tool"]
    assert results[1]["result"]["suppressed"] is True and results[3]["result"]["suppressed"] is True


# -- locks ------------------------------------------------------------------------------------------

def node(tmp_path):
    return HubNode(str(tmp_path), entry_points=False, build_version="1.test.node",
                   settings_store=SettingsStore(tmp_path / "settings.db"))


def tool_for(n, principal):
    async def invoke(cap, args):
        tok = current_principal.set(principal)
        try:
            return await n.invoke(cap, args, principal.id)
        finally:
            current_principal.reset(tok)
    tools = {t.__name__: t for t in n.plugin("job").mcp_tools(invoke)}

    async def call_(**kw):
        return json.loads(await tools["rook_jobs"](**kw))
    return call_


SELF_OFF = {"name": "watch", "entry": "a", "triggers": [{"kind": "cron", "expr": "*/10 * * * *"}],
            "steps": {"a": {"kind": "tool", "tool": "rook_jobs",
                            "args": {"action": "disable", "id": "watch"}}}}


@pytest.mark.asyncio
async def test_only_a_person_locks_and_agents_are_refused(tmp_path):
    n = node(tmp_path)
    agent, static, bake = tool_for(n, AGENT), tool_for(n, STATIC), tool_for(n, PERSON)
    made = await agent(action="create", data=SELF_OFF)
    assert made["ok"] and made["result"]["locked"] is False and made["result"]["can"]["lock"] is False
    for who in (agent, static):
        out = await who(action="lock", id="watch")
        assert not out["ok"] and out["code"] == "PermissionError" and "only a person" in out["error"]
    locked = await bake(action="lock", id="watch", data={"note": "keep it"})
    assert locked["ok"], locked
    j = locked["result"]
    assert j["locked"] is True and j["locked_by"] == "human:u1" and j["can"]["lock"] is True
    assert j["history"][0]["action"] == "lock" and j["history"][0]["actor"] == "human:u1"
    assert j["history"][0]["detail"]["note"] == "keep it"

    seen = (await agent(action="get", id="watch"))["result"]
    assert seen["locked"] is True and seen["can"]["edit"] is False
    run = await agent(action="run", id="watch")                     # running stays open
    assert run["ok"]
    refused = [
        await agent(action="update", id="watch", data={"description": "x"}),
        await agent(action="disable", id="watch"),
        await agent(action="delete", id="watch"),
        await agent(action="cancel", id="watch"),
        await agent(action="cancel", id=run["result"]["id"]),
        await agent(action="set_guardrails", id="watch", data={"guardrails": {"deny": ["shell.exec"]}}),
        await static(action="disable", id="watch"),                  # the operator token too
        await static(action="unlock", id="watch"),
    ]
    for out in refused:
        assert not out["ok"] and out["code"] == "PermissionError", out
    assert all("locked:" in o["error"] for o in refused[:-1])
    off = await bake(action="disable", id="watch")
    assert off["ok"] and off["result"]["enabled"] is False
    assert not (await agent(action="enable", id="watch"))["ok"]
    assert (await bake(action="enable", id="watch"))["ok"]
    assert (await bake(action="update", id="watch", data={"description": "kept"}))["ok"]
    out = await agent(action="update", id="watch", data={"locked": False})
    assert not out["ok"]
    un = await bake(action="unlock", id="watch")
    assert un["ok"] and un["result"]["locked"] is False
    assert un["result"]["history"][0]["action"] == "unlock"
    assert un["result"]["history"][0]["detail"]["was_locked_by"] == "human:u1"
    assert (await agent(action="disable", id="watch"))["ok"]


@pytest.mark.asyncio
async def test_a_locked_jobs_own_run_cannot_switch_it_off(tmp_path):
    """Even a person's job: its run acts with the job in the chain, so it is
    not a person signing in."""
    n = node(tmp_path)
    bake = tool_for(n, PERSON)
    assert (await bake(action="create", data=SELF_OFF))["ok"]
    assert (await bake(action="lock", id="watch"))["ok"]
    started = (await bake(action="run", id="watch"))["result"]
    p = n.plugin("job")
    await p.scheduler.tick()
    await drain(p.scheduler)
    run = (await bake(action="run_get", id=started["id"]))["result"]
    assert run["state"] == "failure" and "locked:" in run["steps"]["a"]["error"]
    assert (await bake(action="get", id="watch"))["result"]["enabled"] is True
    # Unlocked, the same run may switch its job off (as before).
    assert (await bake(action="unlock", id="watch"))["ok"]
    started = (await bake(action="run", id="watch"))["result"]
    await p.scheduler.tick()
    await drain(p.scheduler)
    assert (await bake(action="get", id="watch"))["result"]["enabled"] is False


@pytest.mark.asyncio
async def test_a_person_acting_through_something_else_is_not_a_person(tmp_path):
    n = node(tmp_path)
    assert (await tool_for(n, PERSON)(action="create", data=SELF_OFF))["ok"]
    chained = Principal("human:u1", "human", "owner", ("human:owner",), via=("agent:home",))
    out = await tool_for(n, chained)(action="lock", id="watch")
    assert not out["ok"] and "only a person" in out["error"]

    async def inside(cap, args):                     # a person's request, another caller identity
        tok = current_principal.set(PERSON)
        try:
            return await n.invoke(cap, args, "agent:home")
        finally:
            current_principal.reset(tok)
    tool = n.plugin("job").mcp_tools(inside)[0]
    out = json.loads(await tool(action="lock", id="watch"))
    assert not out["ok"] and "only a person" in out["error"]


# -- web and the rook band panel ------------------------------------------------------------------

ACCOUNTS = SimpleNamespace(session=lambda c: {
    "admin": {"id": "u1", "username": "pat", "name": "Pat", "csrf": "k", "admin": True}}.get(c))
TOKEN = "t" * 64


@pytest.mark.asyncio
async def test_web_session_and_panel_path_lock_and_unlock(tmp_path):
    n = node(tmp_path)
    assert (await tool_for(n, AGENT)(action="create", data=SELF_OFF))["ok"]
    app = Starlette(routes=jobs_web.routes(lambda: n, ACCOUNTS, TOKEN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        web = {"Cookie": "rook_account=admin"}
        r = await c.post(jobs_web.PATH, headers=web, json={"csrf": "k", "action": "lock", "id": "watch"})
        assert r.status_code == 200 and r.json()["result"]["locked"] is True
        assert r.json()["result"]["locked_by"] == "human:u1"
        r = await c.post(jobs_web.PATH, headers=web, json={"csrf": "k", "action": "disable", "id": "watch"})
        assert r.status_code == 200 and r.json()["result"]["enabled"] is False
        # The rook band panel: the dashboard forwards the admitted person.
        panel = {"Authorization": "Bearer " + TOKEN,
                 "X-Rook-Principal": json.dumps({"id": "human:u1", "role": "owner", "label": "Pat"})}
        r = await c.post(jobs_web.PATH, headers=panel, json={"action": "unlock", "id": "watch"})
        assert r.status_code == 200 and r.json()["result"]["locked"] is False
        r = await c.post(jobs_web.PATH, headers=panel, json={"action": "lock", "id": "watch"})
        assert r.json()["result"]["locked"] is True
    out = await tool_for(n, AGENT)(action="enable", id="watch")
    assert not out["ok"] and "locked:" in out["error"]


def test_panel_menu_locks_and_shows_the_mark():
    job = {"id": "j_1", "name": "watch", "enabled": True, "locked": False, "triggers": ["cron */10 * * * *"],
           "next": None, "last_run": None}
    calls = []

    def jobs(action, id=None, data=None, query=""):
        calls.append(action)
        if action == "list":
            return {"ok": True, "result": {"jobs": [job], "timezone": "UTC"}}
        return {"ok": True, "result": dict(job, locked=action == "lock")}
    ui = tui.UI(SimpleNamespace(overview=lambda: {"workers": [], "chats": []}, jobs=jobs), "test")
    try:
        ui.draw = lambda scr: None
        picks = iter([0, 3, 3, None, None])        # watch -> lock -> unlock -> back -> back
        titles, popups = [], []
        ui.picker = lambda scr, title, items: (titles.append(items), next(picks))[1]
        ui.popup = lambda scr, title, text: popups.append((title, text))
        ui.act_jobs(None)
    finally:
        ui.close()
    assert calls == ["list", "lock", "unlock", "list"]
    assert titles[1][3] == "lock" and titles[2][3] == "unlock" and titles[3][3] == "lock"
    assert "agents can no longer" in popups[0][1]
    assert tui._job_line(dict(job, locked=True)).startswith("[x]Lwatch")


def test_jobs_page_has_the_lock_mark_and_control():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "rook/web/jobs.js").read_text()
    assert "export function lockMark" in js and "'unlock':'lock'" in js and "can.lock" in js
