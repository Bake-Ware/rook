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

Added for the house agent's memory (brief MEM5), each optional, the arguments above unchanged:

    fork        with session: a fork of that session (--fork-session), so nothing the turn says
                reaches the conversation: the memory's dream
    schema      a JSON Schema the answer is held to (--json-schema); the reply's `structured` is it.
                Such a turn is a one-off: its session is not saved (--no-session-persistence)
    precompact  {cap, args}: a hub cap Claude Code's PreCompact hook calls through the door, with the
                turn's token, before it compacts the session, waiting until it has answered (the
                memory dreams first). The hook's timeout is Claude Code's only way to say how long it
                may take; it is set to HOOK_WAIT, which no dream comes near.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from rook_worker import Cap

# A session id or model name, never an option: --resume takes an optional value, so
# "--dangerously-skip-permissions" there would be read as a flag (0.5's guard).
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}")
HOOK_WAIT = 10 * 365 * 24 * 3600     # seconds: Claude Code needs a number; ten years stands for no limit
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


def argv(settings, *, session=None, model=None, system=None, door_config=None, fork=False, schema=None, hook=None):
    out = [settings["claude"] or "claude", "-p", "--output-format", "json"]
    if session:
        out += ["--resume", session]
        if fork:
            out += ["--fork-session"]
    if schema is not None:
        out += ["--json-schema", json.dumps(schema), "--no-session-persistence"]
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
    if hook:                         # the hook before compaction (brief MEM5)
        out += ["--settings", str(hook)]
    return out


def door_config(folder, url, token):
    """The turn's Rook MCP server: the door at `url` with `token`, in a 0600 file."""
    path = Path(folder) / "rook-mcp.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"rook": {"type": "http", "url": url,
                                           "headers": {"Authorization": f"Bearer {token}"}}}}, f)
    return path


HOOK = """import json, sys, urllib.request
config = json.load(open(sys.argv[1]))["mcpServers"]["rook"]
cap, args = json.load(open(sys.argv[2]))


def door(name, arguments):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()
    request = urllib.request.Request(config["url"], data=body, method="POST",
                                     headers=dict(config["headers"], **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(request) as reply:
        result = json.loads(reply.read())["result"]
    return json.loads(result["content"][0]["text"]) if not result["isError"] else {"error": result["content"][0]["text"]}


got = door("call", {"worker": "hub", "cap": cap, "args": args,
                    "why": "Claude Code is about to compact the house agent's session: memory dreams first."})
place = 0
while isinstance(got, dict) and got.get("running"):          # a long call: follow its stream until it ends
    read = door("stream", {"action": "read", "stream": got["stream"], "from": place, "wait": 40})
    place = read.get("next", place)
    if not read.get("open", True):
        break
"""


def hook_settings(folder, door_file, precompact):
    """Claude Code settings holding one PreCompact hook: the cap `precompact` names, through the door."""
    folder = Path(folder)
    (folder / "precompact.json").write_text(json.dumps([precompact["cap"], precompact.get("args") or {}]))
    (folder / "precompact.py").write_text(HOOK)
    command = " ".join(f"'{p}'" for p in (sys.executable, folder / "precompact.py", door_file, folder / "precompact.json"))
    path = folder / "hook-settings.json"
    path.write_text(json.dumps({"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": command,
                                                                     "timeout": HOOK_WAIT}]}]}}))
    return path


async def turn(args):
    given = set(args) - {"prompt", "session", "cwd", "model", "effort", "system", "door", "fork", "schema", "precompact"}
    if given:
        raise TurnFailed(f"agents-claude-code.turn takes prompt, session, cwd, model, effort, system, door, fork, schema "
                         f"and precompact; not {', '.join(sorted(given))}")
    if args.get("fork") and not args.get("session"):
        raise TurnFailed("fork needs session: the session to fork")
    precompact = args.get("precompact")
    if precompact is not None and not (isinstance(precompact, dict) and isinstance(precompact.get("cap"), str)
                                       and args.get("door")):
        raise TurnFailed("precompact is {cap, args}, a hub cap the hook calls through the door: it needs door too")
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
        door = door_config(temp, chosen["door_url"], args["door"]) if args.get("door") else None
        cmd = argv(chosen, session=session, model=model, system=args.get("system"), door_config=door,
                   fork=bool(args.get("fork")), schema=args.get("schema"),
                   hook=hook_settings(temp, door, precompact) if precompact else None)
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
        return parse(proc.returncode, out, err, structured=args.get("schema") is not None)
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def parse(code, out, err, structured=False):
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
    out = {"text": res["result"], "session": res["session_id"], **({"usage": usage} if usage else {})}
    if structured:
        if not isinstance(res.get("structured_output"), dict):
            raise TurnFailed("Claude Code answered without the structured answer the schema asked for")
        out["structured"] = res["structured_output"]
    return out


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
            "door": {"type": "string", "description": "The turn's door token: the turn's Rook MCP server uses it."},
            "fork": {"type": "boolean", "description": "With session: fork it, so nothing said reaches the conversation."},
            "schema": {"type": "object", "description": "A JSON Schema the answer is held to; the reply's structured."},
            "precompact": {"type": "object", "description": "{cap, args}: the hub cap the hook before compaction calls, "
                                                            "through the door, and waits for."}}},
        turn),
}
