"""agents-codex.turn: one agent turn in the Codex CLI.

``codex exec --json [-m M] -c developer_instructions=S ... -`` for a new
session, ``codex exec resume --json ... ID -`` to continue one, with the
prompt on stdin, run from the hub as a process (shell.open) on the worker
named by the ``worker`` setting, ended when the turn times out or is
cancelled. Codex prints JSONL events: ``thread.started`` carries the session id,
each ``item.completed`` of type ``agent_message`` a reply (the last one is
the answer), ``turn.completed`` the token usage, ``turn.failed`` the error.
Codex exits 0 on a failed turn, so the events decide.

``door``: the turn's door token by name (``{{secret:turn-...}}``), from the
agents runtime. It goes on untouched as ``ROOK_DOOR_TOKEN`` in the process's
environment, where the hub puts the token on the way to the worker. The
turn's Rook MCP server is the door at ``door_url``, its bearer token read
from that variable: never on the command line or on disk.
"""
from __future__ import annotations

import json
import re

from rook.kit.cli import Place, itself, stream, worker

# A session id or model name, never an option (see agents-claude-code).
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}")
# key=value, the key a dotted TOML path: never an option either.
_CONFIG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\"-]*=.*", re.S)
TOKEN_ENV = "ROOK_DOOR_TOKEN"
USAGE = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def toml_str(s: str) -> str:
    """A TOML basic string: JSON's escapes are TOML's."""
    return json.dumps(s, ensure_ascii=False)


def model_for(settings, model: str | None) -> str:
    """A model id as given; two/three (or blank) from the settings."""
    if model == "three":
        return settings["model_three"] or ""
    if not model or model == "two":
        return settings["model"] or ""
    return model


def argv(settings, *, program="codex", session=None, model=None, effort=None, system=None,
         door=False) -> list[str]:
    out = [program, "exec"]
    if session:
        out.append("resume")
    out += ["--json", "--skip-git-repo-check"]
    if settings["sandbox"]:     # `exec resume` has no -s; the config key works for both
        out += ["-c", f"sandbox_mode={toml_str(settings['sandbox'])}"]
    model = model_for(settings, model)
    if model:
        out += ["-m", model]
    if effort:
        out += ["-c", f"model_reasoning_effort={toml_str(effort)}"]
    if system:
        out += ["-c", f"developer_instructions={toml_str(system)}"]
    for kv in settings["config"] or ():
        out += ["-c", kv]
    if door:                     # the turn's own Rook: the door, with the token from the environment
        out += ["-c", f"mcp_servers.rook.url={toml_str(settings['door_url'])}",
                "-c", f"mcp_servers.rook.bearer_token_env_var={toml_str(TOKEN_ENV)}",
                # no prompts in exec: the hub decides what each call may do
                "-c", 'mcp_servers.rook.default_tools_approval_mode="approve"']
    if settings["isolate"]:      # not the person's own config, MCP servers and rules
        out += ["--ignore-user-config", "--ignore-rules"]
    return out + ([session] if session else []) + ["-"]


async def turn(ctx, prompt, session=None, cwd=None, model=None, effort=None, system=None, timeout="20m",
               door=None):
    for name, value in (("session", session), ("model", model)):
        if value and not _WORD.fullmatch(value):
            raise ctx.Error(f"bad args: {name} is an id or a name, not {value[:40]!r}")
    for kv in ctx.settings["config"] or ():
        if not _CONFIG.fullmatch(kv):
            raise ctx.Error(f"agents-codex.config: {kv[:40]!r} is not key=value")
    if door and not ctx.settings["door_url"]:
        raise ctx.Error("set agents-codex.door_url to the hub's MCP door to give agents Rook")
    ctx = itself(ctx)
    w = worker(ctx)
    place = await Place.of(ctx, w)
    work = place.expand(cwd) if cwd else await place.folder("work")
    cmd = argv(ctx.settings, program=place.expand(ctx.settings["codex"] or "codex"), session=session,
               model=model, effort=effort, system=system, door=bool(door))
    lines: list[str] = []
    code, tail = await stream(ctx, w, cmd, stdin=prompt, cwd=work, timeout=timeout, name="Codex",
                              setting="agents-codex.codex", env={TOKEN_ENV: door} if door else None,
                              line=lines.append)
    return parse(ctx, code, lines, tail)


def _message(raw) -> str:
    """Codex's error text; the API's own error often comes JSON-encoded inside."""
    text = str(raw or "")
    try:
        inner = json.loads(text)
        text = str(inner.get("error", {}).get("message") or inner.get("message") or text)
    except (ValueError, AttributeError):
        pass
    return text[:500]


def parse(ctx, code, lines: list, tail: str) -> dict:
    session, texts, usage, failed, done = None, [], {}, None, False
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if kind == "thread.started" and isinstance(ev.get("thread_id"), str):
            session = ev["thread_id"]
        elif kind == "item.completed" and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            texts.append(item["text"])
        elif kind == "turn.completed":
            done = True
            u = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            usage = {k: u[k] for k in USAGE if isinstance(u.get(k), int)}
        elif kind == "turn.failed":
            e = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            failed = _message(e.get("message")) or "the turn failed"
        elif kind == "error" and failed is None:
            failed = _message(ev.get("message")) or None      # kept only if the turn never completes
    if done and texts and session:
        return {"text": texts[-1], "session": session, **({"usage": usage} if usage else {})}
    if failed and not done:
        raise ctx.Error(f"Codex: {failed}")
    if done:
        raise ctx.Error("Codex answered without a reply or a session id")
    said = "\n".join(line for line in lines if not line.startswith("{")).strip() or tail.strip()
    raise ctx.Error(f"Codex failed (exit {code}): {said[-500:] or 'no output'}")
