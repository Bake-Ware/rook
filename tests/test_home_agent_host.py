"""The home agent on an agent harness (docs/design/huddles.md 3): the
per-turn tool server, the worker plugin ``agenthost`` against fake
``claude``, ``codex`` and ``opencode`` binaries, and the home plugin routing
chat, home.ask, job steps and the page's Test and models through an agent
host."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from types import SimpleNamespace

import pytest

from rook.worker.plugins import agenthost

# -- the per-turn tool server ----------------------------------------------------------

KNOWLEDGE = {"type": "function", "function": {
    "name": "knowledge_search", "description": "Search.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}


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
                        "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
                        "token": os.environ.get("ROOK_TURN_TOKEN")}}) + "\n")
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
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)   # CI runners set it
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
        "headers": {"Authorization": "Bearer ${ROOK_TURN_TOKEN}"}}
    assert seen["token"] == "tok-123"                      # from the environment only
    assert "tok-123" not in " ".join(argv) and seen["oauth"] == "oat-secret"
    # The token file is gone after the turn.
    assert not list(host.data_dir.glob("turn-*"))
    # A retry with the same turn id gets the same reply without a second run.
    again = await host._turn(**_turn())
    assert again == out and len(host.log.read_text().splitlines()) == 1
    st = await host._status()
    assert st["harnesses"]["claude"] == {"version": "9.9.9", "logged_in": True}
    assert host.heartbeat()["harnesses"]["claude"]["version"] == "9.9.9"


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
    await host.stop()           # the re-read a failed turn starts must end inside the test


@pytest.mark.asyncio
async def test_agenthost_probe_hides_console_windows(monkeypatch):
    """The 5-minute version/login probe runs under pythonw.exe on Windows; each
    harness it starts must get CREATE_NO_WINDOW or the desktop flashes."""
    from rook.core import pluginkit
    seen = []

    async def fake_exec(*argv, **kwargs):
        seen.append(kwargs)
        raise OSError("not really started")
    monkeypatch.setattr(pluginkit, "is_windows", lambda: True)
    monkeypatch.setattr(agenthost.asyncio, "create_subprocess_exec", fake_exec)
    assert await agenthost.AgentHost._run_cmd(["claude", "--version"]) == (127, "")
    assert seen and seen[0]["creationflags"] == pluginkit.CREATE_NO_WINDOW


# -- the home plugin on the Claude Code backend ----------------------------------------


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


# -- codex and opencode ------------------------------------------------------------------

FAKE_HARNESS = r"""#!{python}
import json, os, sys
args = sys.argv[1:]
name = os.path.basename(sys.argv[0])
if args[:1] == ["--version"]:
    print(name + " 1.2.3"); sys.exit(0)
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
if args[:2] == ["debug", "models"]:
    print(json.dumps({{"models": [{{"slug": "gpt-a", "visibility": "list"}},
                                 {{"slug": "gpt-hidden", "visibility": "hide"}}]}})); sys.exit(0)
if args[:1] == ["models"]:
    print("kaiju/qwopus\nopencode/big-pickle"); sys.exit(0)
prompt = sys.stdin.read()
cfg = None
if name == "opencode":
    cfg = json.load(open(os.path.join(os.environ["XDG_CONFIG_HOME"], "opencode", "opencode.json")))
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({{"name": name, "argv": args, "prompt": prompt, "cfg": cfg,
                        "token": os.environ.get("ROOK_TURN_TOKEN")}}) + "\n")
def out(ev):
    print(json.dumps(ev), flush=True)
if name == "codex":
    out({{"type": "thread.started", "thread_id": "t"}})
    out({{"type": "item.started", "item": {{"type": "mcp_tool_call", "server": "home",
                                          "tool": "knowledge_search"}}}})
    out({{"type": "item.completed", "item": {{"type": "agent_message", "text": "codex says hi"}}}})
    out({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 2}}}})
else:
    out({{"type": "tool_use", "part": {{"tool": "home_knowledge_search"}}}})
    out({{"type": "text", "part": {{"text": "summary", "synthetic": True}}}})
    out({{"type": "text", "part": {{"text": "opencode says hi"}}}})
    out({{"type": "step_finish", "part": {{"reason": "stop", "tokens": {{"input": 5, "output": 3}},
                                         "cost": 0}}}})
"""


@pytest.fixture
def harnesses(host, tmp_path):
    for name in ("codex", "opencode"):
        exe = tmp_path / "bin" / name
        exe.write_text(FAKE_HARNESS.format(python=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    conf = tmp_path / "home" / ".config" / "opencode"
    conf.mkdir(parents=True)
    (conf / "opencode.jsonc").write_text("""{
      // my setup
      "model": "kaiju/qwopus",
      "provider": {"kaiju": {"options": {"baseURL": "http://kaiju:1234/v1"}}},
      "mcp": {"rook": {"type": "remote", "url": "https://x/mcp", "headers": {"Authorization": "Bearer SECRET"}}},
      "plugin": ["something"],
    }""")
    return host


@pytest.mark.asyncio
async def test_codex_turn_is_locked_down(harnesses):
    host = harnesses
    assert set(host.installed()) == {"claude", "codex", "opencode"}
    out = await host._turn(**_turn(turn_id="c1", harness="codex", model="gpt-a", effort="low"))
    assert out["ok"], out
    assert out["text"] == "codex says hi" and out["tools_used"] == ["knowledge_search"]
    assert out["harness"] == "codex" and out["usage"]["input_tokens"] == 10
    seen = json.loads(host.log.read_text().splitlines()[-1])
    argv = seen["argv"]
    assert argv[:2] == ["exec", "--json"] and argv[-1] == "-"
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules"):
        assert flag in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    joined = " ".join(argv)
    for c in ('approval_policy="never"', "features.shell_tool=false", "include_apply_patch_tool=false",
              'mcp_servers.home.bearer_token_env_var="ROOK_TURN_TOKEN"',
              'mcp_servers.home.default_tools_approval_mode="approve"',
              'model_reasoning_effort="low"', 'developer_instructions="You are home."'):
        assert c in joined, c
    assert "tok-123" not in joined and seen["token"] == "tok-123" and seen["prompt"] == "hi"
    assert (await host._models("codex"))["models"] == ["gpt-a"]


@pytest.mark.asyncio
async def test_opencode_turn_gets_only_its_providers(harnesses):
    host = harnesses
    out = await host._turn(**_turn(turn_id="o1", harness="opencode", model="kaiju/qwopus"))
    assert out["ok"], out
    assert out["text"] == "opencode says hi" and out["tools_used"] == ["knowledge_search"]
    seen = json.loads(host.log.read_text().splitlines()[-1])
    cfg = seen["cfg"]
    assert cfg["provider"] == {"kaiju": {"options": {"baseURL": "http://kaiju:1234/v1"}}}
    assert list(cfg["mcp"]) == ["home"] and "plugin" not in cfg        # never the user's
    assert cfg["mcp"]["home"]["headers"]["Authorization"] == "Bearer {env:ROOK_TURN_TOKEN}"
    assert cfg["tools"] == {"*": False, "home_*": True}
    assert cfg["agent"]["rookhome"]["prompt"] == "You are home."
    assert cfg["permission"]["bash"] == "deny"
    assert seen["argv"][:5] == ["run", "--format", "json", "--pure", "--agent"]
    assert "SECRET" not in json.dumps(seen) and seen["token"] == "tok-123"
    assert (await host._models("opencode"))["models"] == ["kaiju/qwopus", "opencode/big-pickle"]


def test_agenthost_turns_itself_on_when_a_harness_is_installed(host, monkeypatch):
    monkeypatch.delenv("ROOK_AGENTHOST", raising=False)
    plugin = agenthost.AgentHost()
    assert plugin.available()                                # claude is on PATH
    monkeypatch.setenv("ROOK_AGENTHOST", "0")
    assert not plugin.available()


def test_harness_home_is_where_the_sign_in_lives(host, monkeypatch, tmp_path):
    monkeypatch.setattr(agenthost, "HARNESS_HOME", "")
    assert agenthost._harness_env() == {"HOME": os.environ["HOME"]}
    monkeypatch.setenv("ROOK_AGENTHOST_HARNESS_HOME", str(tmp_path / "signed-in"))
    agenthost.AgentHost().available()
    env = agenthost._harness_env()
    assert env == {"HOME": str(tmp_path / "signed-in"),
                   "XDG_CONFIG_HOME": str(tmp_path / "signed-in" / ".config")}


def test_strip_jsonc():
    assert json.loads(agenthost.strip_jsonc('{"a": "x // not a comment", /* c */ "b": [1,],}')) \
        == {"a": "x // not a comment", "b": [1]}


