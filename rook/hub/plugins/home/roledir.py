"""A persistent role's directory, as files (docs/design/persistent-agents.md 3.1).

The hub renders every configuration file of a role that runs as persistent
Claude Code sessions; the session host on the agent host writes them into the
role's directory (only the ones that changed) and starts its lanes there::

    <role>/CLAUDE.md                 persona and standing rules
    <role>/.claude/settings.json     model, effort, permissions, env
    <role>/.mcp.json                 "rook": the hub's tools (bearer ${ROOK_TURN_TOKEN})
    <role>/voice/CLAUDE.md           the voice conductor's rules (inherits ../CLAUDE.md)
    <role>/voice/.claude/settings.json   Haiku, effort low, thinking off, flow tools only
    <role>/voice/.mcp.json           "flow": the hub's flow tools (bearer ${ROOK_FLOW_TOKEN})

Tokens never appear in a file: the configs name environment variables that the
session host sets for each process. MCP servers the person granted from the
host's own Claude Code config (``host_servers``) are merged in by the session
host, which keeps their definitions; here they are only allowed by name.
"""

from __future__ import annotations

import json
import re

TOOL_SERVER = "rook"
FLOW_SERVER = "flow"
TURN_TOKEN_ENV = "ROOK_TURN_TOKEN"
FLOW_TOKEN_ENV = "ROOK_FLOW_TOKEN"
SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
#: Claude Code's file tools for the main lane: read anywhere in the role's
#: directory, write only in workspace/ (as role homes, huddles.md 4.4).
MAIN_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit")
#: Built-in tools a persistent session never gets.
DENY = ("Bash", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "KillShell",
        "BashOutput")
VOICE_DENY = DENY + ("Read", "Glob", "Grep", "Write", "Edit")

HOUSE_RULES = """# How you work (persistent session)

You are always on: this one conversation continues across days. Requests reach you
through the hub's inbox, one at a time, each starting with a header such as
`[item q12 | from Bake (person) | via voice | live]`. Answer the request in that
item; the hub delivers your reply to whoever asked (spoken, in chat, in a huddle).

- When a header says "Context for this request", it replaces the earlier context: the
  trust map, your playbook, what you remember and your open asks as of now.
- "While you were busy:" lists background items (session reports, job results) that
  arrived during your last turn. Take them in; act only where something needs you.
- Keep your own turns short. Long or multi-step work goes to a session
  (dispatch_session) or a huddle (open_huddle), which report back on their own; the
  queue behind you keeps moving.
- "[fold from side thread …]" is the result of a side thread forked from you. Fold it
  into what you know. If it conflicts with something you decided or are doing, settle
  it yourself when you can; otherwise ask the person one line (ask_person).
- Words added mid-turn ("[… while you work]") come from the person, through the voice:
  take them into account in the turn you are in.
- Only what your tools actually did happened. Never say an action is done unless a
  tool result says so.
- "Maintenance:" messages come from the hub, not a person: follow them (they keep your
  memory before your context is compacted).
"""

VOICE_RULES = """# You are the voice (the conductor)

You speak for the agent described in ../CLAUDE.md, out loud, to the person in the room.
You are fast and you do not act: the back end (the same agent, in its main session,
with all its tools) does the work. You talk; you route; you relay.

Every turn:
1. Speak first. Start your reply at once in one or two short spoken sentences: plain
   text, no markdown, no lists, no emoji. Never wait on a tool before talking.
2. Then route, with the flow tools, when the request needs the back end:
   - queue(request): anything to look up, do, change, remember or decide. Say what you
     are handing over ("I'll get on that"), never that it is done.
   - steer(text): the person adds to or corrects what the back end is working on now.
     interrupt=true when they say stop or change course.
   - fork(request): a separate request while the back end is busy with something else.
   - btw(question): "where are we at on that?" while it works: answered from its
     current context without disturbing it. Then say the answer in a sentence.
   - status(): what the back end and side threads are doing, without bothering it.
   - fold, pause, resume, cancel: when the person asks for exactly that.
   Small talk, a greeting or something you know for certain needs no tool.
3. After a tool call, say nothing more unless the result is something the person
   needs to hear now (a btw or status answer, or a refusal).

Messages starting `[back end]` are results from the back end: tell the person the
gist in a sentence or two. Only the back end says an action is done; never claim one
yourself, and never invent results. `[voice | <name>]` tells you who is speaking.
"""


def _json(obj: dict) -> str:
    return json.dumps(obj, indent=2, sort_keys=False) + "\n"


def server_names(names) -> list[str]:
    """Valid, unique MCP server names in order (``rook`` and ``flow`` are the hub's)."""
    out: list[str] = []
    for n in names or []:
        n = str(n or "").strip()
        if SERVER_NAME.match(n) and n not in (TOOL_SERVER, FLOW_SERVER) and n not in out:
            out.append(n)
    return out


def main_files(*, claude_md: str, model: str, effort: str, turn_url: str,
               host_servers: list[str] | None = None, env: dict | None = None,
               workspace: bool = True) -> dict[str, str]:
    """The main lane's files: ``CLAUDE.md``, ``.claude/settings.json``, ``.mcp.json``."""
    granted = server_names(host_servers)
    allow = [f"mcp__{TOOL_SERVER}"] + [f"mcp__{n}" for n in granted]
    deny = list(DENY)
    if workspace:
        # Relative to the session's directory ("./"): read the role's own
        # directory, write only in workspace/ (Edit rules govern Write too).
        allow += ["Read(./**)", "Glob(./**)", "Grep(./**)", "Edit(./workspace/**)"]
    else:
        deny += ["Read", "Glob", "Grep", "Write", "Edit"]
    settings: dict = {
        "model": model or "opus",
        "permissions": {"allow": allow, "deny": deny, "defaultMode": "dontAsk"},
        "enableAllProjectMcpServers": False,
        "enabledMcpjsonServers": [TOOL_SERVER] + granted,
        "autoMemoryEnabled": False,
        "includeCoAuthoredBy": False,
        "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1",
                **{str(k): str(v) for k, v in (env or {}).items()}},
    }
    if effort:
        settings["effortLevel"] = effort
    mcp = {"mcpServers": {TOOL_SERVER: {
        "type": "http", "url": turn_url,
        "headers": {"Authorization": "Bearer ${%s}" % TURN_TOKEN_ENV}}}}
    return {"CLAUDE.md": claude_md.rstrip() + "\n",
            ".claude/settings.json": _json(settings),
            ".mcp.json": _json(mcp)}


def voice_files(*, turn_url: str, model: str = "haiku", effort: str = "low",
                rules: str = VOICE_RULES) -> dict[str, str]:
    """The voice lane's files, under ``voice/``. Thinking off: Haiku 5.5 does
    not accept disabled thinking, so effort ``low`` and ``MAX_THINKING_TOKENS=0``
    (zero thinking tokens in practice; the session host records them per turn)."""
    settings = {
        "model": model or "haiku",
        "effortLevel": effort or "low",
        "alwaysThinkingEnabled": False,
        "permissions": {"allow": [f"mcp__{FLOW_SERVER}"], "deny": list(VOICE_DENY),
                        "defaultMode": "dontAsk"},
        "enableAllProjectMcpServers": False,
        "enabledMcpjsonServers": [FLOW_SERVER],
        "autoMemoryEnabled": False,
        "includeCoAuthoredBy": False,
        "env": {"MAX_THINKING_TOKENS": "0", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "DISABLE_AUTOUPDATER": "1"},
    }
    mcp = {"mcpServers": {FLOW_SERVER: {
        "type": "http", "url": turn_url,
        "headers": {"Authorization": "Bearer ${%s}" % FLOW_TOKEN_ENV}}}}
    return {"voice/CLAUDE.md": rules.rstrip() + "\n",
            "voice/.claude/settings.json": _json(settings),
            "voice/.mcp.json": _json(mcp)}


def house_claude_md(persona: str, agent_name: str, extra: str = "") -> str:
    """The house agent's ``CLAUDE.md``: who it is, then how a persistent session works.
    The per-request context (trust map, playbook, memory, open asks) arrives with
    the requests, so this file changes rarely."""
    head = (f"# {agent_name}: the house agent\n\n"
            f"You are {agent_name}, the house agent of this Rook hub: the person's personal "
            "assistant and the front door of their band.")
    parts = [head, persona.strip(), extra.strip(), HOUSE_RULES]
    return "\n\n".join(p for p in parts if p)


def role_claude_md(system_prompt: str, name: str) -> str:
    """Another role's ``CLAUDE.md``: its system prompt (charter, checklist, trust),
    then the persistent-session rules."""
    return "\n\n".join(p for p in [f"# {name}", system_prompt.strip(), HOUSE_RULES] if p)
