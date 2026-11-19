"""agents-claude-code.turn: one agent turn in the Claude Code CLI, on this worker (brief HA2).

Ported from 0.5 (cbe86a47: rook/plugins/agents-claude-code/scripts/claude.py and plugin.yaml),
the house agent's engine:

    claude -p --output-format json [--resume ID] [--model M] [--append-system-prompt S]
           [--permission-mode P] [--allowedTools ...] [--mcp-config ...]
           [--setting-sources project,local --strict-mcp-config]

with the prompt on stdin, in this plugin's work folder on the worker. The JSON result carries
the reply (`result`), the session id the next turn resumes, and token usage.

`door`: the token the agents plugin minted for this turn, as the person whose message it
answers. The turn's Rook MCP server is the hub's door with that token, written to a 0600 file
in a temporary folder that goes when the turn ends, so every Rook call the agent makes is
journaled under that person and that token.

The settings 0.5 kept on the worker are this plugin's section of the worker's config (CF1 to
CF3; `config.set {worker, set: {"agents-claude-code": {...}}}`), each with 0.5's default:

    claude           the claude program: a name on PATH or a full path (claude)
    model            the model when the agent names none (opus)
    model_three      model "three" (haiku)
    permission_mode  --permission-mode for agent turns (auto: brief HA2)
    allowed_tools    --allowedTools, a list (none)
    mcp_config       a further MCP config file for agent turns (none)
    isolate          leave out the signed-in user's own Claude Code settings, hooks and MCP
                     servers: --setting-sources project,local --strict-mcp-config (true)
    door_url         the hub's MCP door as this worker reaches it (the hub this worker links to, /mcp)

A turn has no timeout (RULES.md). A call that is stopped (its stream closed, brief 59) ends the
claude process at once; nothing else ends it.
"""

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from rook_worker import Cap

# A session id or model name, never an option: --resume takes an optional value, so
# "--dangerously-skip-permissions" there would be read as a flag (0.5's guard).
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}")
USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
DEFAULTS = {"claude": "claude", "model": "opus", "model_three": "haiku", "permission_mode": "auto",
            "allowed_tools": [], "mcp_config": None, "isolate": True, "door_url": None}


class TurnFailed(Exception):
    """A turn that could not run or gave no reply: the call fails with this message (cap_failed)."""


def settings():
    """0.5's settings, from this plugin's section of the worker's config, else their defaults."""
    given = WORKER.config  # noqa: F821 - the worker puts WORKER in this module (docs/plugins.md)
    out = {key: given.get(key, value) for key, value in DEFAULTS.items()}
    if not out["door_url"]:
        out["door_url"] = WORKER.hub_url.rstrip("/") + "/mcp"  # noqa: F821
    return out


def argv(settings, *, session=None, model=None, system=None, door_config=None):
    out = [settings["claude"] or "claude", "-p", "--output-format", "json"]
    if session:
        out += ["--resume", session]
    if model == "three":                 # System Three; blank or two: System Two
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


def door_config(folder, url, token):
    """The turn's Rook MCP server: the door at `url` with `token`, in a 0600 file."""
    path = Path(folder) / "rook-mcp.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"rook": {"type": "http", "url": url,
                                           "headers": {"Authorization": f"Bearer {token}"}}}}, f)
    return path


async def turn(args):
    given = set(args) - {"prompt", "session", "cwd", "model", "effort", "system", "door"}
    if given:
        raise TurnFailed(f"agents-claude-code.turn takes prompt, session, cwd, model, effort, system and door; "
                         f"not {', '.join(sorted(given))}")
    prompt = args.get("prompt")
    if not isinstance(prompt, str):
        raise TurnFailed("prompt is required: the text of the turn")
    session, model, cwd = args.get("session"), args.get("model"), args.get("cwd")
    for name, value in (("session", session), ("model", model)):
        if value and not (isinstance(value, str) and _WORD.fullmatch(value)):
            raise TurnFailed(f"{name} is an id or a name, not {str(value)[:40]!r}")
    work = Path(cwd).expanduser() if cwd else WORKER.data / "work"  # noqa: F821
    if not cwd:
        work.mkdir(parents=True, exist_ok=True)
    elif not work.is_dir():
        raise TurnFailed(f"no folder {work}")
    chosen = settings()
    temp = tempfile.mkdtemp(prefix="rook-turn-")
    try:
        cmd = argv(chosen, session=session, model=model, system=args.get("system"),
                   door_config=door_config(temp, chosen["door_url"], args["door"]) if args.get("door") else None)
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(work), stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            raise TurnFailed(f"{cmd[0]} not found: install Claude Code on this worker, or set its path with "
                             "config.set {worker, set: {\"agents-claude-code\": {\"claude\": \"/path/to/claude\"}}}") from None
        try:
            out, err = await proc.communicate(prompt.encode("utf-8"))
        finally:
            if proc.returncode is None:      # the call was stopped: no orphan turn
                proc.kill()
                await proc.wait()
        return parse(proc.returncode, out, err)
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def parse(code, out, err):
    text = out.decode("utf-8", "replace").strip()
    try:
        res = json.JSONDecoder().raw_decode(text, text.index("{"))[0] if "{" in text else None
    except ValueError:
        res = None
    if not isinstance(res, dict):
        tail = (err.decode("utf-8", "replace").strip() or text)[-500:]
        raise TurnFailed(f"Claude Code failed (exit {code}): {tail or 'no output'}")
    if res.get("is_error") or res.get("subtype", "success") != "success":
        raise TurnFailed(f"Claude Code: {res.get('result') or res.get('subtype') or 'error'}")
    if not isinstance(res.get("result"), str) or not res.get("session_id"):
        raise TurnFailed("Claude Code answered without a reply or a session id")
    usage = {k: v for k, v in (res.get("usage") or {}).items() if k in USAGE and isinstance(v, int)}
    return {"text": res["result"], "session": res["session_id"], **({"usage": usage} if usage else {})}


CAPS = {
    "agents-claude-code.turn": Cap(
        "One agent turn in Claude Code on this worker; pass session back to continue the conversation.",
        {"type": "object", "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "description": "The turn's text, given to Claude Code on stdin."},
            "session": {"type": "string", "description": "The Claude Code session id to resume; blank: a new session."},
            "cwd": {"type": "string", "description": "The folder Claude Code works in; blank: this plugin's work folder."},
            "model": {"type": "string", "description": "A model, or two/three; blank: the model setting."},
            "effort": {"type": "string", "description": "Not used: Claude Code sets its own reasoning effort."},
            "system": {"type": "string", "description": "Appended to Claude Code's system prompt."},
            "door": {"type": "string", "description": "The turn's door token: the turn's Rook MCP server uses it."}}},
        turn),
}
