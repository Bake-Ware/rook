"""agents-opencode.turn: one agent turn in the OpenCode CLI.

``opencode run --format json [-s ID] [-m M] [--variant V]`` with the prompt on
stdin, started through the worker's launch path (as the normal user).
OpenCode prints JSON events: ``text`` parts (the reply), ``tool_use``,
``step_start`` / ``step_finish`` (tokens; reason ``stop`` ends the turn) and
``error``; each carries ``sessionID``. The reply is the text of the last step
that said anything.

Config: a generated ``opencode.json`` in this plugin's data folder, with the
hub's MCP door as the ``rook`` server (its bearer token from
``ROOK_DOOR_TOKEN`` in the turn's own environment: never on the command line
or on disk), OpenCode's own tools on (``own_tools``, default on), and, with
``isolate``, as the whole config (``XDG_CONFIG_HOME``): only the user's model
providers are copied in, never their MCP servers, agents or instructions.

OpenCode's free ``opencode/*`` models refuse a custom system prompt (a
custom agent or ``instructions`` hangs the turn on retries), so the agent's
instructions go at the head of a new session's first message instead.
OpenCode retries a failing model quietly; its error log lines (stderr) end
the turn at once with that error instead of waiting out the timeout.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from pathlib import Path

from rook.core2.worker.security import launch
from rook.kit._types import parse_duration

# A session id or provider/model name, never an option.
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,159}")
TOKEN_ENV = "ROOK_DOOR_TOKEN"
SERVER = "rook"
OWN_TOOLS = ("bash", "edit", "write", "patch", "read", "grep", "glob", "list", "webfetch", "websearch",
             "codesearch", "todowrite", "todoread", "task", "skill", "lsp")
PROVIDER_KEYS = ("provider", "model", "small_model", "enabled_providers", "disabled_providers")
# opencode --print-logs: level=ERROR ... error.error="AI_APICallError: Free usage limit reached..."
_LOG_ERROR = re.compile(r'level=ERROR .*?message="stream error".*?error\.(?:error|message)="((?:[^"\\]|\\.)*)"')


def strip_jsonc(text: str) -> str:
    """JSON with // and /* */ comments and trailing commas (opencode.jsonc) as JSON."""
    out, i, n, quote = [], 0, len(text), False
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                quote = False
        elif c == '"':
            quote = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def user_config() -> dict:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
    for name in ("opencode.json", "opencode.jsonc", "config.json"):
        p = base / name
        if p.is_file():
            with contextlib.suppress(OSError, ValueError):
                cfg = json.loads(strip_jsonc(p.read_text(encoding="utf-8")))
                return cfg if isinstance(cfg, dict) else {}
    return {}


def config(settings, door: bool) -> dict:
    cfg: dict = {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled"}
    if settings["isolate"]:
        user = user_config()
        cfg.update({k: user[k] for k in PROVIDER_KEYS if k in user})
    if door:
        cfg["mcp"] = {SERVER: {"type": "remote", "url": settings["door_url"], "enabled": True,
                               "headers": {"Authorization": "Bearer {env:%s}" % TOKEN_ENV}}}
    if not settings["own_tools"]:
        cfg["tools"] = {t: False for t in OWN_TOOLS}
    # No prompts in a headless run, and nothing denied (Bake, Nov 6: "Remove
    # all restrictions and gates").
    cfg["permission"] = {"*": "allow"}
    return cfg


def model_for(settings, model: str | None) -> str:
    if model == "three":
        return settings["model_three"] or settings["model"] or ""
    if not model or model == "two":
        return settings["model"] or ""
    return model


def argv(settings, *, session=None, model=None, effort=None) -> list[str]:
    out = [settings["opencode"] or "opencode", "run", "--format", "json", "--print-logs", "--log-level", "ERROR"]
    if session:
        out += ["--session", session]
    model = model_for(settings, model)
    if model:
        out += ["--model", model]
    if effort:
        out += ["--variant", effort]
    return out


def message(prompt: str, system: str | None, session: str | None) -> str:
    """A new session's first message carries the agent's instructions."""
    if session or not system:
        return prompt
    return f"<instructions>\n{system.strip()}\n</instructions>\n\n{prompt}"


async def turn(ctx, prompt, session=None, cwd=None, model=None, effort=None, system=None, timeout="20m",
               door=None):
    work = Path(cwd).expanduser() if cwd else ctx.data / "work"
    if not cwd:
        work.mkdir(parents=True, exist_ok=True)
    elif not work.is_dir():
        raise ctx.Error(f"no folder {work}")
    for name, value in (("session", session), ("model", model), ("effort", effort)):
        if value and not _WORD.fullmatch(value):
            raise ctx.Error(f"bad args: {name} is an id or a name, not {value[:40]!r}")
    if door and not ctx.settings["door_url"]:
        raise ctx.Error("set agents-opencode.door_url to the hub's MCP door to give agents Rook")
    home = ctx.data / "config"
    (home / "opencode").mkdir(parents=True, exist_ok=True)
    cfg_file = home / "opencode" / "opencode.json"
    cfg_file.write_text(json.dumps(config(ctx.settings, bool(door)), indent=1), encoding="utf-8")
    env = {**os.environ, "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_CONFIG": str(cfg_file)}
    if ctx.settings["isolate"]:
        env["XDG_CONFIG_HOME"] = str(home)
    if door:
        env[TOKEN_ENV] = door
    cmd = argv(ctx.settings, session=session, model=model, effort=effort)
    try:
        proc = await launch(cmd, getattr(ctx, "lease", None), cwd=str(work), stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
                            limit=16 * 1024 * 1024)      # one JSON event per line, tool output and all
    except FileNotFoundError:
        raise ctx.Error(f"{cmd[0]} not found: install OpenCode or set agents-opencode.opencode") from None
    st = _Turn()
    try:
        await asyncio.wait_for(_run(proc, message(prompt, system, session), st), parse_duration(timeout))
    except asyncio.TimeoutError:
        raise ctx.Error(f"OpenCode took longer than {timeout}", retry=True) from None
    finally:
        if proc.returncode is None:      # timed out, failed or cancelled: no orphan turn
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    return st.result(ctx, proc.returncode)


class _Turn:
    def __init__(self):
        self.session, self.steps, self.usage = None, [[]], {}
        self.done, self.error, self.log_error, self.err_tail = False, None, None, ""

    def feed(self, ev: dict) -> None:
        if isinstance(ev.get("sessionID"), str):
            self.session = self.session or ev["sessionID"]
        kind = ev.get("type")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if kind == "step_start":
            self.steps.append([])
        elif kind == "text" and isinstance(part.get("text"), str) and not part.get("synthetic"):
            self.steps[-1].append(part["text"])
        elif kind == "step_finish":
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            for k in ("input", "output", "reasoning"):
                if isinstance(tok.get(k), int):
                    self.usage[f"{k}_tokens"] = self.usage.get(f"{k}_tokens", 0) + tok[k]
            if part.get("reason") == "stop":
                self.done = True
        elif kind == "error":
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            self.error = str(data.get("message") or err.get("name") or "OpenCode failed")[:500]

    def text(self) -> str:
        for step in reversed(self.steps):
            if any(t.strip() for t in step):
                return "".join(step).strip()
        return ""

    def result(self, ctx, code) -> dict:
        text = self.text()
        if self.done and text and self.session:
            return {"text": text, "session": self.session, **({"usage": self.usage} if self.usage else {})}
        why = self.error or self.log_error
        if why:
            raise ctx.Error(f"OpenCode: {why}", retry="limit" in why.lower())
        if self.done:
            raise ctx.Error("OpenCode answered without a reply or a session id")
        raise ctx.Error(f"OpenCode failed (exit {code}): {self.err_tail[-500:] or 'no output'}")


async def _run(proc, prompt: str, st: _Turn) -> None:
    proc.stdin.write(prompt.encode("utf-8"))
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await proc.stdin.drain()
    proc.stdin.close()

    async def out():
        async for line in proc.stdout:
            with contextlib.suppress(ValueError):
                ev = json.loads(line)
                if isinstance(ev, dict):
                    st.feed(ev)

    async def err():
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            st.err_tail = (st.err_tail + "\n" + line)[-2000:]
            m = _LOG_ERROR.search(line)
            if m and st.log_error is None:
                st.log_error = m.group(1).replace('\\"', '"')[:500]
                # OpenCode would retry quietly until the timeout: stop now with its error.
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return

    await asyncio.gather(out(), err())
    await proc.wait()
