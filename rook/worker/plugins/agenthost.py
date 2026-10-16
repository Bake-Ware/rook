"""agenthost.* — run house agent turns with the locally installed Claude Code.

The hub's home agent can use Claude Code as its model backend
(docs/design/huddles.md 3). The hub stays thin: each turn runs here as one
headless ``claude -p`` process, and its only tools come from the hub, over a
per-turn MCP endpoint (``mcp_url`` with a bearer ``mcp_token`` that dies with
the turn). Claude Code's own tools, settings files, MCP servers, slash
commands and session files are all off for these turns:

    claude -p --output-format stream-json --verbose --model <model>
           --system-prompt <system> --tools "" --setting-sources ""
           --strict-mcp-config --mcp-config <turn dir>/mcp.json
           --allowedTools mcp__home --permission-mode dontAsk
           --no-session-persistence --disable-slash-commands

The prompt goes in on stdin. Auth is the host's own ``claude`` login, or a
long-lived token from ``claude setup-token`` (``oauth_token``, passed as
``CLAUDE_CODE_OAUTH_TOKEN``; the hub resolves it from its vault).

Off unless the worker opts in (``agenthost.enabled``, or ``ROOK_AGENTHOST=1``):
a machine with Claude Code installed is not a house agent host until someone
says so. Then only active where a ``claude`` binary is found.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from ...core.pluginkit import fail, which
from ..plugin import Plugin, capability, setting

log = logging.getLogger("rook.worker.plugins.agenthost")

#: Turns run at once on this host.
MAX_PARALLEL = 3
#: Finished turns kept for an idempotent retry (by turn_id).
KEEP_RESULTS = 50
#: Most characters of the reply text returned.
TEXT_CHARS = 40000
MAX_PROMPT = 200_000
MAX_SYSTEM = 60_000
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MODEL = re.compile(r"^[A-Za-z0-9_.:\[\]-]{1,80}$")
_EFFORT = ("low", "medium", "high", "xhigh", "max")
#: The tool server name in the generated MCP config; tools show up to the
#: model as ``mcp__home__<tool>``.
SERVER = "home"


def _home() -> str:
    """The service user's home (systemd units sometimes leave HOME unset)."""
    h = os.environ.get("HOME")
    if h:
        return h
    with contextlib.suppress(Exception):
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    return os.path.expanduser("~")


def _claude() -> str | None:
    """The claude binary: PATH first, then the native installer's spot."""
    exe = which("claude")
    if exe:
        return exe
    p = Path(_home()) / ".local" / "bin" / "claude"
    return str(p) if p.is_file() and os.access(p, os.X_OK) else None


def build_argv(exe: str, *, model: str, system: str, mcp_config: str,
               effort: str | None = None) -> list[str]:
    argv = [exe, "-p", "--output-format", "stream-json", "--verbose",
            "--model", model, "--system-prompt", system,
            "--tools", "", "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", mcp_config,
            "--allowedTools", f"mcp__{SERVER}",
            "--permission-mode", "dontAsk",
            "--no-session-persistence", "--disable-slash-commands"]
    if effort:
        argv += ["--effort", effort]
    return argv


class Turn:
    """What one stream-json run said, folded as the lines arrive."""

    def __init__(self) -> None:
        self.model = ""
        self.session = ""
        self.texts: list[str] = []
        self.tools: list[str] = []
        self.result: dict | None = None
        self.error = ""

    def feed(self, line: str) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            self.model = str(ev.get("model") or "")
            self.session = str(ev.get("session_id") or "")
        elif kind == "assistant":
            msg = ev.get("message") or {}
            self.model = str(msg.get("model") or self.model)
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    self.texts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    name = str(block.get("name") or "")
                    self.tools.append(name.split("__", 2)[-1] if name.startswith("mcp__") else name)
        elif kind == "result":
            self.result = ev

    def reply(self) -> dict:
        r = self.result or {}
        text = r.get("result") if isinstance(r.get("result"), str) else ""
        if not text and self.texts:
            text = self.texts[-1]
        usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
        return {"text": (text or "")[:TEXT_CHARS], "model": self.model,
                "tools_used": self.tools, "num_turns": r.get("num_turns"),
                "usage": {k: usage.get(k) for k in ("input_tokens", "output_tokens",
                                                     "cache_creation_input_tokens",
                                                     "cache_read_input_tokens") if k in usage},
                "cost_usd": r.get("total_cost_usd"),
                "api_ms": r.get("duration_api_ms"),
                "is_error": bool(r.get("is_error")) or self.result is None}


class AgentHost(Plugin):
    NAMESPACE = "agenthost"
    SETTINGS = (
        setting("enabled", bool, default=False, scope="worker", env="ROOK_AGENTHOST",
                apply="restart", label="House agent host",
                help="On: this worker may run the hub's home agent turns with its local, "
                     "logged-in Claude Code (Manage > Home agent > Agent host). Off by default."),
    )

    def __init__(self) -> None:
        super().__init__()
        self._sem = asyncio.Semaphore(MAX_PARALLEL)
        self._running: dict[str, asyncio.subprocess.Process] = {}
        self._done: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._version = ""
        self._logged_in: bool | None = None
        self._probe: asyncio.Task | None = None

    def available(self) -> bool:
        return bool(self.settings.get("enabled")) and _claude() is not None

    async def start(self) -> None:
        self._probe = asyncio.create_task(self._refresh())

    async def stop(self) -> None:
        if self._probe is not None:
            self._probe.cancel()
            await asyncio.gather(self._probe, return_exceptions=True)
        for proc in list(self._running.values()):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def _cmd(self, *args: str, timeout: float = 20.0) -> str:
        exe = _claude()
        if not exe:
            return ""
        proc = await asyncio.create_subprocess_exec(
            exe, *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={**os.environ, "HOME": _home()})
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return ""
        return out.decode("utf-8", "replace")

    async def _refresh(self) -> dict:
        """Claude Code's version and whether this host is logged in."""
        with contextlib.suppress(Exception):
            self._version = (await self._cmd("--version")).strip().split(" ")[0]
        try:
            st = json.loads(await self._cmd("auth", "status") or "{}")
            self._logged_in = bool(st.get("loggedIn"))
        except (ValueError, AttributeError):
            self._logged_in = None
        return {"version": self._version, "logged_in": self._logged_in}

    def heartbeat(self) -> dict | None:
        # hb.agenthost: what the hub's host picker shows (Manage > Home agent).
        return {"claude": self._version or None, "logged_in": self._logged_in,
                "running": len(self._running), "max_parallel": MAX_PARALLEL}

    @capability("status", risk="read")
    async def _status(self) -> dict:
        """Claude Code on this host: version, login state and running turns.
        Returns {ok, claude, logged_in, running, max_parallel}."""
        await self._refresh()
        return {"ok": True, "claude": self._version or None, "logged_in": self._logged_in,
                "running": sorted(self._running), "max_parallel": MAX_PARALLEL}

    @capability("turn", risk="exec", timeout=None)
    async def _turn(self, turn_id: str, prompt: str, system: str, mcp_url: str,
                    mcp_token: str, model: str = "haiku", timeout_s: float = 180.0,
                    max_tool_calls: int = 20, effort: str | None = None,
                    oauth_token: str | None = None) -> dict:
        """Run one house agent turn with Claude Code; returns its reply.

        ``prompt`` (stdin) and ``system`` (the whole system prompt) come from
        the hub; the only tools are the hub's, from ``mcp_url`` with bearer
        ``mcp_token``. Ends the turn after ``max_tool_calls`` tool calls or
        ``timeout_s``. ``oauth_token``: a ``claude setup-token`` token instead
        of this host's login. A retry with the same ``turn_id`` gets the first
        run's reply. Returns {ok, text, model, tools_used, num_turns, usage,
        cost_usd, api_ms, latency_ms} or {ok: false, error}."""
        if not _ID.match(str(turn_id or "")):
            fail("turn_id: 1-64 of A-Z a-z 0-9 _ . -")
        if not _MODEL.match(str(model or "")):
            fail("model: an alias (haiku, sonnet, opus) or a model id")
        if effort is not None and effort not in _EFFORT:
            fail(f"effort: one of {', '.join(_EFFORT)}")
        if not str(prompt or "").strip():
            fail("prompt is empty")
        if len(prompt) > MAX_PROMPT or len(system or "") > MAX_SYSTEM:
            fail("prompt or system prompt too long")
        if not str(system or "").strip():
            fail("system is empty (an empty one would load Claude Code's own)")
        if not str(mcp_url or "").startswith(("https://", "http://")) or not mcp_token:
            fail("mcp_url and mcp_token are required")
        if turn_id in self._done:
            return self._done[turn_id]
        if turn_id in self._inflight:
            return await asyncio.shield(self._inflight[turn_id])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[turn_id] = fut
        try:
            out = await self._run(turn_id, prompt, system, mcp_url, mcp_token, model,
                                  max(5.0, min(float(timeout_s), 1800.0)),
                                  max(0, min(int(max_tool_calls), 100)), effort, oauth_token)
        except Exception as e:  # noqa: BLE001 - a failed turn is a reply, not a crash
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            self._inflight.pop(turn_id, None)
        self._done[turn_id] = out
        while len(self._done) > KEEP_RESULTS:
            self._done.popitem(last=False)
        if not fut.done():
            fut.set_result(out)
        return out

    async def _run(self, turn_id: str, prompt: str, system: str, mcp_url: str, mcp_token: str,
                   model: str, timeout_s: float, max_calls: int, effort: str | None,
                   oauth_token: str | None) -> dict:
        exe = _claude()
        if not exe:
            return {"ok": False, "error": "claude is not installed on this host"}
        base = self.data_dir
        cwd = base / "work"
        cwd.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="turn-", dir=base))
        os.chmod(tmp, 0o700)
        try:
            cfg = tmp / "mcp.json"
            fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {SERVER: {
                    "type": "http", "url": mcp_url,
                    "headers": {"Authorization": f"Bearer {mcp_token}"}}}}, f)
            env = {**os.environ, "HOME": _home(), "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                   "DISABLE_AUTOUPDATER": "1"}
            env.pop("ANTHROPIC_API_KEY", None)
            if oauth_token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
            argv = build_argv(exe, model=model, system=system, mcp_config=str(cfg), effort=effort)
            async with self._sem:
                return await self._spawn(turn_id, argv, env, str(cwd), prompt, timeout_s, max_calls)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _spawn(self, turn_id: str, argv: list[str], env: dict, cwd: str, prompt: str,
                     timeout_s: float, max_calls: int) -> dict:
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024, start_new_session=True)
        self._running[turn_id] = proc
        turn = Turn()
        stopped = ""
        err_tail: collections.deque = collections.deque(maxlen=20)

        async def pump_out() -> None:
            nonlocal stopped
            assert proc.stdout is not None
            async for raw in proc.stdout:
                turn.feed(raw.decode("utf-8", "replace"))
                if len(turn.tools) > max_calls and not stopped:
                    stopped = f"stopped after {max_calls} tool calls"
                    with contextlib.suppress(ProcessLookupError):
                        proc.terminate()

        async def pump_err() -> None:
            assert proc.stderr is not None
            async for raw in proc.stderr:
                err_tail.append(raw.decode("utf-8", "replace").rstrip()[:300])

        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            try:
                await asyncio.wait_for(asyncio.gather(pump_out(), pump_err(), proc.wait()),
                                       timeout_s)
            except asyncio.TimeoutError:
                stopped = f"timed out after {timeout_s:g}s"
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), 5)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        finally:
            self._running.pop(turn_id, None)
        out = turn.reply()
        out["latency_ms"] = round((time.monotonic() - t0) * 1000)
        if stopped:
            return {"ok": False, "error": stopped, **out}
        if proc.returncode != 0 or out["is_error"]:
            why = out["text"] if out["is_error"] and out["text"] else \
                next((ln for ln in reversed(err_tail) if ln), f"claude exited {proc.returncode}")
            return {"ok": False, "error": str(why)[:300], **out}
        out.pop("is_error", None)
        return {"ok": True, **out}

    @capability("cancel", risk="exec")
    async def _cancel(self, turn_id: str) -> dict:
        """Stop a running turn. Returns {ok, stopped}."""
        proc = self._running.get(str(turn_id or ""))
        if proc is None:
            return {"ok": True, "stopped": False}
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        return {"ok": True, "stopped": True}


PLUGIN = AgentHost
