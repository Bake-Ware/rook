"""agents-claude-code.turn: one agent turn in the Claude Code CLI, run from
the hub on the worker named by the ``worker`` setting.

``claude -p --output-format json [--resume ID] [--model M]
[--append-system-prompt S] ...`` with the prompt on stdin, a process on that
worker (shell.open), ended when the turn times out or is cancelled. The JSON
result carries the reply (``result``), the session id to resume and token
usage.

``door``: the turn's door token by name (``{{secret:turn-...}}``), from the
agents runtime. It goes on untouched as ``ROOK_DOOR_TOKEN`` in the process's
environment; the hub puts the token there on the way to the worker. The
turn's Rook MCP server is the door at ``door_url``, from an MCP config on the
worker (``~/.rook-plugins/agents-claude-code/rook-mcp.json``) that names that
variable, so no file and no command line holds the token.
"""
from __future__ import annotations

import json
import re

from rook.kit.cli import Place, itself, stream, worker

# A session id or model name, never an option: ``--resume`` takes an optional
# value, so "--dangerously-skip-permissions" there would be parsed as a flag.
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}")
USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
TOKEN_ENV = "ROOK_DOOR_TOKEN"
SERVER = "rook"


def argv(settings, *, program="claude", session=None, model=None, system=None, door_config=None) -> list[str]:
    out = [program, "-p", "--output-format", "json"]
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
    allowed = list(settings["allowed_tools"] or ())
    if door_config and not any(t == f"mcp__{SERVER}" or t.startswith(f"mcp__{SERVER}__") for t in allowed):
        allowed.append(f"mcp__{SERVER}")         # a turn given Rook may use it
    if allowed:
        out += ["--allowedTools", ",".join(allowed)]
    configs = [str(c) for c in (settings["mcp_config"], door_config) if c]
    if configs:
        out += ["--mcp-config", *configs]
    if settings["isolate"]:          # not the person's own hooks, plugins and MCP servers (another band's, say)
        out += ["--setting-sources", "project,local", "--strict-mcp-config"]
    return out


def door_config(url: str) -> str:
    """The turn's Rook MCP server: the door at ``url``, the token from the environment."""
    return json.dumps({"mcpServers": {SERVER: {"type": "http", "url": url,
                                               "headers": {"Authorization": "Bearer ${%s}" % TOKEN_ENV}}}})


async def turn(ctx, prompt, session=None, cwd=None, model=None, effort=None, system=None, timeout="20m",
               door=None):
    for name, value in (("session", session), ("model", model)):
        if value and not _WORD.fullmatch(value):
            raise ctx.Error(f"bad args: {name} is an id or a name, not {value[:40]!r}")
    if door and not ctx.settings["door_url"]:
        raise ctx.Error("set agents-claude-code.door_url to the hub's MCP door to give agents Rook")
    ctx = itself(ctx)
    w = worker(ctx)
    place = await Place.of(ctx, w)
    work = place.expand(cwd) if cwd else await place.folder("work")
    config = None
    if door:
        config = place.join(await place.folder(), "rook-mcp.json")
        await ctx.call_on(w, "file.write", path=config, content=door_config(ctx.settings["door_url"]))
    cmd = argv(ctx.settings, program=place.expand(ctx.settings["claude"] or "claude"), session=session,
               model=model, system=system, door_config=config)
    seen: list[dict] = []
    code, tail = await stream(ctx, w, cmd, stdin=prompt, cwd=work, timeout=timeout, name="Claude Code",
                              setting="agents-claude-code.claude", env={TOKEN_ENV: door} if door else None,
                              line=lambda text: _json(text, seen))
    return parse(ctx, code, seen, tail)


def _json(text: str, seen: list) -> None:
    if text.startswith("{"):
        try:
            got = json.loads(text)
        except ValueError:
            return
        if isinstance(got, dict):
            seen.append(got)


def parse(ctx, code, seen: list, tail: str) -> dict:
    res = next((r for r in reversed(seen) if "result" in r or "subtype" in r), None)
    if res is None:
        raise ctx.Error(f"Claude Code failed (exit {code}): {tail.strip()[-500:] or 'no output'}")
    if res.get("is_error") or res.get("subtype", "success") != "success":
        raise ctx.Error(f"Claude Code: {res.get('result') or res.get('subtype') or 'error'}")
    if not isinstance(res.get("result"), str) or not res.get("session_id"):
        raise ctx.Error("Claude Code answered without a reply or a session id")
    usage = {k: v for k, v in (res.get("usage") or {}).items() if k in USAGE and isinstance(v, int)}
    return {"text": res["result"], "session": res["session_id"], **({"usage": usage} if usage else {})}
