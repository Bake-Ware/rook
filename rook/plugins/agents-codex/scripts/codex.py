"""agents-codex.turn: one agent turn in the Codex CLI.

``codex exec --json [-m M] -c developer_instructions=S ... -`` for a new
session, ``codex exec resume --json ... ID -`` to continue one, with the
prompt on stdin, started through the worker's launch path (as the normal
user). Codex prints JSONL events: ``thread.started`` carries the session id,
each ``item.completed`` of type ``agent_message`` a reply (the last one is
the answer), ``turn.completed`` the token usage, ``turn.failed`` the error.
Codex exits 0 on a failed turn, so the events decide.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from rook.core2.worker.security import launch
from rook.kit._types import parse_duration

# A session id or model name, never an option (see agents-claude-code).
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}")
# key=value, the key a dotted TOML path: never an option either.
_CONFIG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\"-]*=.*", re.S)
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


def argv(settings, *, session=None, model=None, effort=None, system=None) -> list[str]:
    out = [settings["codex"] or "codex", "exec"]
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
    if settings["isolate"]:      # not the person's own config, MCP servers and rules
        out += ["--ignore-user-config", "--ignore-rules"]
    return out + ([session] if session else []) + ["-"]


async def turn(ctx, prompt, session=None, cwd=None, model=None, effort=None, system=None, timeout="20m"):
    work = Path(cwd).expanduser() if cwd else ctx.data / "work"
    if not cwd:
        work.mkdir(parents=True, exist_ok=True)
    elif not work.is_dir():
        raise ctx.Error(f"no folder {work}")
    for name, value in (("session", session), ("model", model)):
        if value and not _WORD.fullmatch(value):
            raise ctx.Error(f"bad args: {name} is an id or a name, not {value[:40]!r}")
    for kv in ctx.settings["config"] or ():
        if not _CONFIG.fullmatch(kv):
            raise ctx.Error(f"agents-codex.config: {kv[:40]!r} is not key=value")
    cmd = argv(ctx.settings, session=session, model=model, effort=effort, system=system)
    try:
        proc = await launch(cmd, getattr(ctx, "lease", None), cwd=str(work), stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise ctx.Error(f"{cmd[0]} not found: install Codex or set agents-codex.codex") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), parse_duration(timeout))
    except asyncio.TimeoutError:
        raise ctx.Error(f"Codex took longer than {timeout}", retry=True) from None
    finally:
        if proc.returncode is None:      # timed out or the call was cancelled: no orphan turn
            proc.kill()
            await proc.wait()
    return parse(ctx, proc.returncode, out, err)


def _message(raw) -> str:
    """Codex's error text; the API's own error often comes JSON-encoded inside."""
    text = str(raw or "")
    try:
        inner = json.loads(text)
        text = str(inner.get("error", {}).get("message") or inner.get("message") or text)
    except (ValueError, AttributeError):
        pass
    return text[:500]


def parse(ctx, code, out: bytes, err: bytes) -> dict:
    session, texts, usage, failed, done = None, [], {}, None, False
    for line in out.decode("utf-8", "replace").splitlines():
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
    tail = (err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip())[-500:]
    raise ctx.Error(f"Codex failed (exit {code}): {tail or 'no output'}")
