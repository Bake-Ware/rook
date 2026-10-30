"""llm-cli: one completion through Claude Code (``claude -p --output-format
json --tools ""``, the system prompt by --system-prompt) or Codex (``codex
exec --ephemeral -s read-only -o FILE -``), the prompt on stdin, started
through the worker's launch path in a per-call temp folder."""
from __future__ import annotations

import asyncio
import json

from rook.core2.worker.security import launch
from rook.kit._types import parse_duration
from rook.kit.ctx import KitError

DEFAULTS = {"claude": {"one": "haiku", "two": "opus", "three": "haiku"}, "codex": {}}
USAGE = {"input": "input_tokens", "output": "output_tokens"}


async def complete(ctx, messages, model=None, max_tokens=None, temperature=None, stop=None):
    tool = ctx.settings["tool"]
    model = system_model(ctx.settings, model)
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    prompt = transcript([m for m in messages if m["role"] != "system"])
    if tool == "claude":
        cmd = [_program(ctx), "-p", "--output-format", "json", "--tools", "", "--no-session-persistence"]
        cmd += ["--model", model] if model else []
        cmd += ["--system-prompt", system] if system else []
        out = await _run(ctx, cmd, prompt)
        text, usage = _claude(out)
    else:
        last = ctx.tmp / "reply.txt"
        cmd = [_program(ctx), "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-o", str(last)]
        cmd += ["-m", model] if model else []
        await _run(ctx, cmd + ["-"], f"{system}\n\n{prompt}" if system else prompt)
        text, usage = (last.read_text(encoding="utf-8").strip() if last.is_file() else ""), {}
    reason = "end"
    for s in stop or ():
        if s and s in text:
            text, reason = text[:text.index(s)], "stop"
    return {"text": text, "model": model, "stop_reason": reason, **({"usage": usage} if usage else {})}


async def models(ctx):
    out = await _run(ctx, [_program(ctx), "--version"], "")
    return {"tool": ctx.settings["tool"], "version": out.decode("utf-8", "replace").strip()[:200],
            "models": sorted({m for m in systems(ctx.settings).values() if m}), "systems": systems(ctx.settings)}


def systems(settings) -> dict:
    base = DEFAULTS[settings["tool"]]
    return {s: settings[f"model_{s}"] or base.get(s, "") for s in ("one", "two", "three")}


def system_model(settings, model: str | None) -> str:
    """A model id as given, or System One/Two/Three's (blank: Two)."""
    if not model or model in ("one", "two", "three"):
        return systems(settings)[model or "two"]
    return model


def transcript(turns: list) -> str:
    """One user message as is; a longer conversation as a labelled transcript."""
    if not turns:
        raise KitError("no user message to answer")
    if len(turns) == 1:
        return turns[0]["content"]
    lines = [f"{'User' if t['role'] == 'user' else 'Assistant'}: {t['content']}" for t in turns]
    return ("Continue this conversation: reply as the assistant to the last message, with the reply only.\n\n"
            + "\n\n".join(lines))


def _program(ctx) -> str:
    return str(ctx.settings["program"] or ctx.settings["tool"])


async def _run(ctx, cmd: list, stdin: str) -> bytes:
    try:
        proc = await launch(cmd, getattr(ctx, "lease", None), cwd=str(ctx.tmp), stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise KitError(f"{cmd[0]} not found: install it or set llm-cli.program") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode("utf-8")),
                                          parse_duration(ctx.settings["timeout"]))
    except asyncio.TimeoutError:
        raise KitError(f"{cmd[0]} took longer than {ctx.settings['timeout']}", retry=True) from None
    finally:
        if proc.returncode is None:      # timed out or cancelled: no orphan process
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        tail = (err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip())[-300:]
        raise KitError(f"{cmd[0]} failed (exit {proc.returncode}): {tail or 'no output'}")
    return out


def _claude(out: bytes) -> tuple[str, dict]:
    text = out.decode("utf-8", "replace").strip()
    try:
        res = json.JSONDecoder().raw_decode(text, text.index("{"))[0] if "{" in text else None
    except ValueError:
        res = None
    if not isinstance(res, dict) or not isinstance(res.get("result"), str):
        raise KitError(f"Claude Code answered without a reply: {text[-300:] or 'no output'}")
    if res.get("is_error") or res.get("subtype", "success") != "success":
        raise KitError(f"Claude Code: {res.get('result') or res.get('subtype')}")
    usage = res.get("usage") or {}
    return res["result"], {k: usage[u] for k, u in USAGE.items() if isinstance(usage.get(u), int)}
