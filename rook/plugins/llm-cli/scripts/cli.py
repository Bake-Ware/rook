"""llm-cli: one completion through Claude Code (``claude -p --output-format
json --tools "" --strict-mcp-config --setting-sources project,local``: no
tools, no MCP servers, none of the signed-in user's hooks; the system
prompt by --system-prompt) or Codex (``codex exec --ephemeral -s read-only
-o FILE -``), the prompt on stdin. It runs on the worker named by the
``worker`` setting (where the CLI is signed in) through shell.exec, in a
fresh folder in that worker's temp folder, removed afterwards."""
from __future__ import annotations

import json
import secrets

from rook.kit._types import parse_duration
from rook.kit.cli import Place, itself, plain, worker
from rook.kit.ctx import KitError

DEFAULTS = {"claude": {"one": "haiku", "two": "opus", "three": "haiku"}, "codex": {}}
USAGE = {"input": "input_tokens", "output": "output_tokens"}


async def complete(ctx, messages, model=None, max_tokens=None, temperature=None, stop=None):
    ctx = itself(ctx)
    tool = ctx.settings["tool"]
    model = system_model(ctx.settings, model)
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    prompt = transcript([m for m in messages if m["role"] != "system"])
    async with _Folder(ctx) as folder:
        if tool == "claude":
            cmd = [_program(ctx), "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
                   "--setting-sources", "project,local", "--no-session-persistence"]
            cmd += ["--model", model] if model else []
            cmd += ["--system-prompt", system] if system else []
            text, usage = _claude(await _run(ctx, cmd, prompt, folder.path))
        else:
            last = folder.join("reply.txt")
            cmd = [_program(ctx), "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-o", last]
            cmd += ["-m", model] if model else []
            await _run(ctx, cmd + ["-"], f"{system}\n\n{prompt}" if system else prompt, folder.path)
            try:
                got = await ctx.call_on(worker(ctx), "file.read", path=last)
                text = got["content"].strip()
            except KitError:
                text = ""
            usage = {}
    reason = "end"
    for s in stop or ():
        if s and s in text:
            text, reason = text[:text.index(s)], "stop"
    return {"text": text, "model": model, "stop_reason": reason, **({"usage": usage} if usage else {})}


async def models(ctx):
    ctx = itself(ctx)
    out = await _run(ctx, [_program(ctx), "--version"], "", None)
    return {"tool": ctx.settings["tool"], "version": out.strip()[:200],
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


class _Folder:
    """A fresh folder in the worker's temp folder for one run, removed after."""

    def __init__(self, ctx):
        self.ctx, self.wid = ctx, worker(ctx)

    def join(self, name: str) -> str:
        return self.place.join(self.path, name)

    async def __aenter__(self):
        self.place = place = await Place.of(self.ctx, self.wid)
        root = await place.env("TEMP" if place.windows else "TMPDIR", "/tmp")
        self.path = await place.mkdir(place.join(root, f"rook-llm-{secrets.token_hex(8)}"))
        return self

    async def __aexit__(self, *exc):
        rm = ["cmd", "/c", "rmdir", "/s", "/q", self.path] if self.place.windows else ["rm", "-rf", self.path]
        await self.ctx.call_on(self.wid, "shell.exec", argv=rm)


async def _run(ctx, cmd: list, stdin: str, cwd: str | None) -> str:
    limit = parse_duration(ctx.settings["timeout"])
    req = {"argv": cmd, "stdin": stdin, "timeout": limit, **({"cwd": cwd} if cwd else {})}
    try:
        out = await ctx.call_on(worker(ctx), "shell.exec", **req)
    except KitError as e:
        raise plain(e, cmd[0], "llm-cli.program", ctx.settings["timeout"]) from None
    if out["code"] != 0:
        tail = (out["stderr"].strip() or out["stdout"].strip())[-300:]
        raise KitError(f"{cmd[0]} failed (exit {out['code']}): {tail or 'no output'}")
    return out["stdout"]


def _claude(text: str) -> tuple[str, dict]:
    text = text.strip()
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
