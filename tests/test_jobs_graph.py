"""Jobs graph: validation on save, the safe join evaluator, and the executor
(branching, parallel branches, joins, retries, timeouts/hang, success rules,
the step-execution cap, guardrail seam, secret substitution and masking)
against a fake band (tests/jobs_fakes.py)."""
import asyncio

import pytest

from rook.hub.plugins.jobs import executor as executor_mod
from rook.hub.plugins.jobs.expr import compile_expr, evaluate, step_refs
from rook.hub.plugins.jobs.guardrails import Verdict
from rook.hub.plugins.jobs.model import ValidationError, check, normalize, schema, validate
from rook.hub.plugins.jobs.steps import (StepResult, register_step_kind, step_kinds,
                                         unregister_step_kind)
from tests.jobs_fakes import FakeRuntime, execute, job_doc, ok, shell


def errors(doc, now=None):
    return validate(normalize(doc), now=now)[0]


def cap(on=None, **kw):
    return {"kind": "cap", "worker": "alpha", "cap": "shell.exec", "args": {"cmd": "true"},
            **({"on": on} if on else {}), **kw}


# -- validation -------------------------------------------------------------------------

def test_minimal_job_gets_defaults():
    job, warns = check(job_doc({"a": cap()}))
    assert warns == []
    assert job["triggers"] == [{"kind": "manual"}]
    assert job["overlap"] == {"mode": "queue", "max_queue": None}  # unbounded queue by default
    assert job["missed"] == {"mode": "run_once", "grace": "10m"}
    assert job["steps"]["a"]["timeout"] == "5m" and job["steps"]["a"]["on"] == {}
    assert job["identity"] == {"mode": "creator"}


def test_on_accepts_a_single_id():
    job, _ = check(job_doc({"a": cap(on={"success": "b"}), "b": {"kind": "noop"}}))
    assert job["steps"]["a"]["on"] == {"success": ["b"]}


@pytest.mark.parametrize("doc,needle", [
    (job_doc({"a": cap(on={"success": ["nope"]})}), "no step 'nope'"),
    (job_doc({"a": cap(), "b": {"kind": "noop"}}), "steps.b: not reachable"),
    (job_doc({"a": {"kind": "bogus"}}), "unknown kind 'bogus'"),
    (job_doc({"a": {"kind": "agent"}}), "not available on this hub yet"),
    (job_doc({"a": {"kind": "ask"}}), "not available on this hub yet"),
    (job_doc({"a": cap(timeout="0s")}), "must be more than zero"),
    (job_doc({"a": cap(timeout="soon")}), "not a duration"),
    (job_doc({"a": cap(on={"done": ["a"]})}), "outcome keys are"),
    (job_doc({"a": cap(retry={"max": 50})}), "retry.max"),
    (job_doc({"a": cap(success={"match": "("})}), "bad regex"),
    (job_doc({"a": cap(success={"exit_codes": ["0"]})}), "exit_codes"),
    (job_doc({"a": {"kind": "cap", "cap": "x"}}), "worker must be"),
    (job_doc({"a": {"kind": "cap", "worker": "w"}}), "cap is required"),
    (job_doc({"a": {"kind": "fanout", "cap": "x", "filter": {"color": "red"}}}), "unknown key 'color'"),
    (job_doc({"a": {"kind": "fanout", "cap": "x", "join": "most"}}), 'join must be'),
    (job_doc({"a": {"kind": "wait"}}), "exactly one of"),
    (job_doc({"a": {"kind": "wait", "for": "10m"}}), "longer than the wait"),
    (job_doc({"a": {"kind": "notify", "via": "pigeon", "text": "x"}}), "via must be"),
    (job_doc({"a": {"kind": "tool", "tool": "shell"}}), "hub MCP tool"),
    (job_doc({"a": cap()}, entry="zz"), "entry: no step"),
    (job_doc({}), "at least one step"),
    (job_doc({"a": cap()}, triggers=[{"kind": "cron", "expr": "61 * * * *"}]), "triggers[0].expr"),
    (job_doc({"a": cap()}, triggers=[{"kind": "cron", "expr": "@daily", "tz": "Mars/Base"}]), "unknown time zone"),
    (job_doc({"a": cap()}, triggers=[{"kind": "at", "when": "2026-10-11T09:00"}]), "no UTC offset"),
    (job_doc({"a": cap()}, triggers=[{"kind": "after", "every": "0s"}]), "more than zero"),
    (job_doc({"a": cap()}, triggers=[{"kind": "after", "every": "1h", "on": "never"}]), "triggers[0].on"),
    (job_doc({"a": cap()}, triggers=[{"kind": "hourly"}]), "triggers[0].kind"),
    (job_doc({"a": cap()}, overlap={"mode": "pile"}), "overlap.mode"),
    (job_doc({"a": cap()}, overlap={"mode": "queue", "max_queue": -1}), "max_queue"),
    (job_doc({"a": cap()}, missed={"mode": "later"}), "missed.mode"),
    (job_doc({"a": cap()}, identity={"mode": "vault", "ref": "k"}), "creator only"),
    (job_doc({"a": cap()}, retention_days=0), "retention_days"),
    (job_doc({"a": cap()}, alerts={"on_failure": [{"via": "notify"}]}), "alerts.on_failure[0]: text"),
    (job_doc({"a": cap()}, colour="blue"), "colour: unknown field"),
    ({"entry": "a", "steps": {"a": cap()}}, "name: required"),
    (job_doc({"a b": {"kind": "noop"}}, entry="a b"), "step ids are"),
])
def test_validation_errors(doc, needle):
    errs = errors(doc)
    assert any(needle in e for e in errs), errs
    with pytest.raises(ValidationError) as e:
        check(doc)
    assert e.value.errors == errs


def test_join_validation():
    base = {"a": cap(on={"success": ["b", "c"]}), "b": cap(on={"success": ["j"]}),
            "c": cap(on={"success": ["j"]})}
    assert errors(job_doc({**base, "j": {"kind": "join"}})) == []
    assert errors(job_doc({**base, "j": {"kind": "join", "condition": {"at_least": 2}}})) == []
    assert any("at_least 3" in e for e in errors(job_doc({**base, "j": {"kind": "join", "condition": {"at_least": 3}}})))
    assert errors(job_doc({**base, "j": {"kind": "join", "condition": "steps.b.ok and not steps.c.ok"}})) == []
    assert any("no step 'zz'" in e for e in errors(job_doc({**base, "j": {"kind": "join", "condition": "steps.zz.ok"}})))
    assert any("may not use Call" in e for e in errors(job_doc({**base, "j": {"kind": "join", "condition": "__import__('os')"}})))
    # A join inside a loop could never settle once per run.
    looped = {**base, "j": {"kind": "join", "on": {"failure": ["a"]}}}
    assert any("inside a loop" in e for e in errors(job_doc(looped)))
    # An entry join has nothing to wait for.
    assert any("needs incoming" in e for e in errors(job_doc({"j": {"kind": "join"}})))


def test_at_in_the_past_is_a_warning():
    _, warns = check(job_doc({"a": cap()}, triggers=[{"kind": "at", "when": "2020-01-01T00:00:00Z"}]),
                     now=1791633600.0)
    assert warns and "in the past" in warns[0]


def test_schema_lists_registered_kinds_and_defaults():
    s = schema()
    assert set(s["$defs"]["kinds"]) == set(step_kinds()) >= {"cap", "fanout", "tool", "wait", "notify", "join", "noop"}
    assert s["properties"]["overlap"]["properties"]["max_queue"]["default"] is None
    assert s["required"] == ["name", "entry", "steps"]


# -- the expression evaluator ------------------------------------------------------------

def test_expressions_are_safe_and_read_lookups():
    scope = {"steps": {"a": {"ok": True, "exit_code": 0}, "b-2": {"ok": False}},
             "run": {"missed": True}, "vars": {"mode": "full"}}
    assert evaluate("steps.a.ok and not steps['b-2'].ok", scope)
    assert evaluate("run.missed and vars.mode == 'full'", scope)
    assert evaluate("steps.a.exit_code in [0, 2]", scope)
    assert not evaluate("steps.nope.ok", scope)
    assert not evaluate("steps.a.exit_code > 'x'", scope)  # type errors are false, not crashes
    assert step_refs(compile_expr("steps.a.ok or steps['b-2'].ok")) == {"a", "b-2"}
    for bad in ("1 + 1", "open('x')", "os.system", "steps.a.__class__ if True else 1", "lambda: 1",
                "[x for x in steps]", "steps[steps]", ""):
        with pytest.raises(ValueError):
            compile_expr(bad)


# -- the executor --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branches_follow_outcomes(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": lambda a, w: ok({"exit_code": 1 if a["cmd"] == "false" else 0})})
    res = await execute(rt, {
        "check": cap(args={"cmd": "false"}, on={"success": ["good"], "failure": ["bad"]}),
        "good": {"kind": "noop"}, "bad": {"kind": "noop"}})
    assert res["steps"]["check"]["state"] == "failure" and res["steps"]["check"]["error"] == "exit code 1"
    assert "good" not in res["steps"] and res["steps"]["bad"]["state"] == "success"
    assert res["state"] == "failure"   # the worst final step state
    assert rt.calls[0][2] == "w1" and rt.calls[0][3].startswith("job:j_test/")


@pytest.mark.asyncio
async def test_allow_failure_keeps_the_run_green(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": shell(exit_code=2)})
    res = await execute(rt, {"a": cap(allow_failure=True, on={"failure": ["b"]}), "b": {"kind": "noop"}})
    assert res["steps"]["a"]["state"] == "failure" and res["state"] == "success"


@pytest.mark.asyncio
async def test_success_rules(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": shell(exit_code=3, stdout="disk 91% full")})
    res = await execute(rt, {"a": cap(success={"exit_codes": [0, 3], "not_match": r"9\d%"})})
    assert res["steps"]["a"]["state"] == "failure" and "matches" in res["steps"]["a"]["error"]
    res = await execute(rt, {"a": cap(success={"exit_codes": [3], "match": "full"})})
    assert res["state"] == "success" and res["steps"]["a"]["exit_code"] == 3
    rt.caps["shell.exec"] = lambda a, w: {"ok": False, "error": "boom"}
    res = await execute(rt, {"a": cap()})
    assert res["steps"]["a"] == {**res["steps"]["a"], "state": "failure", "error": "boom"}
    rt.caps["shell.exec"] = lambda a, w: ok({"ok": False, "error": "nested no"})
    assert (await execute(rt, {"a": cap()}))["steps"]["a"]["error"] == "nested no"


@pytest.mark.asyncio
async def test_retries_before_branching(tmp_path):
    seen = []

    def flaky(args, wid):
        seen.append(1)
        return ok({"exit_code": 0 if len(seen) == 3 else 1})
    rt = FakeRuntime(tmp_path, caps={"shell.exec": flaky})
    res = await execute(rt, {"a": cap(retry={"max": 2, "delay": "30s"}, on={"failure": ["alarm"]}),
                             "alarm": {"kind": "noop"}})
    assert res["steps"]["a"]["state"] == "success" and res["steps"]["a"]["attempts"] == 3
    assert "alarm" not in res["steps"] and rt.slept == [30, 30]
    seen.clear()
    res = await execute(rt, {"a": cap(retry={"max": 1}, on={"failure": ["alarm"]}), "alarm": {"kind": "noop"}})
    assert res["steps"]["a"]["attempts"] == 2 and res["steps"]["alarm"]["state"] == "success"


@pytest.mark.asyncio
async def test_timeout_is_a_hang_and_branches_on_hang(tmp_path):
    async def slow(args, wid):
        await asyncio.sleep(5)
        return ok()
    rt = FakeRuntime(tmp_path, caps={"shell.exec": slow})
    res = await execute(rt, {"a": cap(timeout="0.2s", retry={"max": 1}, on={"hang": ["page"], "failure": ["x"]}),
                             "page": {"kind": "noop"}, "x": {"kind": "noop"}})
    assert res["steps"]["a"]["state"] == "hang" and res["steps"]["a"]["attempts"] == 2
    assert "page" in res["steps"] and "x" not in res["steps"]
    assert res["state"] == "hang"
    # The journal has the timed-out calls, with the placeholders' templated args.
    assert any(r["reply"].get("timeout") for r in rt.node.journal.rows)


@pytest.mark.asyncio
async def test_offline_worker_is_retried_until_the_timeout(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": shell()})
    res = await execute(rt, {"a": {**cap(), "worker": "gamma", "timeout": "0.3s"}})
    assert res["steps"]["a"]["state"] == "hang" and rt.slept and not rt.calls

    async def comes_online():
        await asyncio.sleep(0.05)
        rt.workers["w3"] = {"name": "gamma", "caps": ["shell.exec"]}
    rt.slept.clear()
    asyncio.ensure_future(comes_online())

    async def sleep(s):
        rt.slept.append(s)
        await asyncio.sleep(0.02)
    rt.sleep = sleep
    res = await execute(rt, {"a": {**cap(), "worker": "gamma", "timeout": "2s"}})
    assert res["steps"]["a"]["state"] == "success" and rt.calls[-1][2] == "w3"


@pytest.mark.asyncio
async def test_worker_selection(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": shell(), "hub.info": lambda a, w: ok({"name": "rook"})})
    await execute(rt, {"a": {**cap(), "worker": {"any_with_cap": True}}})
    assert rt.calls[-1][2] == "w1"  # first by name
    await execute(rt, {"a": {**cap(), "worker": {"filter": {"os": "windows"}}}})
    assert rt.calls[-1][2] == "w2"
    await execute(rt, {"a": {"kind": "cap", "worker": "rook", "cap": "hub.info"}})
    assert rt.calls[-1][2] == "hubnode"
    rt.workers["w9"] = {"name": "alpha", "caps": ["shell.exec"]}
    res = await execute(rt, {"a": cap()})
    assert "ambiguous" in res["steps"]["a"]["error"]


@pytest.mark.asyncio
async def test_parallel_branches_and_join_all(tmp_path):
    order = []

    async def work(args, wid):
        order.append(("start", args["cmd"]))
        await asyncio.sleep(0.05)
        order.append(("end", args["cmd"]))
        return ok({"exit_code": 0})
    rt = FakeRuntime(tmp_path, caps={"shell.exec": work})
    res = await execute(rt, {
        "a": {"kind": "noop", "on": {"success": ["b", "c"]}},
        "b": cap(args={"cmd": "b"}, on={"success": ["j"]}),
        "c": cap(args={"cmd": "c"}, on={"success": ["j"]}),
        "j": {"kind": "join", "on": {"success": ["done"]}},
        "done": {"kind": "noop"}})
    assert order[:2] == [("start", "b"), ("start", "c")]  # ran in parallel
    assert res["steps"]["j"]["state"] == "success" and res["steps"]["j"]["output"]["arrived"] == ["b", "c"]
    assert res["state"] == "success" and res["steps"]["done"]["runs"] == 1


def _diamond(cond, b_exit=0, c_exit=0):
    return {
        "a": {"kind": "noop", "on": {"success": ["b", "c"]}},
        "b": cap(args={"cmd": "b"}, on={"success": ["j"]}, allow_failure=True),
        "c": cap(args={"cmd": "c"}, on={"success": ["j"], "failure": ["j"]}, allow_failure=True),
        "j": {"kind": "join", "condition": cond, "on": {"success": ["yes"], "failure": ["no"]}},
        "yes": {"kind": "noop"}, "no": {"kind": "noop"}}


def _rt(tmp_path, b_exit, c_exit):
    return FakeRuntime(tmp_path, caps={"shell.exec": lambda a, w: ok({"exit_code": b_exit if a["cmd"] == "b" else c_exit})})


@pytest.mark.asyncio
@pytest.mark.parametrize("cond,b_exit,c_exit,branch", [
    ("all", 0, 0, "yes"),
    ("all", 1, 0, "no"),          # b failed: its branch never arrives; settles as failure
    ("any", 1, 0, "yes"),
    ({"at_least": 2}, 1, 1, "no"),
    ({"at_least": 1}, 1, 1, "yes"),  # c arrives on its failure branch
    ("steps.b.ok and not steps.c.ok", 0, 1, "yes"),
    ("steps.b.ok and not steps.c.ok", 0, 0, "no"),
    ("steps.c.exit_code == 7", 1, 7, "yes"),
])
async def test_join_conditions(tmp_path, cond, b_exit, c_exit, branch):
    res = await execute(_rt(tmp_path, b_exit, c_exit), _diamond(cond))
    assert branch in res["steps"] and ({"yes", "no"} - {branch}).isdisjoint(res["steps"])
    assert res["steps"]["j"]["runs"] == 1


@pytest.mark.asyncio
async def test_join_any_fires_once(tmp_path):
    res = await execute(_rt(tmp_path, 0, 0), _diamond("any"))
    assert res["steps"]["yes"]["runs"] == 1 and res["executions"] == 5  # a, b, c, j, yes


@pytest.mark.asyncio
async def test_failure_loop_is_capped(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": shell(exit_code=1)})
    res = await execute(rt, {"a": cap(on={"failure": ["fix"]}), "fix": {"kind": "noop", "on": {"success": ["a"]}}},
                        settings=lambda k: 10 if k == "max_step_executions" else None)
    assert res["state"] == "failure" and "more than 10 step executions" in res["error"]
    assert res["executions"] == 11 and res["steps"]["a"]["runs"] == 5


@pytest.mark.asyncio
async def test_loop_that_recovers_succeeds(tmp_path):
    n = []
    rt = FakeRuntime(tmp_path, caps={"shell.exec": lambda a, w: (n.append(1), ok({"exit_code": 0 if len(n) > 2 else 1}))[1]})
    res = await execute(rt, {"check": cap(on={"failure": ["fix"]}), "fix": {"kind": "noop", "on": {"success": ["check"]}}})
    assert res["state"] == "success" and res["steps"]["check"]["runs"] == 3


@pytest.mark.asyncio
async def test_guardrail_seam_blocks_and_never_retries(tmp_path, monkeypatch):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": shell()})
    monkeypatch.setattr(executor_mod, "check_step",
                        lambda job, step, ident: Verdict(False, "shell is off-limits", "deny shell.*")
                        if step.get("cap") == "shell.exec" else Verdict(True))
    res = await execute(rt, {"a": cap(retry={"max": 3}, on={"failure": ["b"]}), "b": {"kind": "noop"}})
    assert res["state"] == "blocked" and res["steps"]["a"]["state"] == "blocked"
    assert res["steps"]["a"]["rule"] == "deny shell.*" and not rt.calls and "b" not in res["steps"]


@pytest.mark.asyncio
async def test_policy_denial_is_blocked(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": lambda a, w: {"ok": False, "denied": {"rule": "r1"},
                                                                 "error": "denied by policy"}})
    res = await execute(rt, {"a": cap()})
    assert res["state"] == "blocked"


@pytest.mark.asyncio
async def test_secrets_resolve_at_execution_and_are_masked(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": lambda a, w: ok({"exit_code": 0, "stdout": "token=" + a["cmd"]})})
    rt.vault.set("pw", "hunter22-correct-horse", "test", "test")
    res = await execute(rt, {"a": cap(args={"cmd": "{{secret:pw}}"})})
    assert rt.calls[0][1] == {"cmd": "hunter22-correct-horse"}            # the worker got the value
    assert "hunter22" not in str(res["steps"])                             # the record did not
    assert res["steps"]["a"]["output"]["stdout"] == "token={{secret:pw}}"
    assert rt.node.journal.rows[0]["args"] == {"cmd": "{{secret:pw}}"}   # journal keeps the placeholder
    assert "hunter22" not in str(rt.node.journal.rows)
    res = await execute(rt, {"a": cap(args={"cmd": "{{secret:nope}}"})})
    assert res["state"] == "failure" and "nope" in res["steps"]["a"]["error"]


@pytest.mark.asyncio
async def test_templates_and_vars(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": shell()})
    await execute(rt, {"a": cap(args={"cmd": "echo {{job.name}} {{run.id}} {{vars.where}} {{vars.missing}}",
                                      "at": "{{run.started}}"})},
                  vars={"where": "lab"}, name="nightly")
    assert rt.calls[0][1]["cmd"] == "echo nightly r_test lab {{vars.missing}}"
    assert rt.calls[0][1]["at"] == "2026-10-10T12:00:00+00:00"


@pytest.mark.asyncio
async def test_output_is_truncated(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": lambda a, w: ok({"exit_code": 0, "stdout": "x" * 20000})})
    res = await execute(rt, {"a": cap()})
    out = res["steps"]["a"]["output"]
    assert isinstance(out, str) and len(out) < 4100 and "truncated" in out


@pytest.mark.asyncio
async def test_fanout_join_rules(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"shell.exec": lambda a, w: ok({"exit_code": 0 if w == "w1" else 1})})
    fan = {"kind": "fanout", "cap": "shell.exec", "args": {"cmd": "x"}}
    res = await execute(rt, {"a": fan})
    assert res["state"] == "failure" and res["steps"]["a"]["output"]["beta"]["state"] == "failure"
    res = await execute(rt, {"a": {**fan, "join": "any"}})
    assert res["state"] == "success" and res["steps"]["a"]["succeeded"] == 1
    res = await execute(rt, {"a": {**fan, "join": {"at_least": 2}}})
    assert res["state"] == "failure"
    res = await execute(rt, {"a": {**fan, "filter": {"os": "linux"}}})
    assert res["state"] == "success" and set(res["steps"]["a"]["output"]) == {"alpha"}
    res = await execute(rt, {"a": {**fan, "filter": {"tags": ["gpu"]}}})
    assert res["state"] == "failure" and "no live worker" in res["steps"]["a"]["error"]


@pytest.mark.asyncio
async def test_wait_notify_and_tool_steps(tmp_path):
    async def rook_task(action="deck", **kw):
        return '{"ok":true,"result":{"in_progress":[]}}' if action == "deck" else '{"ok":false,"error":"no"}'
    rt = FakeRuntime(tmp_path, caps={"notify.post": lambda a, w: ok({"posted": True}),
                                     "voice.speak": lambda a, w: ok({}),
                                     "notify.send": lambda a, w: ok({"ok": False, "error": "no chat integration"})},
                     tools={"rook_task": rook_task})
    res = await execute(rt, {
        "w": {"kind": "wait", "for": "90s", "on": {"success": ["n"]}},
        "n": {"kind": "notify", "text": "run {{run.id}} done", "on": {"success": ["v"]}},
        "v": {"kind": "notify", "via": "voice", "text": "hi", "on": {"success": ["t"]}},
        "t": {"kind": "tool", "tool": "rook_task", "args": {"action": "deck"}, "on": {"success": ["bad"]}},
        "bad": {"kind": "tool", "tool": "rook_task", "args": {"action": "nope"}, "allow_failure": True,
                "on": {"failure": ["tg"]}},
        "tg": {"kind": "notify", "via": "telegram", "text": "x", "allow_failure": True}})
    assert rt.slept == [90]
    assert ("notify.post", {"title": "Rook job j", "text": "run r_test done"}, "w1") == rt.calls[0][:3]
    assert rt.calls[1][:3] == ("voice.speak", {"text": "hi", "wait": False}, "w1")
    assert res["steps"]["t"]["output"] == {"in_progress": []}
    assert res["steps"]["bad"]["state"] == "failure"
    assert rt.calls[2][:3] == ("notify.send", {"text": "x", "channel": "telegram"}, "hubnode")
    assert res["steps"]["tg"]["state"] == "failure" and res["state"] == "success"


@pytest.mark.asyncio
async def test_registered_kind_is_usable(tmp_path):
    """The registry is the seam for the agent/ask kinds."""
    async def run_ask(ctx, step):
        return StepResult("success", output={"reply": "yes"}, extra={"reply": "yes"})
    register_step_kind("ask", run_ask, validate=lambda s: [] if s.get("question") else ["question is required"])
    try:
        assert any("question is required" in e for e in errors(job_doc({"a": {"kind": "ask"}})))
        res = await execute(FakeRuntime(tmp_path), {"a": {"kind": "ask", "question": "go?"}})
        assert res["steps"]["a"]["reply"] == "yes" and "ask" in schema()["$defs"]["kinds"]
        with pytest.raises(ValueError):
            register_step_kind("ask", run_ask)
    finally:
        unregister_step_kind("ask")
    assert any("not available" in e for e in errors(job_doc({"a": {"kind": "ask", "question": "go?"}})))


@pytest.mark.asyncio
async def test_cancel_marks_running_steps(tmp_path):
    async def slow(args, wid):
        await asyncio.sleep(5)
    rt = FakeRuntime(tmp_path, caps={"shell.exec": slow})
    task = asyncio.ensure_future(execute(rt, {"a": cap()}))
    await asyncio.sleep(0.05)
    task.cancel()
    res = await task
    assert res["state"] == "cancelled" and res["steps"]["a"]["state"] == "cancelled"
