"""Jobs J4: the ``agent`` and ``ask`` step kinds and the house agent's job
tool set, against a fake band (tests/jobs_fakes.py) and the real home plugin
talking to a scripted fake LLM endpoint (tests/test_home_agent.py's FakeLLM).

Covers delegate vs wait, verdicts ok/failed/none/timeout, context masking,
the tool scope, guardrail refusal of an agent tool call, the policy chain on
the calls it makes, the session-agent paths with fake sessions caps, ask with
and without a reply, and that home chat is unchanged."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from rook.band_mcp import secret_mask
from rook.band_mcp.chat_rooms import ChatStore
from rook.hub.node import HubNode
from rook.hub.plugins.home import HomeAgent
from rook.hub.plugins.home.job_agent import parse_verdict
from rook.hub.plugins.jobs import agent_kinds, guardrails
from rook.hub.plugins.jobs.guardrails import Verdict
from rook.hub.plugins.jobs.model import ValidationError, check, schema
from rook.hub.settings_store import SettingsStore
from tests.jobs_fakes import OWNER, FakeRuntime, execute, job_doc, ok
from tests.test_home_agent import KEY, FakeLLM, FakeVault, configure

AGENT_ID = "agent:home"


class Rt(FakeRuntime):
    """FakeRuntime that keeps the identity object of every call."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.idents = []

    async def call(self, cap, args, target, timeout, identity):
        self.idents.append((cap, identity))
        return await super().call(cap, args, target, timeout, identity)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import rook.hub.plugins.settings as settings_plugin
    monkeypatch.setattr(settings_plugin, "_admin_gate", lambda what: None)
    node = HubNode(str(tmp_path / "hub"), entry_points=False, vault=FakeVault({"llm-key": KEY}),
                   chat=ChatStore(str(tmp_path / "chat.db")),
                   settings_store=SettingsStore(tmp_path / "settings.db"))
    node.journal = SimpleNamespace(rows=[])
    node.journal.record = lambda **kw: node.journal.rows.append(kw)
    home: HomeAgent = node.plugin("home")
    llm = FakeLLM()
    home._http = llm
    e = SimpleNamespace(node=node, home=home, llm=llm, svc=node.settings, tmp=tmp_path)
    configure(e)
    return e


def runtime(env, **kw):
    """A fake band runtime (its own vault dir) wired to the real home plugin."""
    env.n = getattr(env, "n", 0) + 1
    d = env.tmp / f"rt{env.n}"
    d.mkdir()
    rt = Rt(d, **kw)
    rt.home_agent = env.home
    return rt


def call(name, i=1, **args):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def finish(verdict="ok", text="done"):
    return call("finish", 99, verdict=verdict, text=text)


def agent_step(**kw):
    return {"kind": "agent", "prompt": "Check {{job.name}} and report.", "mode": "wait", **kw}


def tool_results(llm):
    """The tool messages the model was handed, in order."""
    last = llm.chats()[-1][2]["messages"]
    return [json.loads(m["content"]) for m in last if m.get("role") == "tool"]


def journal_rows(rt):
    return rt.node.journal.rows


# -- validation and schema --------------------------------------------------------------------

@pytest.mark.parametrize("step,needle", [
    ({"kind": "agent"}, "prompt is required"),
    (agent_step(prompt="use {{secret:tok}}"), "never put {{secret"),
    (agent_step(mode="later"), "mode: one of"),
    (agent_step(tools="all"), "tools:"),
    (agent_step(context=["everything"]), "context: 'everything'"),
    (agent_step(agent="hermes"), 'agent must be "home"'),
    (agent_step(agent={"session": {"agent": "claude", "native_id": "x"}}), "worker is required"),
    (agent_step(agent={"session": {"worker": "alpha", "agent": "gemini", "native_id": "x"}}), "agent.session.agent"),
    (agent_step(agent={"session": {"worker": "alpha", "cwd": "relative/dir"}}), "absolute cwd"),
    (agent_step(max_tool_calls=500), "max_tool_calls"),
    (agent_step(budget="0s"), "budget"),
    ({"kind": "ask"}, "text is required"),
    ({"kind": "ask", "text": "ok?", "via": "telegram"}, 'via: "voice"'),
    ({"kind": "ask", "text": "ok?", "reply_timeout": 99}, "reply_timeout"),
])
def test_validation(step, needle):
    with pytest.raises(ValidationError) as e:
        check(job_doc({"a": step}))
    assert any(needle in x for x in e.value.errors), e.value.errors


def test_valid_steps_and_schema():
    check(job_doc({"a": agent_step(context=["run", "steps.a", "job"], tools=["task.*"], on={"success": ["b"]}),
                   "b": {"kind": "ask", "text": "Deploy?"}}, entry="a"))
    check(job_doc({"a": agent_step(agent={"session": {"worker": {"any_with_cap": True}, "agent": "codex",
                                                      "cwd": "C:\\work"}})}))
    kinds = schema()["$defs"]["kinds"]
    assert "agent" in kinds and "ask" in kinds and "prompt" in kinds["agent"]["properties"]


def test_design_doc_examples_validate():
    """The JSON examples in docs/design/jobs.md 6.1 are valid jobs."""
    import pathlib
    import re
    doc = (pathlib.Path(__file__).parents[1] / "docs/design/jobs.md").read_text(encoding="utf-8")
    section = doc.split("### 6.1 Examples", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```json\n(.*?)```", section, re.S)
    assert len(blocks) == 3
    jobs = [check(json.loads(b))[0] for b in blocks]
    assert jobs[0]["triggers"] == [{"kind": "cron", "expr": "@hourly"}]
    assert jobs[0]["steps"]["work"]["kind"] == "agent"


def test_parse_verdict():
    assert parse_verdict('{"verdict": "ok", "text": "fine"}') == ("ok", "fine")
    assert parse_verdict("all good\nVERDICT: failed disk full") == ("failed", "disk full")
    assert parse_verdict("no idea")[0] is None
    assert agent_kinds.find_verdict("\x1b[1mROOK-VERDICT:\x1b[0m ok shipped") == ("ok", "shipped")
    assert agent_kinds.find_verdict("end with ROOK-VERDICT: followed by ok or failed") is None


# -- the house agent, wait mode ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_ok_calls_caps_on_behalf_of_the_job(env):
    rt = runtime(env, caps={"shell.exec": lambda a, w: ok({"exit_code": 0, "stdout": "disk 40%"})})
    env.llm.script = [call("rook_call", worker="alpha", cap="shell.exec", args={"cmd": "df -h"}),
                      finish("ok", "disk is fine")]
    res = await execute(rt, {"a": agent_step(on={"success": ["done"]}), "done": {"kind": "noop"}})
    rec = res["steps"]["a"]
    assert res["state"] == "success" and rec["verdict"] == "ok" and res["steps"]["done"]["state"] == "success"
    assert rec["output"]["text"] == "disk is fine" and rec["output"]["calls"] == 1
    # Policy chain: the job identity is the principal; home and the job are on its behalf.
    cap, ident = rt.idents[0]
    assert cap == "shell.exec"
    assert ident.principal.id == OWNER["id"]
    assert ident.principal.via == (AGENT_ID, "job:j_test")
    assert ident.display == f"{AGENT_ID}/job:j_test/{OWNER['id']}"
    # The model saw the masked result; the call and the step are journaled.
    assert tool_results(env.llm)[0]["result"]["stdout"] == "disk 40%"
    assert any(r["cap"] == "shell.exec" and r["identity"].startswith(AGENT_ID) for r in journal_rows(rt))
    assert any(r["cap"] == "home.job_step" and r["identity"] == AGENT_ID for r in env.node.journal.rows)
    # The job tool set was offered (with finish); chat's knowledge tool was not.
    names = {t["function"]["name"] for t in env.llm.chats()[0][2]["tools"]}
    assert {"rook_call", "rook_tool", "notify_bake", "ask_bake", "finish"} <= names
    assert "knowledge_search" not in names


@pytest.mark.asyncio
async def test_wait_failed_verdict_branches(env):
    rt = runtime(env)
    env.llm.script = [finish("failed", "backup is stale")]
    res = await execute(rt, {"a": agent_step(on={"failure": ["alert"]}), "alert": {"kind": "noop"}})
    assert res["steps"]["a"]["state"] == "failure" and res["steps"]["a"]["verdict"] == "failed"
    assert res["steps"]["a"]["error"] == "backup is stale" and res["steps"]["alert"]["state"] == "success"


@pytest.mark.asyncio
async def test_wait_structured_final_answer_and_no_verdict(env):
    rt = runtime(env)
    env.llm.script = ['{"verdict": "ok", "text": "checked"}']
    res = await execute(rt, {"a": agent_step()})
    assert res["steps"]["a"]["state"] == "success" and res["steps"]["a"]["output"]["text"] == "checked"
    env.llm.script = ["I looked around."]
    res = await execute(rt, {"a": agent_step()})
    assert res["steps"]["a"]["state"] == "failure" and "no verdict" in res["steps"]["a"]["error"]


@pytest.mark.asyncio
async def test_wait_timeout_is_hang(env):
    rt = runtime(env)
    env.llm.gate = asyncio.Event()          # the model never answers
    res = await execute(rt, {"a": agent_step(timeout=0.3, on={"hang": ["h"]}), "h": {"kind": "noop"}})
    assert res["steps"]["a"]["state"] == "hang" and res["steps"]["h"]["state"] == "success"


@pytest.mark.asyncio
async def test_tool_budget_is_bounded(env):
    rt = runtime(env, caps={"shell.exec": lambda a, w: ok({"exit_code": 0})})
    env.llm.script = [call("rook_call", i, worker="alpha", cap="shell.exec", args={}) for i in range(5)] \
        + ["still going"]
    res = await execute(rt, {"a": agent_step(max_tool_calls=2)})
    assert len([c for c in rt.calls if c[0] == "shell.exec"]) == 2
    assert res["steps"]["a"]["state"] == "failure"
    # After the budget only finish is offered.
    assert [t["function"]["name"] for t in env.llm.chats()[-1][2]["tools"]] == ["finish"]


@pytest.mark.asyncio
async def test_home_unavailable_fails_the_step(env):
    env.svc.set("home.enabled", False, actor="human:op")
    res = await execute(runtime(env), {"a": agent_step()})
    assert res["steps"]["a"]["state"] == "failure" and "home agent is off" in res["steps"]["a"]["error"]
    env.svc.set("home.enabled", True, actor="human:op")
    env.svc.set("home.job_steps", False, actor="human:op")
    res = await execute(runtime(env), {"a": agent_step()})
    assert "home.job_steps" in res["steps"]["a"]["error"]


# -- context masking -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_is_masked(env):
    rt = runtime(env, caps={"shell.exec": lambda a, w: ok({"exit_code": 0, "stdout": f"token={a['cmd']}"})})
    rt.vault.set("tok", "s3cr3t-value-123", "", "human:op")
    env.llm.script = [call("job_run"), finish()]
    res = await execute(rt, {
        "a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec", "args": {"cmd": "{{secret:tok}}"},
              "on": {"success": ["b"]}},
        "b": agent_step(context=["steps.a", "job"])})
    assert res["state"] == "success"
    sent = json.dumps(env.llm.chats()[0][2]["messages"])
    assert "s3cr3t-value-123" not in sent and "token={{secret:tok}}" in sent
    assert '"name": "j"' in env.llm.chats()[0][2]["messages"][1]["content"]  # the job definition
    assert "s3cr3t-value-123" not in json.dumps(tool_results(env.llm))
    assert "s3cr3t-value-123" not in json.dumps(res)


@pytest.mark.asyncio
async def test_context_choice(env):
    rt = runtime(env, caps={"shell.exec": lambda a, w: ok({"exit_code": 0, "stdout": "one"})})
    env.llm.script = [finish()]
    steps = {"a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec", "args": {}, "on": {"success": ["b"]}},
             "b": agent_step(context=["vars"])}
    await execute(rt, steps, vars={"env": "prod"})
    user = env.llm.chats()[0][2]["messages"][1]["content"]
    assert '"env": "prod"' in user and '"one"' not in user and "Check j and report." in user


# -- scope, guardrails and policy ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guardrail_refuses_an_agent_tool_call(env, monkeypatch):
    seen = []

    def check_step(job, step, identity):
        seen.append((step.get("kind"), step.get("cap"), identity.display))
        if step.get("cap") == "shell.exec":
            return Verdict(False, "exec is denied for this job", "deny-exec")
        return Verdict(True)
    monkeypatch.setattr(guardrails, "check_step", check_step)
    rt = runtime(env, caps={"shell.exec": lambda a, w: ok({"exit_code": 0})})
    env.llm.script = [call("rook_call", worker="alpha", cap="shell.exec", args={"cmd": "rm -rf /tmp/x"}),
                      finish("failed", "blocked")]
    res = await execute(rt, {"a": agent_step()})
    assert not [c for c in rt.calls if c[0] == "shell.exec"]          # never sent
    out = tool_results(env.llm)[0]
    assert out["state"] == "blocked" and "exec is denied" in out["error"] and "deny-exec" in out["error"]
    assert ("cap", "shell.exec", f"{AGENT_ID}/job:j_test/{OWNER['id']}") in seen
    row = [r for r in journal_rows(rt) if r["cap"] == "shell.exec"][0]
    assert row["reply"]["denied"] and row["identity"].startswith(AGENT_ID)
    assert res["steps"]["a"]["state"] == "failure"


@pytest.mark.asyncio
async def test_guardrails_cover_tools_notify_and_ask(env, monkeypatch):
    monkeypatch.setattr(guardrails, "check_step",
                        lambda job, step, ident: Verdict(step.get("kind") not in ("tool", "notify", "ask"), "no"))
    called = []
    rt = runtime(env, tools={"rook_task": lambda **kw: called.append(kw)})
    env.llm.script = [call("rook_tool", 1, tool="rook_task", args={"action": "deck"}),
                      call("notify_bake", 2, text="hi"), call("ask_bake", 3, question="go?"), finish()]
    await execute(rt, {"a": agent_step()})
    assert not called and not rt.calls
    assert [r["state"] for r in tool_results(env.llm)] == ["blocked"] * 3


@pytest.mark.asyncio
async def test_policy_denial_reaches_the_model(env):
    rt = runtime(env, caps={"shell.exec": lambda a, w: {"ok": False, "denied": {"rule": "r1"},
                                                        "error": "denied by policy"}})
    env.llm.script = [call("rook_call", worker="alpha", cap="shell.exec", args={}), finish("failed", "denied")]
    await execute(rt, {"a": agent_step()})
    assert tool_results(env.llm)[0]["state"] == "blocked"


@pytest.mark.asyncio
async def test_tool_scope(env):
    rt = runtime(env, caps={"shell.exec": lambda a, w: ok({"exit_code": 0}),
                            "info.get": lambda a, w: ok({"up": 1})})
    rt.workers["w1"]["caps"].append("info.get")
    env.llm.script = [call("rook_call", 1, worker="alpha", cap="shell.exec", args={}),
                      call("rook_call", 2, worker="alpha", cap="info.get", args={}),
                      call("rook_workers", 3), finish()]
    await execute(rt, {"a": agent_step(tools=["info.*"])})
    out = tool_results(env.llm)
    assert out[0]["state"] == "blocked" and "outside this step's tools scope" in out[0]["error"]
    assert out[1]["ok"] is True
    alpha = [w for w in out[2]["workers"] if w["name"] == "alpha"][0]
    assert alpha["caps"] == ["info.get"]
    # "read": exec caps are refused by tier.
    env.llm.script = [call("rook_call", worker="alpha", cap="shell.exec", args={}), finish()]
    await execute(rt, {"a": agent_step(tools="read")})
    assert "scope is read" in tool_results(env.llm)[0]["error"]
    # "none": no cap tools are offered at all.
    env.llm.script = [finish()]
    await execute(rt, {"a": agent_step(tools="none")})
    names = {t["function"]["name"] for t in env.llm.chats()[-1][2]["tools"]}
    assert names == {"job_run", "notify_bake", "ask_bake", "finish"}


@pytest.mark.asyncio
async def test_agent_reaches_bake_and_uses_hub_tools(env):
    async def deck(**kw):
        return json.dumps({"ok": True, "result": {"tasks": [{"id": "t_1", "title": "fix it"}]}})
    phone = {"voice.speak": lambda a, w: ok({"reply": {"text": "yes go", "via": "voice"}}),
             "notify.post": lambda a, w: ok({"posted": True})}
    rt = runtime(env, caps=phone, tools={"rook_task": deck})
    env.llm.script = [call("rook_tool", 1, tool="rook_task", args={"action": "deck"}),
                      call("notify_bake", 2, text="working t_1"),
                      call("ask_bake", 3, question="Deploy t_1?"), finish("ok", "deployed")]
    res = await execute(rt, {"a": agent_step()})
    out = tool_results(env.llm)
    assert out[0]["result"]["tasks"][0]["id"] == "t_1"
    assert out[1]["ok"] is True and out[2]["reply"] == "yes go"
    caps = [(c, i.display) for c, i in rt.idents]
    assert ("notify.post", f"{AGENT_ID}/job:j_test/{OWNER['id']}") in caps
    speak = [c for c in rt.calls if c[0] == "voice.speak"][0][1]
    assert speak["reply"] is True and speak["wait"] is True
    assert any(r["cap"] == "rook_task" and r["identity"].startswith(AGENT_ID) for r in journal_rows(rt))
    assert res["state"] == "success"


# -- delegate ----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delegate_moves_on_and_keeps_working(env):
    rt = runtime(env, caps={"shell.exec": lambda a, w: ok({"exit_code": 0})})
    gate = asyncio.Event()
    env.llm.gate = gate
    env.llm.script = [call("rook_call", worker="alpha", cap="shell.exec", args={}), finish("ok", "later")]
    res = await execute(rt, {"a": agent_step(mode="delegate", on={"success": ["next"]}),
                             "next": {"kind": "noop"}})
    assert res["state"] == "success" and res["steps"]["next"]["state"] == "success"
    assert res["steps"]["a"]["output"]["mode"] == "delegate" and not rt.calls
    gate.set()
    await asyncio.gather(*agent_kinds.background())
    assert [c[0] for c in rt.calls] == ["shell.exec"]
    row = [r for r in env.node.journal.rows if r["cap"] == "home.job_step"][-1]
    assert row["reply"]["result"]["verdict"] == "ok"


@pytest.mark.asyncio
async def test_default_mode_is_delegate_and_default_agent_setting(env):
    rt = runtime(env, caps={"sessions.send": lambda a, w: ok({"delivery": "turn"})})
    rt.workers["w1"]["caps"].append("sessions.send")
    spec = json.dumps({"session": {"worker": "alpha", "agent": "claude", "native_id": "abc"}})
    settings = {"default_agent": spec}.get
    res = await execute(rt, {"a": {"kind": "agent", "prompt": "Work the deck."}}, settings=settings)
    assert res["steps"]["a"]["state"] == "success"
    assert rt.calls[0][0] == "sessions.send" and rt.calls[0][1]["native_id"] == "abc"
    res = await execute(rt, {"a": {"kind": "agent", "prompt": "x"}}, settings={"default_agent": "{nope"}.get)
    assert "job.default_agent" in res["steps"]["a"]["error"]


# -- session agents ------------------------------------------------------------------------------------

def session_rt(env, caps):
    rt = runtime(env, caps=caps)
    rt.workers["w1"]["caps"] += ["sessions.send", "sessions.follow", "work.stream.open",
                                 "work.stream.read", "work.stream.write"]
    return rt


@pytest.mark.asyncio
async def test_session_poke_delegate_defuses_secrets(env):
    rt = session_rt(env, {"sessions.send": lambda a, w: ok({"delivery": "turn"}),
                          "shell.exec": lambda a, w: ok({"exit_code": 0, "stdout": "pw={{secret:tok}}"})})
    rt.vault.set("tok", "hunter2-value", "", "human:op")
    sess = {"session": {"worker": "alpha", "agent": "claude", "native_id": "abc"}}
    res = await execute(rt, {
        "a": {"kind": "cap", "worker": "alpha", "cap": "shell.exec", "args": {}, "on": {"success": ["b"]}},
        "b": {"kind": "agent", "agent": sess, "prompt": "Look at {{job.name}}", "context": ["steps.a"]}})
    rec = res["steps"]["b"]
    assert rec["state"] == "success" and rec["output"]["session"]["delivery"] == "turn"
    text = [c for c in rt.calls if c[0] == "sessions.send"][0][1]["text"]
    assert text.startswith("Look at j") and "pw=[secret:tok]" in text and "hunter2" not in text
    assert "ROOK-VERDICT" not in text                    # delegate does not ask for a verdict


@pytest.mark.asyncio
async def test_session_poke_wait_reads_the_verdict(env):
    follows = []

    def follow(a, w):
        follows.append(a)
        if a.get("tail") == 5:
            return ok({"messages": [{"index": 3, "role": "assistant", "text": "ROOK-VERDICT: ok old"}]})
        if len(follows) < 3:
            return ok({"messages": [{"index": 4, "role": "user", "text": "ROOK-VERDICT: ok typed"}]})
        return ok({"messages": [{"index": 5, "role": "assistant", "text": "Done.\nROOK-VERDICT: failed tests red"}]})
    rt = session_rt(env, {"sessions.send": lambda a, w: ok({"delivery": "keys"}), "sessions.follow": follow})
    sess = {"session": {"worker": "alpha", "agent": "codex", "native_id": "n1", "poll": "1s"}}
    res = await execute(rt, {"a": agent_step(agent=sess)})
    rec = res["steps"]["a"]
    assert rec["state"] == "failure" and rec["verdict"] == "failed" and rec["error"] == "tests red"
    assert "ROOK-VERDICT: followed by ok or failed" in rt.calls[1][1]["text"]


@pytest.mark.asyncio
async def test_session_new_wait_types_the_prompt_and_reads_the_terminal(env):
    reads = []

    def read(a, w):
        reads.append(a)
        if len(reads) == 1:
            return ok({"enc": "t", "data": "claude ready", "next": 12, "running": True})
        return ok({"enc": "t", "data": "\x1b[32mROOK-VERDICT:\x1b[0m ok merged", "next": 40, "running": True})
    rt = session_rt(env, {"work.stream.open": lambda a, w: ok({"id": "term1"}),
                          "work.stream.read": read,
                          "work.stream.write": lambda a, w: ok({"written": len(a["data"])})})
    sess = {"session": {"worker": "alpha", "agent": "claude", "cwd": "/srv/project", "task": "t_9",
                        "settle": "1s"}}
    res = await execute(rt, {"a": agent_step(agent=sess, model="opus", prompt="Line one\nLine two")})
    rec = res["steps"]["a"]
    assert rec["state"] == "success" and rec["verdict"] == "ok" and rec["output"]["text"] == "merged"
    opened = rt.calls[0][1]
    assert opened == {"harness": "claude", "cwd": "/srv/project", "title": "job j", "model": "opus",
                      "task": "t_9"}
    written = [c for c in rt.calls if c[0] == "work.stream.write"][0][1]
    assert written["id"] == "term1" and written["data"].startswith("\x1b[200~Line one\nLine two")
    assert written["data"].endswith("\r") and reads[1]["cursor"] == 12


@pytest.mark.asyncio
async def test_session_new_delegate_and_ended_session(env):
    rt = session_rt(env, {"work.stream.open": lambda a, w: ok({"id": "t2"}),
                          "work.stream.read": lambda a, w: ok({"enc": "t", "data": "", "next": 0,
                                                               "running": False, "eof": True}),
                          "work.stream.write": lambda a, w: ok({})})
    sess = {"session": {"worker": "alpha", "cwd": "/srv", "settle": "1s"}}
    res = await execute(rt, {"a": agent_step(agent=sess, mode="delegate")})
    assert res["steps"]["a"]["state"] == "success" and res["steps"]["a"]["output"]["session"]["terminal"] == "t2"
    res = await execute(rt, {"a": agent_step(agent=sess)})
    assert "ended without a verdict" in res["steps"]["a"]["error"]


# -- ask ---------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ask_with_reply(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"voice.speak": lambda a, w: ok({"reply": {"text": " Yes, deploy ", "via": "text"}})})
    res = await execute(rt, {
        "q": {"kind": "ask", "text": "Deploy {{job.name}}?", "timeout": "2m", "on": {"success": ["j"]}},
        "j": {"kind": "join", "condition": "steps.q.reply == 'Yes, deploy'"}})
    rec = res["steps"]["q"]
    assert rec["state"] == "success" and rec["reply"] == "Yes, deploy" and res["steps"]["j"]["state"] == "success"
    cap, args, target, _ = rt.calls[0]
    assert cap == "voice.speak" and target == "w1" and args["text"] == "Deploy j?"
    assert args["reply"] is True and args["wait"] is True and args["reply_timeout"] == 20
    assert 5 <= args["timeout"] < 120


@pytest.mark.asyncio
async def test_ask_without_reply_fails_and_branches(tmp_path):
    rt = FakeRuntime(tmp_path, caps={"voice.speak": lambda a, w: ok({"reply_state": "none"})})
    res = await execute(rt, {"q": {"kind": "ask", "text": "There?", "on": {"failure": ["f"]}},
                             "f": {"kind": "noop"}})
    assert res["steps"]["q"]["state"] == "failure" and "no answer" in res["steps"]["q"]["error"]
    assert res["steps"]["f"]["state"] == "success"


@pytest.mark.asyncio
async def test_ask_worker_choice(tmp_path):
    workers = {"p1": {"name": "phone", "caps": ["voice.speak"]},
               "p2": {"name": "tablet", "caps": ["voice.speak"]}}
    rt = FakeRuntime(tmp_path, workers=workers, caps={"voice.speak": lambda a, w: ok({"reply": {"text": "y"}})})
    await execute(rt, {"q": {"kind": "ask", "text": "?"}}, settings={"notify_worker": "tablet"}.get)
    await execute(rt, {"q": {"kind": "ask", "text": "?", "worker": "tablet"}})
    await execute(rt, {"q": {"kind": "ask", "text": "?"}})
    assert [c[2] for c in rt.calls] == ["p2", "p2", "p1"]


# -- home chat is unchanged ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_home_chat_and_ask_get_no_job_tools(env):
    env.llm.script = ["hello"]
    out = await env.home.ask("hi")
    assert out["answer"] == "hello"
    body = env.llm.chats()[-1][2]
    assert "tools" not in body                       # home.tools is off by default
    assert env.home.settings.get("tools") is False


def test_jobs_setting_default_agent(tmp_path):
    from rook.hub.plugins.jobs import Jobs
    names = {s.name: s for s in Jobs.SETTINGS}
    assert names["default_agent"].default == "home"


@pytest.mark.asyncio
async def test_hub_tools_check_each_cap_they_call(env):
    """On a real hub node, a hub tool's own cap calls are checked against the
    scope and guardrails too, and run as the agent identity."""
    rt = runtime(env)
    rt.node = env.node                       # the real hub plugins' mcp_tools
    env.llm.script = [call("rook_tool", 1, tool="rook_home_ask", args={"question": "status?"}), finish()]
    await execute(rt, {"a": agent_step(tools=["knowledge.*"])})
    out = tool_results(env.llm)[0]
    assert out["ok"] is False and "outside this step's tools scope" in out["error"]
    assert any(r["cap"] == "home.ask" and r["reply"].get("denied") for r in env.node.journal.rows)
    # In scope: the inner home.ask runs, as the agent on behalf of the job.
    env.llm.script = [call("rook_tool", 1, tool="rook_home_ask", args={"question": "status?"}),
                      "all quiet", finish()]
    await execute(rt, {"a": agent_step()})
    assert tool_results(env.llm)[0]["answer"] == "all quiet"
    asked = env.llm.chats()[-2][2]["messages"][1]["content"]
    assert asked.startswith(f"[{AGENT_ID}/job:j_test/{OWNER['id']}]")
