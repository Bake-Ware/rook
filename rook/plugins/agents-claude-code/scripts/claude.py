"""agents-claude-code.turn: one agent turn in the Claude Code CLI.

``claude -p --output-format json [--resume ID] [--model M]
[--append-system-prompt S] ...`` with the prompt on stdin, started through
the worker's launch path (as the normal user). The JSON result carries the
reply (``result``), the session id to resume and token usage.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rook.core2.worker.security import launch
from rook.kit._types import parse_duration

USAGE = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def argv(settings, *, session=None, model=None, system=None) -> list[str]:
    out = [settings["claude"] or "claude", "-p", "--output-format", "json"]
    if session:
        out += ["--resume", session]
    model = model or settings["model"]
    if model:
        out += ["--model", model]
    if system:
        out += ["--append-system-prompt", system]
    if settings["permission_mode"]:
        out += ["--permission-mode", settings["permission_mode"]]
    if settings["allowed_tools"]:
        out += ["--allowedTools", ",".join(settings["allowed_tools"])]
    if settings["mcp_config"]:
        out += ["--mcp-config", str(settings["mcp_config"])]
    return out


async def turn(ctx, prompt, session=None, cwd=None, model=None, effort=None, system=None, timeout="20m"):
    work = Path(cwd).expanduser() if cwd else ctx.data / "work"
    if not cwd:
        work.mkdir(parents=True, exist_ok=True)
    elif not work.is_dir():
        raise ctx.Error(f"no folder {work}")
    cmd = argv(ctx.settings, session=session, model=model, system=system)
    try:
        proc = await launch(cmd, getattr(ctx, "lease", None), cwd=str(work), stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise ctx.Error(f"{cmd[0]} not found: install Claude Code or set agents-claude-code.claude") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), parse_duration(timeout))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ctx.Error(f"Claude Code took longer than {timeout}", retry=True) from None
    return parse(ctx, proc.returncode, out, err)


def parse(ctx, code, out: bytes, err: bytes) -> dict:
    text = out.decode("utf-8", "replace").strip()
    try:
        res = json.loads(text[text.index("{"):]) if "{" in text else None
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
