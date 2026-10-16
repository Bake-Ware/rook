"""agenthost.* — run house agent turns with a local agent harness.

The hub's home agent can use an agent harness on a worker as its model
backend (docs/design/huddles.md 3): Claude Code, Codex or OpenCode, whichever
is installed here. The hub stays thin: each turn runs here as one headless
process, and its only tools come from the hub, over a per-turn MCP endpoint
(``mcp_url`` with a bearer ``mcp_token`` that dies with the turn). Each
harness runs with its own tools, settings, plugins, MCP servers and session
files switched off as far as it allows:

* **claude**: ``claude -p --output-format stream-json --system-prompt <s>
  --tools "" --setting-sources "" --strict-mcp-config --mcp-config <file>
  --allowedTools mcp__home --permission-mode dontAsk
  --no-session-persistence --disable-slash-commands``.
* **codex**: ``codex exec --json --ephemeral --ignore-user-config
  --ignore-rules -s read-only`` with approvals off, web search, apply_patch
  and the shell and other built-in tool features off, the system prompt as
  ``developer_instructions`` and the hub as the only MCP server (bearer from
  the environment).
* **opencode**: ``opencode run --format json --pure --agent rookhome`` with a
  generated config in a private config dir (the user's own config, with its
  MCP servers, is not loaded; only its model providers are copied in), every
  tool but the hub's off and edit/bash/webfetch denied.

The prompt goes in on stdin. Auth is the harness's own login on this host
(for claude, optionally a ``claude setup-token`` token passed by the hub).

The plugin is on wherever one of the harnesses is installed (the hub offers
the host in Manage > Home agent; nothing runs here until someone picks it).
Switch it off for a worker with ``agenthost.enabled`` or ``ROOK_AGENTHOST=0``.
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
from typing import Any

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
#: How often the harness versions and logins are re-read for the heartbeat.
PROBE_S = 300.0
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MODEL = re.compile(r"^[A-Za-z0-9_.:/\[\]-]{1,120}$")
_EFFORT = ("low", "medium", "high", "xhigh", "max")
#: The tool server name in every generated config.
SERVER = "home"
TOKEN_ENV = "ROOK_TURN_TOKEN"


def _home() -> str:
    """The service user's home (systemd units sometimes leave HOME unset)."""
    h = os.environ.get("HOME")
    if h:
        return h
    with contextlib.suppress(Exception):
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    return os.path.expanduser("~")


def _find(name: str, *extra: str) -> str | None:
    """A harness binary: PATH first, then where its installer puts it."""
    exe = which(name)
    if exe:
        return exe
    for rel in (f".local/bin/{name}", *extra):
        p = Path(_home()) / rel
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def _toml_str(s: str) -> str:
    """A TOML basic string (JSON escaping is valid TOML for these)."""
    return json.dumps(s, ensure_ascii=True)


def strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (opencode.jsonc) as JSON."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
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


class Turn:
    """What one run said, folded as its JSON lines arrive."""

    def __init__(self) -> None:
        self.model = ""
        self.texts: list[str] = []
        self.tools: list[str] = []
        self.final: str | None = None
        self.usage: dict = {}
        self.cost: float | None = None
        self.api_ms: int | None = None
        self.num_turns: int | None = None
        self.error = ""
        self.done = False

    def reply(self) -> dict:
        text = self.final if self.final is not None else (self.texts[-1] if self.texts else "")
        return {"text": (text or "")[:TEXT_CHARS], "model": self.model,
                "tools_used": list(self.tools), "num_turns": self.num_turns,
                "usage": self.usage, "cost_usd": self.cost, "api_ms": self.api_ms,
                "is_error": bool(self.error) or not self.done}


def _tool_name(name: str) -> str:
    for prefix in (f"mcp__{SERVER}__", f"{SERVER}_", f"{SERVER}."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


# -- harnesses -------------------------------------------------------------------------

class Harness:
    name = ""
    binary = ""
    extra_paths: tuple[str, ...] = ()

    def exe(self) -> str | None:
        return _find(self.binary, *self.extra_paths)

    async def probe(self, run) -> dict:
        """``{version, logged_in}``; ``run(argv) -> (code, stdout)``."""
        code, out = await run([self.exe(), "--version"])
        m = re.search(r"\d+(\.\d+)+", out or "")
        return {"version": m.group(0) if m else (out.strip()[:40] or None), "logged_in": None}

    def prepare(self, tmp: Path, cwd: str, *, model: str, system: str, mcp_url: str,
                effort: str | None) -> tuple[list[str], dict]:
        raise NotImplementedError

    def feed(self, turn: Turn, ev: dict) -> None:
        raise NotImplementedError

    async def models(self, run) -> list[str]:
        return []


class ClaudeHarness(Harness):
    name, binary = "claude", "claude"

    async def probe(self, run) -> dict:
        out = await super().probe(run)
        code, text = await run([self.exe(), "auth", "status"])
        with contextlib.suppress(ValueError, AttributeError):
            out["logged_in"] = bool(json.loads(text or "{}").get("loggedIn"))
        return out

    def prepare(self, tmp, cwd, *, model, system, mcp_url, effort):
        cfg = tmp / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {SERVER: {
            "type": "http", "url": mcp_url,
            "headers": {"Authorization": "Bearer ${%s}" % TOKEN_ENV}}}}), encoding="utf-8")
        argv = [self.exe(), "-p", "--output-format", "stream-json", "--verbose",
                "--model", model or "haiku", "--system-prompt", system,
                "--tools", "", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", str(cfg),
                "--allowedTools", f"mcp__{SERVER}",
                "--permission-mode", "dontAsk",
                "--no-session-persistence", "--disable-slash-commands"]
        if effort:
            argv += ["--effort", effort]
        return argv, {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1"}

    def feed(self, turn, ev):
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            turn.model = str(ev.get("model") or "")
        elif kind == "assistant":
            msg = ev.get("message") or {}
            turn.model = str(msg.get("model") or turn.model)
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    turn.texts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    turn.tools.append(_tool_name(str(block.get("name") or "")))
        elif kind == "result":
            turn.done = True
            if isinstance(ev.get("result"), str):
                turn.final = ev["result"]
            if ev.get("is_error"):
                turn.error = str(ev.get("result") or "the model returned an error")
            usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            turn.usage = {k: usage[k] for k in ("input_tokens", "output_tokens",
                                                "cache_creation_input_tokens",
                                                "cache_read_input_tokens") if k in usage}
            turn.cost = ev.get("total_cost_usd")
            turn.api_ms = ev.get("duration_api_ms")
            turn.num_turns = ev.get("num_turns")

    async def models(self, run):
        return ["haiku", "sonnet", "opus"]


#: Codex built-in tool features turned off for house agent turns (unknown
#: names are ignored by this Codex version, so this list may run ahead).
#: ``code_mode_host`` stays on: Codex calls MCP tools through it.
CODEX_OFF = ("shell_tool", "unified_exec", "multi_agent", "image_generation",
             "browser_use", "browser_use_external", "computer_use", "plugins", "apps",
             "view_image", "hooks", "skill_search", "tool_suggest", "sleep_tool", "goals",
             "memories", "in_app_browser", "in_app_local_automation", "workspace_dependencies")


class CodexHarness(Harness):
    name, binary = "codex", "codex"

    async def probe(self, run) -> dict:
        out = await super().probe(run)
        code, text = await run([self.exe(), "login", "status"])
        out["logged_in"] = code == 0 and "logged in" in (text or "").lower()
        return out

    def prepare(self, tmp, cwd, *, model, system, mcp_url, effort):
        argv = [self.exe(), "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--ignore-user-config", "--ignore-rules", "-s", "read-only", "-C", cwd,
                "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                "-c", "include_apply_patch_tool=false",
                "-c", f"developer_instructions={_toml_str(system)}",
                "-c", f"mcp_servers.{SERVER}.url={_toml_str(mcp_url)}",
                "-c", f'mcp_servers.{SERVER}.bearer_token_env_var="{TOKEN_ENV}"',
                # The hub decides what each tool may do; Codex must not hold
                # the calls for an approval nobody can give.
                "-c", f'mcp_servers.{SERVER}.default_tools_approval_mode="approve"']
        for feature in CODEX_OFF:
            argv += ["-c", f"features.{feature}=false"]
        if model:
            argv += ["-m", model]
        if effort:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        return argv + ["-"], {}

    def feed(self, turn, ev):
        kind = ev.get("type")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if kind == "item.started" and item.get("type") in ("mcp_tool_call", "command_execution"):
            name = str(item.get("tool") or item.get("command") or item.get("type"))
            turn.tools.append(_tool_name(name))
        elif kind == "item.completed" and item.get("type") == "agent_message":
            if item.get("text"):
                turn.texts.append(str(item["text"]))
        elif kind == "turn.completed":
            turn.done = True
            usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            turn.usage = {k: usage[k] for k in ("input_tokens", "cached_input_tokens",
                                                "output_tokens", "reasoning_output_tokens")
                          if k in usage}
        elif kind in ("turn.failed", "error"):
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            turn.error = str(err.get("message") or ev.get("message") or "codex failed")[:300]

    async def models(self, run):
        code, text = await run([self.exe(), "debug", "models"])
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            return [m["slug"] for m in json.loads(text)["models"] if m.get("visibility") == "list"]
        return []


class OpenCodeHarness(Harness):
    name, binary = "opencode", "opencode"
    extra_paths = (".opencode/bin/opencode",)
    AGENT = "rookhome"

    @staticmethod
    def user_config() -> dict:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path(_home()) / ".config") / "opencode"
        for name in ("opencode.json", "opencode.jsonc", "config.json"):
            p = base / name
            if p.is_file():
                with contextlib.suppress(OSError, ValueError):
                    return json.loads(strip_jsonc(p.read_text(encoding="utf-8")))
        return {}

    def prepare(self, tmp, cwd, *, model, system, mcp_url, effort):
        user = self.user_config()
        only_home = {"*": False, f"{SERVER}_*": True}
        cfg: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json", "autoupdate": False,
            "share": "disabled",
            "mcp": {SERVER: {"type": "remote", "url": mcp_url, "enabled": True,
                             "headers": {"Authorization": "Bearer {env:%s}" % TOKEN_ENV}}},
            "tools": only_home,
            "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
            "agent": {self.AGENT: {"mode": "primary", "prompt": system, "tools": only_home}}}
        # The user's model providers (and default model) only: never their
        # MCP servers, plugins, agents or instructions.
        for key in ("provider", "model", "small_model", "enabled_providers",
                    "disabled_providers"):
            if key in user:
                cfg[key] = user[key]
        d = tmp / "config" / "opencode"
        d.mkdir(parents=True)
        (d / "opencode.json").write_text(json.dumps(cfg), encoding="utf-8")
        argv = [self.exe(), "run", "--format", "json", "--pure", "--agent", self.AGENT]
        if model:
            argv += ["-m", model]
        if effort:
            argv += ["--variant", effort]
        return argv, {"XDG_CONFIG_HOME": str(tmp / "config"), "OPENCODE_DISABLE_AUTOUPDATE": "1"}

    def feed(self, turn, ev):
        kind = ev.get("type")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if kind == "text" and part.get("text") and not part.get("synthetic"):
            turn.texts.append(str(part["text"]))
        elif kind == "tool_use":
            turn.tools.append(_tool_name(str(part.get("tool") or "")))
        elif kind == "step_finish":
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            for k in ("input", "output", "reasoning"):
                if isinstance(tok.get(k), int):
                    turn.usage[f"{k}_tokens"] = turn.usage.get(f"{k}_tokens", 0) + tok[k]
            if isinstance(part.get("cost"), (int, float)):
                turn.cost = (turn.cost or 0) + part["cost"]
            if part.get("reason") == "stop":
                turn.done = True
        elif kind == "error":
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            turn.error = str(data.get("message") or err.get("name") or "opencode failed")[:300]

    async def models(self, run):
        code, text = await run([self.exe(), "models"])
        return [ln.strip() for ln in (text or "").splitlines() if "/" in ln and " " not in ln.strip()]


HARNESSES: dict[str, Harness] = {h.name: h for h in (ClaudeHarness(), CodexHarness(),
                                                       OpenCodeHarness())}


class AgentHost(Plugin):
    NAMESPACE = "agenthost"
    SETTINGS = (
        setting("enabled", bool, default=True, scope="worker", env="ROOK_AGENTHOST",
                apply="restart", label="House agent host",
                help="On (default): when Claude Code, Codex or OpenCode is installed, this "
                     "worker offers to run the hub's home agent turns with it (picked on Manage "
                     "> Home agent; nothing runs here until someone picks it). Off: never."),
    )

    def __init__(self) -> None:
        super().__init__()
        self._sem = asyncio.Semaphore(MAX_PARALLEL)
        self._running: dict[str, asyncio.subprocess.Process] = {}
        self._done: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._found: dict[str, dict] = {}
        self._probe_task: asyncio.Task | None = None

    def installed(self) -> list[str]:
        return [n for n, h in HARNESSES.items() if h.exe()]

    def available(self) -> bool:
        return bool(self.settings.get("enabled", True)) and bool(self.installed())

    async def start(self) -> None:
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def stop(self) -> None:
        if self._probe_task is not None:
            self._probe_task.cancel()
            await asyncio.gather(self._probe_task, return_exceptions=True)
        for proc in list(self._running.values()):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    @staticmethod
    async def _run_cmd(argv: list, timeout: float = 30.0) -> tuple[int, str]:
        if not argv or not argv[0]:
            return 127, ""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env={**os.environ, "HOME": _home()})
        except OSError:
            return 127, ""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return 124, ""
        return proc.returncode or 0, out.decode("utf-8", "replace")

    async def refresh(self) -> dict:
        """Each installed harness's version and login, for the heartbeat."""
        found = {}
        for name, h in HARNESSES.items():
            if not h.exe():
                continue
            try:
                found[name] = await h.probe(self._run_cmd)
            except Exception:  # noqa: BLE001 - a broken probe still lists the harness
                log.debug("agenthost: probing %s failed", name, exc_info=True)
                found[name] = {"version": None, "logged_in": None}
        self._found = found
        return found

    async def _probe_loop(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.refresh()
            await asyncio.sleep(PROBE_S)

    def heartbeat(self) -> dict | None:
        # hb.agenthost: what the hub's host picker shows (Manage > Home agent).
        return {"harnesses": self._found or {n: {} for n in self.installed()},
                "running": len(self._running), "max_parallel": MAX_PARALLEL}

    @capability("status", risk="read")
    async def _status(self) -> dict:
        """The agent harnesses on this host (claude, codex, opencode): version and
        login, re-read now; and the running turns.
        Returns {ok, harnesses: {name: {version, logged_in}}, running, max_parallel}."""
        return {"ok": True, "harnesses": await self.refresh(), "running": sorted(self._running),
                "max_parallel": MAX_PARALLEL}

    @capability("models", risk="read")
    async def _models(self, harness: str = "claude") -> dict:
        """Models a harness here can use (claude: aliases; codex: its catalog;
        opencode: ``opencode models``). Returns {ok, harness, models}."""
        h = HARNESSES.get(str(harness))
        if h is None or not h.exe():
            fail(f"harness {harness!r} is not installed here")
        return {"ok": True, "harness": h.name, "models": await h.models(self._run_cmd)}

    @capability("turn", risk="exec", timeout=None)
    async def _turn(self, turn_id: str, prompt: str, system: str, mcp_url: str,
                    mcp_token: str, harness: str = "claude", model: str = "",
                    timeout_s: float = 180.0, max_tool_calls: int = 20,
                    effort: str | None = None, oauth_token: str | None = None) -> dict:
        """Run one house agent turn with a local harness; returns its reply.

        ``harness``: claude, codex or opencode. ``prompt`` (stdin) and ``system``
        come from the hub; the only tools are the hub's, from ``mcp_url`` with
        bearer ``mcp_token``. Ends the turn after ``max_tool_calls`` tool calls
        or ``timeout_s``. ``model``: blank for the harness's default.
        ``oauth_token`` (claude only): a ``claude setup-token`` token instead of
        this host's login. A retry with the same ``turn_id`` gets the first
        run's reply. Returns {ok, text, model, tools_used, num_turns, usage,
        cost_usd, api_ms, latency_ms, harness} or {ok: false, error}."""
        h = HARNESSES.get(str(harness or ""))
        if h is None:
            fail(f"harness: one of {', '.join(HARNESSES)}")
        if not h.exe():
            fail(f"{h.name} is not installed on this host")
        if not _ID.match(str(turn_id or "")):
            fail("turn_id: 1-64 of A-Z a-z 0-9 _ . -")
        if model and not _MODEL.match(str(model)):
            fail("model: an alias or a model id")
        if effort is not None and effort not in _EFFORT:
            fail(f"effort: one of {', '.join(_EFFORT)}")
        if not str(prompt or "").strip():
            fail("prompt is empty")
        if len(prompt) > MAX_PROMPT or len(system or "") > MAX_SYSTEM:
            fail("prompt or system prompt too long")
        if not str(system or "").strip():
            fail("system is empty (an empty one would load the harness's own)")
        if not str(mcp_url or "").startswith(("https://", "http://")) or not mcp_token:
            fail("mcp_url and mcp_token are required")
        if turn_id in self._done:
            return self._done[turn_id]
        if turn_id in self._inflight:
            return await asyncio.shield(self._inflight[turn_id])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[turn_id] = fut
        try:
            out = await self._run(h, turn_id, prompt, system, mcp_url, mcp_token, model,
                                  max(5.0, min(float(timeout_s), 1800.0)),
                                  max(0, min(int(max_tool_calls), 100)), effort, oauth_token)
        except Exception as e:  # noqa: BLE001 - a failed turn is a reply, not a crash
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            self._inflight.pop(turn_id, None)
        out["harness"] = h.name
        if not out.get("ok"):
            # A login may have lapsed: re-read before the next heartbeat.
            asyncio.get_running_loop().create_task(self.refresh())
        self._done[turn_id] = out
        while len(self._done) > KEEP_RESULTS:
            self._done.popitem(last=False)
        if not fut.done():
            fut.set_result(out)
        return out

    async def _run(self, h: Harness, turn_id: str, prompt: str, system: str, mcp_url: str,
                   mcp_token: str, model: str, timeout_s: float, max_calls: int,
                   effort: str | None, oauth_token: str | None) -> dict:
        base = self.data_dir
        cwd = base / "work"
        cwd.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="turn-", dir=base))
        os.chmod(tmp, 0o700)
        try:
            argv, extra = h.prepare(tmp, str(cwd), model=model, system=system,
                                    mcp_url=mcp_url, effort=effort)
            env = {**os.environ, "HOME": _home(), **extra, TOKEN_ENV: mcp_token}
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("OPENAI_API_KEY", None)
            if oauth_token and h.name == "claude":
                env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
            async with self._sem:
                return await self._spawn(h, turn_id, argv, env, str(cwd), prompt, timeout_s,
                                         max_calls)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _spawn(self, h: Harness, turn_id: str, argv: list[str], env: dict, cwd: str,
                     prompt: str, timeout_s: float, max_calls: int) -> dict:
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
                try:
                    ev = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    h.feed(turn, ev)
                if len(turn.tools) > max_calls and not stopped:
                    stopped = f"stopped after {max_calls} tool calls"
                    with contextlib.suppress(ProcessLookupError):
                        proc.terminate()

        async def pump_err() -> None:
            assert proc.stderr is not None
            async for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    err_tail.append(line[:300])

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
        if proc.returncode == 0 and not turn.error and (turn.texts or turn.final):
            turn.done = True          # some harnesses end without a closing event
        out = turn.reply()
        out["latency_ms"] = round((time.monotonic() - t0) * 1000)
        if stopped:
            return {"ok": False, "error": stopped, **out}
        if proc.returncode != 0 or out["is_error"]:
            why = turn.error or next((ln for ln in reversed(err_tail) if ln),
                                     f"{h.name} exited {proc.returncode}")
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
