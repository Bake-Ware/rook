"""Huddle roles (docs/design/huddles.md 3.3, 3.4, 4): role homes and their
per-harness config, role memory in the home and its stdio MCP server, role
turns on the agenthost worker plugin against fake harnesses (cwd = the
home), and the hub ``roles`` plugin (store, ``role.ask``, the role's scoped
``rook_call``, memory and ``role.move`` through the role's host)."""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from rook.worker import rolehome, rolemem
from rook.worker.plugins import agenthost

URL = "https://hub.example/home/turn-mcp"

WORKSPACE_TOOLS = ["workspace_list", "workspace_read", "workspace_write", "workspace_edit",
                   "workspace_search", "workspace_delete"]


# -- role homes ------------------------------------------------------------------------

def _render(home, **kw):
    args = {"role": "qa", "name": "QA engineer", "rev": 3, "mcp_url": URL,
            "tools": ["rook_call", "knowledge_search"], "builtin": [], "memory": "write", **kw}
    return rolehome.render(home, **args)


def test_a_role_home_declares_exactly_the_roles_tools_for_every_harness(tmp_path):
    home = rolehome.ensure(tmp_path, "qa")
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert (home / "memory").is_dir() and (home / rolehome.SCRATCH).is_dir()
    changed = _render(home)
    assert set(changed) == {"role.json", ".mcp.json", ".claude/settings.json",
                            ".codex/config.toml", ".config/opencode/opencode.json"}
    assert _render(home) == []                                   # unchanged: nothing rewritten

    mcp = json.loads((home / ".mcp.json").read_text())["mcpServers"]
    assert mcp["rook"] == {"type": "http", "url": URL,
                           "headers": {"Authorization": "Bearer ${ROOK_TURN_TOKEN}"}}
    assert mcp["memory"]["type"] == "stdio" and mcp["memory"]["command"] == sys.executable
    assert mcp["memory"]["args"][-2:] == ["rook.worker.rolemem", str(home)]
    claude = json.loads((home / ".claude/settings.json").read_text())
    assert claude["permissions"]["allow"] == [
        "mcp__rook__knowledge_search", "mcp__rook__rook_call", "mcp__memory__memory_recall",
        "mcp__memory__memory_remember", "mcp__memory__memory_forget"]
    assert {"Bash", "Edit", "Write", "WebFetch"} <= set(claude["permissions"]["deny"])
    assert claude["autoMemoryEnabled"] is False and claude["enableAllProjectMcpServers"] is False

    codex = tomllib.loads((home / ".codex/config.toml").read_text())
    assert codex["approval_policy"] == "never" and codex["sandbox_mode"] == "read-only"
    assert codex["features"]["shell_tool"] is False and codex["features"]["memories"] is False
    assert codex["mcp_servers"]["rook"]["enabled_tools"] == ["knowledge_search", "rook_call"]
    assert codex["mcp_servers"]["rook"]["bearer_token_env_var"] == "ROOK_TURN_TOKEN"
    assert codex["mcp_servers"]["memory"]["enabled_tools"] == [
        "memory_recall", "memory_remember", "memory_forget"]

    oc = json.loads((home / rolehome.OPENCODE_CONFIG).read_text())
    assert oc["tools"] == {"*": False, "rook_knowledge_search": True, "rook_rook_call": True,
                           "memory_memory_recall": True, "memory_memory_remember": True,
                           "memory_memory_forget": True}
    assert oc["permission"] == {"edit": "deny", "bash": "deny", "webfetch": "deny"}
    assert oc["agent"]["rookrole"]["prompt"] == "{file:../../.rook/system.md}"
    assert oc["mcp"]["rook"]["headers"]["Authorization"] == "Bearer {env:ROOK_TURN_TOKEN}"

    spec = json.loads((home / "role.json").read_text())
    assert spec == {"id": "qa", "name": "QA engineer", "rev": 3, "builtin": [],
                    "memory": "write", "tools": ["knowledge_search", "rook_call"],
                    "workspace": False}
    # Nothing secret is ever written into a home.
    assert "Bearer tok" not in "".join(p.read_text() for p in home.rglob("*") if p.is_file())


def test_memory_mode_and_builtins_change_the_config(tmp_path):
    home = rolehome.ensure(tmp_path, "lead")
    _render(home, role="lead", memory="read", builtin=["read", "search"], tools=[])
    mcp = json.loads((home / ".mcp.json").read_text())["mcpServers"]
    assert mcp["memory"]["args"][-1] == "--read-only"
    allow = json.loads((home / ".claude/settings.json").read_text())["permissions"]["allow"]
    assert allow == ["mcp__memory__memory_recall", "Read", "Grep", "Glob"]
    oc = json.loads((home / rolehome.OPENCODE_CONFIG).read_text())["tools"]
    assert oc["read"] and oc["grep"] and oc["glob"] and oc["list"]
    assert "Read" not in (home / ".codex/config.toml").read_text()   # codex: no file tools
    _render(home, role="lead", memory="off", tools=[])
    assert "memory" not in json.loads((home / ".mcp.json").read_text())["mcpServers"]
    assert "[mcp_servers.memory]" not in (home / ".codex/config.toml").read_text()
    for bad in (dict(tools=["Bad-Name"]), dict(builtin=["shell"]), dict(memory="all")):
        with pytest.raises(rolehome.RoleHomeError):
            _render(home, role="lead", **bad)
    with pytest.raises(rolehome.RoleHomeError):
        rolehome.ensure(tmp_path, "../etc")


def test_a_home_moves_as_a_folder(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    home = rolehome.ensure(a, "qa")
    _render(home)
    rolemem.MemoryDir(home).remember("The staging database is called pelican.")
    (home / rolehome.SCRATCH / "system.md").write_text("host-local")
    pack = rolehome.export(a, "qa")
    got = rolehome.import_(b, "qa", pack["archive"], pack["sha256"])
    assert got["memories"] == 1 and got["role"] == "qa"
    moved = b / "qa"
    assert (moved / ".claude/settings.json").is_file() and (moved / ".mcp.json").is_file()
    assert not (moved / rolehome.SCRATCH / "system.md").exists()    # scratch stays behind
    assert stat.S_IMODE(moved.stat().st_mode) == 0o700
    with pytest.raises(rolehome.RoleHomeError, match="already has a home"):
        rolehome.import_(b, "qa", pack["archive"], pack["sha256"])
    again = rolehome.import_(b, "qa", pack["archive"], pack["sha256"], replace=True)
    assert again["set_aside"].startswith("qa.replaced-")
    with pytest.raises(rolehome.RoleHomeError, match="sha256"):
        rolehome.import_(b, "pm", pack["archive"], "0" * 64)
    aside = rolehome.set_aside(a, "qa")
    assert aside.startswith("qa.moved-") and (a / aside).is_dir() and not (a / "qa").exists()
    assert [h["role"] for h in rolehome.homes(b)] == ["qa"]          # set-aside copies not listed


def test_an_archive_cannot_write_outside_the_home(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("../escape.txt", b"x"), ("/abs.txt", b"x"), ("memory/ok.md", b"ok")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    data = buf.getvalue()
    import hashlib
    rolehome.import_(tmp_path / "roles", "qa", base64.b64encode(data).decode(),
                     hashlib.sha256(data).hexdigest())
    assert (tmp_path / "roles/qa/memory/ok.md").read_text() == "ok"
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "roles/escape.txt").exists()
    assert not (tmp_path / "roles/qa/evil").exists()


# -- role memory -----------------------------------------------------------------------


def test_the_memory_server_speaks_mcp_over_stdio(tmp_path):
    home = tmp_path / "qa"
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "memory_remember", "arguments": {"text": "Deploys happen on Fridays."}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "memory_recall", "arguments": {"query": "deploys"}}}]
    cmd, args, env = rolehome.memory_server(home, "write")
    proc = subprocess.run([cmd, *args], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                          capture_output=True, text=True, timeout=60, cwd=tmp_path,
                          env={**os.environ, **env})
    out = [json.loads(ln) for ln in proc.stdout.splitlines()]
    assert [o["id"] for o in out] == [1, 2, 3, 4], proc.stderr
    assert [t["name"] for t in out[1]["result"]["tools"]] == [
        "memory_recall", "memory_remember", "memory_forget"]
    assert out[2]["result"]["isError"] is False
    found = json.loads(out[3]["result"]["content"][0]["text"])["memories"]
    assert found[0]["text"] == "Deploys happen on Fridays."
    ro = rolemem.handle(rolemem.MemoryDir(home), {"jsonrpc": "2.0", "id": 5,
                        "method": "tools/call", "params": {"name": "memory_forget",
                                                           "arguments": {"id": found[0]["id"]}}},
                        read_only=True)
    assert ro["result"]["isError"]                                 # read-only: no forgetting
    assert [t["name"] for t in rolemem.tools(True)] == ["memory_recall"]


# -- role turns on the agent host ------------------------------------------------------

FAKE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
name = os.path.basename(sys.argv[0])
if args[:1] == ["--version"]:
    print(name + " 9.9.9"); sys.exit(0)
if args[:2] in (["auth", "status"], ["login", "status"]):
    print(json.dumps({{"loggedIn": True}}) if name == "claude" else "Logged in"); sys.exit(0)
prompt = sys.stdin.read()
rec = {{"name": name, "argv": args, "prompt": prompt, "cwd": os.getcwd(),
       "token": os.environ.get("ROOK_TURN_TOKEN"), "xdg": os.environ.get("XDG_CONFIG_HOME")}}
if name == "opencode":
    rec["system"] = open(os.path.join(".rook", "system.md")).read()
    rec["config"] = os.environ.get("OPENCODE_CONFIG")
    rec["global"] = json.load(open(os.path.join(os.environ["XDG_CONFIG_HOME"], "opencode",
                                                "opencode.json")))
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\n")
def out(ev):
    print(json.dumps(ev), flush=True)
if name == "claude":
    out({{"type": "assistant", "message": {{"model": "m", "content": [
        {{"type": "tool_use", "name": "mcp__memory__memory_recall"}},
        {{"type": "tool_use", "name": "mcp__rook__rook_call"}}]}}}})
    out({{"type": "result", "is_error": False, "result": "qa here", "usage": {{}}}})
elif name == "codex":
    out({{"type": "item.started", "item": {{"type": "mcp_tool_call", "server": "rook",
                                          "tool": "rook_call"}}}})
    out({{"type": "item.completed", "item": {{"type": "agent_message", "text": "codex qa"}}}})
    out({{"type": "turn.completed", "usage": {{}}}})
else:
    out({{"type": "tool_use", "part": {{"tool": "rook_rook_call"}}}})
    out({{"type": "text", "part": {{"text": "opencode qa"}}}})
    out({{"type": "step_finish", "part": {{"reason": "stop"}}}})
'''


def _role_turn(**kw):
    return {"turn_id": "r1", "prompt": "is the backup ok?", "system": "You are QA.",
            "mcp_url": URL, "mcp_token": "tok-123", "role": "qa", "role_name": "QA",
            "role_rev": 2, "tools": ["rook_call"], "builtin": [], "memory": "write", **kw}


def _last(host):
    return json.loads(host.fake_log.read_text().splitlines()[-1])


# -- the role store --------------------------------------------------------------------


# -- the hub plugin --------------------------------------------------------------------


