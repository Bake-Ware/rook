"""The home agent's Claude Code backend (docs/design/huddles.md 3): the
per-turn tool server, the worker plugin ``agenthost`` against a fake
``claude`` binary, and the home plugin routing chat, home.ask, job steps and
the page's Test through an agent host."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from types import SimpleNamespace

import pytest

from rook.hub.plugins.home import claude_code, turn_mcp
from rook.hub.plugins.home.claude_code import flatten
from rook.worker.plugins import agenthost
from tests.test_home_agent import FakeLLM, FakeVault  # noqa: F401 - shared fakes

# -- the per-turn tool server ----------------------------------------------------------

KNOWLEDGE = {"type": "function", "function": {
    "name": "knowledge_search", "description": "Search.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}


@pytest.mark.asyncio
async def test_turn_server_speaks_mcp_for_one_turn():
    calls = []

    async def handler(name, args):
        calls.append((name, args))
        return json.dumps({"results": ["lights on a timer"]})
    reg = turn_mcp.Registry()
    token, turn = reg.open([KNOWLEDGE], handler, 30)
    assert reg.get(token) is turn and reg.get("nope") is None
    init = await turn_mcp.handle(turn, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {}})
    assert init["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert await turn_mcp.handle(turn, {"jsonrpc": "2.0", "method":
                                        "notifications/initialized"}) is None
    tools = (await turn_mcp.handle(turn, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    assert tools["result"]["tools"] == [{"name": "knowledge_search", "description": "Search.",
                                         "inputSchema": KNOWLEDGE["function"]["parameters"]}]
    res = await turn_mcp.handle(turn, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "knowledge_search",
                                                  "arguments": {"query": "lights"}}})
    assert res["result"]["isError"] is False and "timer" in res["result"]["content"][0]["text"]
    other = await turn_mcp.handle(turn, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                         "params": {"name": "rook_call", "arguments": {}}})
    assert other["result"]["isError"] and calls == [("knowledge_search", {"query": "lights"})]
    assert turn.used == ["knowledge_search"]
    reg.close(token)
    assert reg.get(token) is None


def test_turn_tokens_expire():
    now = [1000.0]
    reg = turn_mcp.Registry(clock=lambda: now[0])

    async def handler(name, args):
        return ""
    token, _ = reg.open([], handler, 10)
    now[0] += 10 + turn_mcp.GRACE_S + 1
    assert reg.get(token) is None and len(reg) == 0


def test_turn_route_needs_the_turn_token():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    reg = turn_mcp.Registry()

    async def handler(name, args):
        return "ok"
    token, _ = reg.open([KNOWLEDGE], handler, 30)
    client = TestClient(Starlette(routes=turn_mcp.routes(lambda: reg)))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert client.post(turn_mcp.PATH, json=body).status_code == 401
    assert client.post(turn_mcp.PATH, json=body,
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.post(turn_mcp.PATH, json=body, headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200 and ok.json()["result"]["tools"][0]["name"] == "knowledge_search"
    note = client.post(turn_mcp.PATH, json={"jsonrpc": "2.0", "method": "notifications/x"},
                       headers={"Authorization": f"Bearer {token}"})
    assert note.status_code == 202
    assert client.get(turn_mcp.PATH).status_code == 405


def test_flatten_a_conversation():
    sys_, prompt = flatten([{"role": "system", "content": "Be brief."},
                            {"role": "user", "content": "hello"}])
    assert sys_ == "Be brief." and prompt == "hello"
    sys_, prompt = flatten([{"role": "system", "content": "S"},
                            {"role": "user", "content": "[user:operator] hi"},
                            {"role": "assistant", "content": "Hello!"},
                            {"role": "user", "content": "[user:operator] what's on deck?"}])
    assert "[user:operator] hi" in prompt and "You: Hello!" in prompt
    assert prompt.index("The message to answer now") < prompt.index("what's on deck?")


# -- the agenthost worker plugin against a fake claude ---------------------------------

FAKE_CLAUDE = r'''#!{python}
import json, os, sys, time
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)"); sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True}})); sys.exit(0)
prompt = sys.stdin.read()
cfg = json.load(open(args[args.index("--mcp-config") + 1]))
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({{"argv": args, "prompt": prompt, "mcp": cfg,
                        "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}}) + "\n")
def out(ev):
    print(json.dumps(ev), flush=True)
out({{"type": "system", "subtype": "init", "model": "claude-haiku-test", "session_id": "s1"}})
if "SLEEP" in prompt:
    time.sleep(30)
if "TOOLS" in prompt:
    for i in range(5):
        out({{"type": "assistant", "message": {{"model": "claude-haiku-test", "content": [
            {{"type": "tool_use", "name": "mcp__home__knowledge_search", "input": {{}}}}]}}}})
        time.sleep(0.05)
if "FAIL" in prompt:
    out({{"type": "result", "is_error": True, "result": "model refused"}}); sys.exit(1)
out({{"type": "assistant", "message": {{"model": "claude-haiku-test", "content": [
    {{"type": "text", "text": "Hello from fake"}}]}}}})
out({{"type": "result", "subtype": "success", "is_error": False, "result": "Hello from fake",
     "num_turns": 1, "total_cost_usd": 0.0001, "duration_api_ms": 12,
     "usage": {{"input_tokens": 3, "output_tokens": 4}}}})
'''


@pytest.fixture
def host(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE_CLAUDE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    plugin = agenthost.AgentHost()
    plugin.__dict__["_data_root"] = str(tmp_path / "state")
    plugin.log = tmp_path / "claude.log"
    return plugin


def _turn(**kw):
    return {"turn_id": "t1", "prompt": "hi", "system": "You are home.",
            "mcp_url": "https://hub.example/home/turn-mcp", "mcp_token": "tok-123", **kw}


@pytest.mark.asyncio
async def test_agenthost_runs_a_locked_down_turn(host):
    assert host.available()
    out = await host._turn(**_turn(model="haiku", oauth_token="oat-secret"))
    assert out["ok"], out
    assert out["text"] == "Hello from fake" and out["model"] == "claude-haiku-test"
    assert out["usage"] == {"input_tokens": 3, "output_tokens": 4} and out["cost_usd"] == 0.0001
    seen = json.loads(host.log.read_text().splitlines()[-1])
    argv = seen["argv"]
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__home"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--system-prompt") + 1] == "You are home."
    assert seen["prompt"] == "hi" and "hi" not in argv              # stdin, not argv
    assert seen["mcp"]["mcpServers"]["home"] == {
        "type": "http", "url": "https://hub.example/home/turn-mcp",
        "headers": {"Authorization": "Bearer tok-123"}}
    assert "tok-123" not in " ".join(argv) and seen["oauth"] == "oat-secret"
    # The token file is gone after the turn.
    assert not list(host.data_dir.glob("turn-*"))
    # A retry with the same turn id gets the same reply without a second run.
    again = await host._turn(**_turn())
    assert again == out and len(host.log.read_text().splitlines()) == 1
    st = await host._status()
    assert st["claude"] == "9.9.9" and st["logged_in"] is True


@pytest.mark.asyncio
async def test_agenthost_bounds_tools_time_and_errors(host):
    tools = await host._turn(**_turn(turn_id="t2", prompt="TOOLS", max_tool_calls=2))
    assert not tools["ok"] and "2 tool calls" in tools["error"]
    t0 = time.monotonic()
    slow = await host._turn(**_turn(turn_id="t3", prompt="SLEEP", timeout_s=5))
    assert not slow["ok"] and "timed out" in slow["error"] and time.monotonic() - t0 < 15
    bad = await host._turn(**_turn(turn_id="t4", prompt="FAIL"))
    assert not bad["ok"] and "model refused" in bad["error"]
    for kw, msg in ((dict(system=""), "system"), (dict(model="x y"), "model"),
                    (dict(turn_id="../x"), "turn_id"), (dict(mcp_url="file:///x"), "mcp_url")):
        with pytest.raises(Exception, match=msg):
            await host._turn(**_turn(**kw))


# -- the home plugin on the Claude Code backend ----------------------------------------

class FakeAgentHost:
    """The band as the hub sees it: one agent host. A turn's 'model' calls the
    hub's turn tools through the real turn server, then answers."""

    def __init__(self, home, name="cachyrig", script=None):
        self.home = home
        self.workers = {"w-cachy": {"name": name, "caps": ["agenthost.turn"],
                                    "hb": {"agenthost": {"claude": "2.1", "logged_in": True}},
                                    "last_seen": time.time()}}
        self.script = list(script or [])
        self.calls = []

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None,
                   principal=None):
        self.calls.append(SimpleNamespace(cap=cap, args=args, target=target, identity=identity,
                                          principal=principal, timeout=timeout))
        if cap != "agenthost.turn":
            return {"ok": True, "result": {"ok": True}}
        turn = self.home.claude.registry.get(args["mcp_token"])
        assert turn is not None, "the turn's tools must be registered during the call"
        step = self.script.pop(0) if self.script else {"text": "hi from claude"}
        for name, targs in step.get("tools", []):
            r = await turn_mcp.handle(turn, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": name, "arguments": targs}})
            step.setdefault("seen", []).append(r["result"]["content"][0]["text"])
        if step.get("fail"):
            return {"ok": True, "result": {"ok": False, "error": step["fail"]}}
        return {"ok": True, "result": {"ok": True, "text": step.get("text", ""),
                                       "model": "claude-haiku-test", "usage": {"output_tokens": 5},
                                       "cost_usd": 0.001}}


@pytest.fixture
def cenv(tmp_path, monkeypatch):
    import rook.hub.plugins.settings as settings_plugin
    from rook.band_mcp.chat_rooms import ChatStore
    from rook.hub.node import HubNode
    from rook.hub.settings_store import SettingsStore
    monkeypatch.setattr(settings_plugin, "_admin_gate", lambda what: None)
    node = HubNode(str(tmp_path), entry_points=False,
                   vault=FakeVault({"cc-token": "oat-xyz", "llm-key": "sk-1234"}),
                   chat=ChatStore(str(tmp_path / "chat.db")),
                   settings_store=SettingsStore(tmp_path / "settings.db"))
    node.journal = SimpleNamespace(rows=[])
    node.journal.record = lambda **kw: node.journal.rows.append(kw)
    home = node.plugin("home")
    home._http = FakeLLM(["from the openai endpoint"])
    band = FakeAgentHost(home)
    monkeypatch.setattr(node, "client", band, raising=False)
    home.claude.public_url = "https://mcp.example.com"
    for k, v in {"enabled": True, "backend": "claude-code", "claude_worker": "cachyrig"}.items():
        node.settings.set(f"home.{k}", v, actor="human:op")
    return SimpleNamespace(node=node, home=home, band=band, svc=node.settings)


@pytest.mark.asyncio
async def test_home_ask_runs_on_the_agent_host(cenv):
    st = cenv.home.status()
    assert st["backend"] == "claude-code" and st["missing"] == [] and st["model"] == "haiku"
    assert st["claude"]["hosts"][0]["name"] == "cachyrig"
    res = await cenv.node.dispatch("home.ask", {"question": "hello?"}, "agent:claude")
    assert res["ok"], res
    assert res["result"]["answer"] == "hi from claude"
    assert res["result"]["model"] == "claude-haiku-test"
    (call,) = cenv.band.calls
    assert call.cap == "agenthost.turn" and call.target == "w-cachy"
    assert call.args["model"] == "haiku" and call.args["mcp_url"] == \
        "https://mcp.example.com/home/turn-mcp"
    assert call.principal.kind == "system" and call.identity == "system:home.home"
    assert "hello?" in call.args["prompt"] and "home agent" in call.args["system"]
    assert "oauth_token" not in call.args
    assert len(cenv.home.claude.registry) == 0           # the token died with the turn


@pytest.mark.asyncio
async def test_tools_come_back_to_the_hub_as_the_home_agent(cenv, monkeypatch):
    cenv.svc.set("home.tools", True, actor="human:op")
    seen = []

    async def invoke(cap, args, identity=None):
        seen.append((cap, args, identity))
        return {"results": [{"slug": "lights", "excerpt": "On a timer."}]}
    monkeypatch.setattr(cenv.node, "invoke", invoke)
    monkeypatch.setattr(cenv.home, "_has_knowledge", lambda: True)
    step = {"tools": [("knowledge_search", {"query": "lights"})], "text": "On a timer."}
    cenv.band.script = [step]
    res = await cenv.node.dispatch("home.ask", {"question": "lights?"}, "agent:claude")
    assert res["ok"] and res["result"]["tools_used"] == ["knowledge_search"]
    assert seen == [("knowledge.read", {"action": "search", "query": "lights"}, "agent:home")]
    assert "timer" in step["seen"][0]


@pytest.mark.asyncio
async def test_sign_in_token_comes_from_the_vault(cenv):
    cenv.svc.set("home.claude_auth", "token", actor="human:op")
    cenv.svc.set("home.claude_token", "{{secret:cc-token}}", actor="human:op")
    await cenv.node.dispatch("home.ask", {"question": "x"}, "agent:claude")
    assert cenv.band.calls[-1].args["oauth_token"] == "oat-xyz"


@pytest.mark.asyncio
async def test_no_host_falls_back_or_fails(cenv):
    cenv.band.workers["w-cachy"]["last_seen"] = 0          # offline
    res = await cenv.node.dispatch("home.ask", {"question": "x"}, "agent:claude")
    assert not res["ok"]                                    # no OpenAI endpoint to fall back to
    for k, v in {"base_url": "http://llm.example/v1", "model": "small",
                 "api_key": "{{secret:llm-key}}"}.items():
        cenv.svc.set(f"home.{k}", v, actor="human:op")
    res = await cenv.node.dispatch("home.ask", {"question": "x"}, "agent:claude")
    assert res["ok"] and res["result"]["answer"] == "from the openai endpoint"
    cenv.svc.set("home.claude_on_unavailable", "fail", actor="human:op")
    res = await cenv.node.dispatch("home.ask", {"question": "x"}, "agent:claude")
    assert not res["ok"]
    # A fallback host takes over.
    cenv.band.workers["w-other"] = {"name": "sojourn", "caps": ["agenthost.turn"], "hb": {},
                                    "last_seen": time.time()}
    cenv.svc.set("home.claude_fallback_workers", ["sojourn"], actor="human:op")
    res = await cenv.node.dispatch("home.ask", {"question": "x"}, "agent:claude")
    assert res["ok"] and cenv.band.calls[-1].target == "w-other"


@pytest.mark.asyncio
async def test_a_failed_turn_is_an_error(cenv):
    cenv.band.script = [{"fail": "claude exited 1"}]
    res = await cenv.node.dispatch("home.ask", {"question": "x"}, "agent:claude")
    assert not res["ok"]


class FakeJobBackend:
    def __init__(self):
        self.calls = []

    def tool_names(self):
        return ["rook_workers", "rook_call", "finish"]

    async def workers(self):
        return [{"name": "soundwave"}]

    async def call(self, worker, cap, args):
        self.calls.append((worker, cap, args))
        return {"ok": True, "result": "up"}


@pytest.mark.asyncio
async def test_job_step_on_claude_code(cenv):
    jb = FakeJobBackend()
    cenv.band.script = [{"tools": [("rook_call", {"worker": "soundwave", "cap": "info.uptime"}),
                                   ("finish", {"verdict": "ok", "text": "Uptime checked."})],
                         "text": "Done."}]
    out = await cenv.home.work_job_step("Check uptime", None, jb, job="j1", run="r1", step="s")
    assert out["verdict"] == "ok" and out["text"] == "Uptime checked."
    assert jb.calls == [("soundwave", "info.uptime", {})] and out["tools_used"] == ["rook_call"]
    call = cenv.band.calls[-1]
    assert call.args["timeout_s"] == 1800.0
    # The budget holds: one call allowed, the second is refused.
    cenv.band.script = [{"tools": [("rook_call", {"worker": "a", "cap": "x"}),
                                   ("rook_call", {"worker": "b", "cap": "y"})],
                         "text": "VERDICT: failed ran out"}]
    jb.calls.clear()
    out = await cenv.home.work_job_step("x", None, jb, max_calls=1)
    assert len(jb.calls) == 1 and out["verdict"] == "failed"


@pytest.mark.asyncio
async def test_page_test_and_models_for_claude_code(cenv):
    svc = cenv.svc
    models = await cenv.home.page_action(svc, {"action": "home_models", "values": {}}, "human:op")
    assert models["models"] == list(claude_code.MODELS) and models["hosts"][0]["name"] == "cachyrig"
    res = await cenv.home.page_action(svc, {"action": "home_test",
                                            "values": {"claude_model": "sonnet"}}, "human:op")
    assert res["ok"] and res["reply"] == "hi from claude" and res["worker"] == "cachyrig"
    assert cenv.band.calls[-1].args["model"] == "sonnet"
    # A token reference typed into the form is never resolved.
    await cenv.home.page_action(svc, {"action": "home_test", "values": {
        "claude_auth": "token", "claude_token": "{{secret:cc-token}}"}}, "human:op")
    assert "oauth_token" not in cenv.band.calls[-1].args
    page = cenv.home.page(svc)
    assert "claude-code" in page["backends"] and page["claude_hosts"]


@pytest.mark.asyncio
async def test_chat_reply_on_claude_code(cenv):
    store = cenv.node.chat
    room = store.start("lounge", "user:operator", ["agent:home"])["room"]
    await cenv.home.tick()                                    # baseline
    store.send(room, "user:operator", "hello home", [], False)
    tasks = await cenv.home.tick()
    await asyncio.gather(*tasks)
    msgs = store.read(room, "user:operator")["messages"]
    assert msgs[-1]["sender"] == "agent:home" and msgs[-1]["text"] == "hi from claude"
    assert "hello home" in cenv.band.calls[-1].args["prompt"]
