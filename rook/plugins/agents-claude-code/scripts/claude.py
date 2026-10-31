"""agents-claude-code.turn: one agent turn in the Claude Code CLI.

``claude -p --output-format json [--resume ID] [--model M]
[--append-system-prompt S] ...`` with the prompt on stdin, started through
the worker's launch path (as the normal user). The JSON result carries the
reply (``result``), the session id to resume and token usage.

``door``: the turn token the agents runtime got from the hub for this turn
(the agent, for the person behind it). The turn's Rook MCP server is the
door at ``door_url`` with that token, written to a 0600 config file in the
call's temp folder (gone when the call ends), so every Rook call the agent
makes is journaled as the agent and held to its own grants.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from rook.core2.worker.security import launch
from rook.kit._types import parse_duration

# A session id or model name, never an option: ``--resume`` takes an optional
# value, so "--dangerously-skip-permissions" there would be parsed as a flag.
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}")
USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def argv(settings, *, session=None, model=None, system=None, door_config=None) -> list[str]:
    out = [settings["claude"] or "claude", "-p", "--output-format", "json"]
    if session:
        out += ["--resume", session]
    if model == "three":                 # System Three (dream cycles); blank or two: System Two
        model = settings["model_three"]
    elif not model or model == "two":
        model = settings["model"]
    if model:
        out += ["--model", model]
    if system:
        out += ["--append-system-prompt", system]
    if settings["permission_mode"]:
        out += ["--permission-mode", settings["permission_mode"]]
    if settings["allowed_tools"]:
        out += ["--allowedTools", ",".join(settings["allowed_tools"])]
    configs = [str(c) for c in (settings["mcp_config"], door_config) if c]
    if configs:
        out += ["--mcp-config", *configs]
    if settings["isolate"]:          # not the person's own hooks, plugins and MCP servers (another band's, say)
        out += ["--setting-sources", "project,local", "--strict-mcp-config"]
    return out


def door_config(ctx, token: str) -> Path:
    """The turn's Rook MCP server: the door at ``door_url`` with ``token``."""
    url = ctx.settings["door_url"]
    if not url:
        raise ctx.Error("set agents-claude-code.door_url to the hub's MCP door to give agents Rook")
    path = ctx.tmp / "rook-mcp.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"rook": {"type": "http", "url": url,
                                           "headers": {"Authorization": f"Bearer {token}"}}}}, f)
    return path


async def turn(ctx, prompt, session=None, cwd=None, model=None, effort=None, system=None, timeout="20m",
               door=None):
    work = Path(cwd).expanduser() if cwd else ctx.data / "work"
    if not cwd:
        work.mkdir(parents=True, exist_ok=True)
    elif not work.is_dir():
        raise ctx.Error(f"no folder {work}")
    for name, value in (("session", session), ("model", model)):
        if value and not _WORD.fullmatch(value):
            raise ctx.Error(f"bad args: {name} is an id or a name, not {value[:40]!r}")
    cmd = argv(ctx.settings, session=session, model=model, system=system,
               door_config=door_config(ctx, door) if door else None)
    try:
        proc = await launch(cmd, getattr(ctx, "lease", None), cwd=str(work), stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise ctx.Error(f"{cmd[0]} not found: install Claude Code or set agents-claude-code.claude") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), parse_duration(timeout))
    except asyncio.TimeoutError:
        raise ctx.Error(f"Claude Code took longer than {timeout}", retry=True) from None
    finally:
        if proc.returncode is None:      # timed out or the call was cancelled: no orphan turn
            proc.kill()
            await proc.wait()
    return parse(ctx, proc.returncode, out, err)


def parse(ctx, code, out: bytes, err: bytes) -> dict:
    text = out.decode("utf-8", "replace").strip()
    try:
        res = json.JSONDecoder().raw_decode(text, text.index("{"))[0] if "{" in text else None
    except ValueError:
        res = None
    if not isinstance(res, dict):
        tail = (err.decode("utf-8", "replace").strip() or text)[-500:]
        raise ctx.Error(f"Claude Code failed (exit {code}): {tail or 'no output'}")
    if res.get("is_error") or res.get("subtype", "success") != "success":
        raise ctx.Error(f"Claude Code: {res.get('result') or res.get('subtype') or 'error'}")
    if not isinstance(res.get("result"), str) or not res.get("session_id"):
        raise ctx.Error("Claude Code answered without a reply or a session id")
    usage = {k: v for k, v in (res.get("usage") or {}).items() if k in USAGE and isinstance(v, int)}
    return {"text": res["result"], "session": res["session_id"], **({"usage": usage} if usage else {})}
