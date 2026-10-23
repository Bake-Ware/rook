"""One lane: a long-lived Claude Code process in a role directory (design 3.2).

``claude -p --input-format stream-json --output-format stream-json`` reads
user messages from stdin and answers each with a turn (``system/init``,
``assistant`` messages, a ``result``). The lane keeps the process, its
session id (resumed by id after any restart, never started fresh silently)
and the accounting of its turns:

* ``send`` starts a turn owned by an item (lane idle);
* ``steer`` writes a message into the running turn (Claude Code folds it in
  at the next tool boundary: one ``result``); a result with no owner (a steer
  that became its own turn) is reported with item ``steer``;
* ``interrupt`` is the stream-json control request;
* ``compact`` is the user message ``/compact <instructions>``;
* a crash restarts with backoff, resumed by id; idle sleep stops the
  process, and the next input starts it again.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import roledir

if TYPE_CHECKING:  # pragma: no cover
    from .host import Host

log = logging.getLogger("rook.agents.lane")

STATES = ("starting", "idle", "busy", "sleeping", "stopped", "crashed")
LANES = ("main", "voice")
TEXT_CHARS = 40000
STREAM_LIMIT = 16 * 1024 * 1024
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL = re.compile(r"^[A-Za-z0-9_.:/\[\]-]{1,120}$")
TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
#: Environment names a spec may not set (the process, not the agent, owns them).
ENV_DENY = ("PATH", "HOME", "USER", "SHELL", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS",
            "CLAUDE_CONFIG_DIR")
#: Removed from every child's environment.
SCRUB = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
         "CLAUDE_CODE_SSE_PORT")
MCP_PREFIX = re.compile(r"^mcp__[^_].*?__")
RESTARTED = "the session restarted"
STOPPED = "the session was stopped"


class LaneError(Exception):
    pass


class Busy(LaneError):
    def __init__(self) -> None:
        super().__init__("busy")


def tool_name(name: str) -> str:
    """A tool's own name (``mcp__<server>__`` stripped)."""
    return MCP_PREFIX.sub("", str(name or ""), count=1)


def check_env(env: Any) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise LaneError("env: {NAME: value}")
    out = {}
    for k, v in env.items():
        k = str(k)
        if not ENV_NAME.match(k) or k in ENV_DENY or k.startswith("LD_") \
                or k.startswith("DYLD_"):
            raise LaneError(f"env: {k!r} may not be set")
        out[k] = "" if v is None else str(v)
    return out


def base_env(token_env: str = "") -> dict[str, str]:
    env = dict(os.environ)
    for k in (*SCRUB, token_env):
        if k:
            env.pop(k, None)
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


@dataclass
class LaneSpec:
    model: str = ""
    effort: str = ""
    tools: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    partial: bool = False
    session_id: str = ""
    idle_sleep_s: float = 0.0
    thinking: bool | None = None

    @classmethod
    def parse(cls, d: Any) -> "LaneSpec":
        if not isinstance(d, dict):
            raise LaneError("LaneSpec: an object")
        s = cls()
        s.model = str(d.get("model") or "")
        if s.model and not MODEL.match(s.model):
            raise LaneError("model: an alias or a model id")
        s.effort = str(d.get("effort") or "")
        if s.effort and s.effort not in EFFORTS:
            raise LaneError(f"effort: one of {', '.join(EFFORTS)}")
        tools = d.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(t, str) and TOOL.match(t)
                                                  for t in tools):
            raise LaneError("tools: built-in tool names")
        s.tools = list(tools)
        s.env = check_env(d.get("env"))
        s.partial = bool(d.get("partial"))
        sid = str(d.get("session_id") or "")
        s.session_id = sid if roledir.SESSION_ID.match(sid) else ""
        try:
            s.idle_sleep_s = max(0.0, float(d.get("idle_sleep_s") or 0))
        except (TypeError, ValueError):
            raise LaneError("idle_sleep_s: seconds") from None
        th = d.get("thinking")
        s.thinking = None if th is None else bool(th)
        return s

    def key(self) -> tuple:
        """What a running process depends on (not the hub's session record)."""
        return (self.model, self.effort, tuple(self.tools), tuple(sorted(self.env.items())),
                self.partial, self.thinking)


class Turn:
    """One turn on a lane, folded as its events arrive. ``kind``: item, steer,
    voice, compact. ``sink(kind, **fields)`` gets deltas and tools (voice)."""

    def __init__(self, item: str | None, kind: str = "item",
                 sink: Callable[..., None] | None = None) -> None:
        self.item = item
        self.kind = kind
        self.sink = sink
        self.t0 = time.monotonic()
        self.first_ms: int | None = None
        self.texts: list[str] = []
        self.tools: list[str] = []
        self.deltas: list[str] = []
        self.usage: dict = {}
        self.last_uuid: str | None = None
        self.interrupted = False
        self.streamed_block = False
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()

    def finish(self, result: dict) -> None:
        if not self.done.done():
            self.done.set_result(result)


def context_of(usage: dict) -> int:
    return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens"))


def _usage(usage: Any) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    return {k: usage[k] for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                  "cache_read_input_tokens") if k in usage}


def thinking_of(usage: Any) -> int:
    usage = usage if isinstance(usage, dict) else {}
    det = usage.get("output_tokens_details")
    return int((det or {}).get("thinking_tokens") or 0) if isinstance(det, dict) else 0


async def kill_group(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    """SIGTERM the process group (Claude Code and its MCP servers), then SIGKILL."""
    if proc.returncode is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), grace)
            return
        except asyncio.TimeoutError:
            continue


class Lane:
    def __init__(self, host: "Host", role: str, name: str, cwd: Path, spec: LaneSpec) -> None:
        self.host = host
        self.role = role
        self.name = name
        self.cwd = cwd
        self.spec = spec
        self.sessions = roledir.Sessions(cwd if name == "main" else cwd.parent)
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.turn: Turn | None = None
        rec = self.sessions.get(name)
        self.session_id: str | None = rec.get("id") or spec.session_id or None
        self.last_completed_uuid: str | None = rec.get("last_turn_uuid")
        self.context_tokens = int(rec.get("context_tokens") or 0)
        self.last_error = ""
        self.idle_since = time.monotonic()
        self.want_reload = False
        self._stopping = False
        self._backoff = host.cfg.restart_backoff[0]
        self._restart_task: asyncio.Task | None = None
        self._sleep_task: asyncio.Task | None = None
        self._reload_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self._resume_id: str | None = None
        self._reset: dict | None = None
        self._force_fresh: str | None = None
        self._reported = False
        self._err_tail: collections.deque = collections.deque(maxlen=8)
        self._tasks: list[asyncio.Task] = []
        self._req = 0

    # -- state -------------------------------------------------------------------
    @property
    def key(self) -> str:
        return f"{self.role}/{self.name}"

    def _emit(self, kind: str, **fields) -> None:
        self.host.emit(self.role, self.name, kind, **fields)

    def _set_state(self, state: str, **extra) -> None:
        if state == self.state and not extra:
            return
        self.state = state
        if state in ("idle", "sleeping", "stopped", "crashed"):
            self._idle.set()
        else:
            self._idle.clear()
        if state == "idle":
            self.idle_since = time.monotonic()
        self._emit("state", state=state, session_id=self.session_id,
                   context_tokens=self.context_tokens, **extra)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def status(self) -> dict:
        return {"state": self.state, "session_id": self.session_id,
                "context_tokens": self.context_tokens,
                "busy_item": (self.turn.item if self.turn else None),
                "idle_s": (round(time.monotonic() - self.idle_since, 1)
                           if self.state in ("idle", "sleeping") else 0),
                "last_error": self.last_error or None}

    async def wait_idle(self) -> None:
        while self.turn is not None or self.state in ("busy", "starting"):
            await self._idle.wait()
            if self.turn is not None or self.state in ("busy", "starting"):
                await asyncio.sleep(0.02)

    # -- process ------------------------------------------------------------------
    def argv(self, resume: str | None) -> list[str]:
        s = self.spec
        cfg = self.host.cfg
        argv = [cfg.claude, "-p", "--input-format", "stream-json", "--output-format",
                "stream-json", "--verbose"]
        if s.partial or self.name == "voice":
            argv.append("--include-partial-messages")
        argv += ["--setting-sources", "project,local",
                 "--settings", str(self.cwd / ".claude" / "settings.json"),
                 "--strict-mcp-config", "--mcp-config", str(self.cwd / ".mcp.json"),
                 "--permission-mode", "dontAsk", "--tools", ",".join(s.tools)]
        if s.model:
            argv += ["--model", s.model]
        if s.effort:
            argv += ["--effort", s.effort]
        if resume:
            argv += ["--resume", resume]
        return argv

    def env(self) -> dict[str, str]:
        env = base_env(self.host.cfg.token_env)
        if self.spec.thinking is False:
            env["MAX_THINKING_TOKENS"] = "0"
        env.update(self.spec.env)
        return env

    def _ensure_files(self) -> None:
        # Claude Code refuses a missing --settings / --mcp-config file.
        settings = self.cwd / ".claude" / "settings.json"
        if not settings.exists():
            roledir.write_text(settings, "{}\n")
        mcp = self.cwd / ".mcp.json"
        if not mcp.exists():
            roledir.write_text(mcp, '{"mcpServers": {}}\n')

    def _pick_resume(self) -> str | None:
        """The id to resume: the host's record, else the hub's. A missing
        transcript means a fresh start, reported with ``reset_reason``."""
        rec = self.sessions.get(self.name)
        sid = rec.get("id") or self.spec.session_id or None
        if self._force_fresh:
            self._reset = {"reset_reason": self._force_fresh, "previous_session_id": sid}
            self._force_fresh = None
            return None
        if not sid:
            return None
        if not roledir.transcript_exists(self.host.cfg.claude_config_dir, sid):
            self._reset = {"reset_reason": f"the transcript of session {sid} is gone",
                           "previous_session_id": sid}
            return None
        return sid

    async def start(self) -> None:
        async with self._start_lock:
            if self.running:
                return
            self._cancel(self._restart_task)
            self._restart_task = None
            self._stopping = False
            self._reported = False
            self._reset = None
            self._err_tail.clear()
            resume = self._pick_resume()
            self._resume_id = resume
            self.session_id = resume or (None if self._reset else self.session_id)
            self.cwd.mkdir(parents=True, exist_ok=True)
            self._ensure_files()
            self._set_state("starting")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.argv(resume), cwd=str(self.cwd), env=self.env(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT,
                    start_new_session=True)
            except OSError as e:
                self.proc = None
                self._crashed(f"cannot start claude: {e.strerror or e}")
                return
            self.want_reload = False
            proc = self.proc
            self._tasks = [asyncio.create_task(self._read_out(proc)),
                           asyncio.create_task(self._read_err(proc))]
            self._set_state("idle")
            self._arm_sleep()

    async def ensure_running(self) -> None:
        if self.running:
            return
        if self.state == "crashed" and self._restart_task is not None \
                and not self._restart_task.done():
            raise LaneError("the session is restarting")
        await self.start()
        if not self.running:
            raise LaneError(self.last_error or "the session could not start")

    async def _write(self, obj: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise LaneError("the session is not running")
        try:
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise LaneError("the session is not running") from e

    async def _write_user(self, text: str) -> None:
        await self._write({"type": "user", "message": {"role": "user", "content": text}})

    # -- turns --------------------------------------------------------------------
    async def begin(self, turn: Turn, text: str) -> Turn:
        """Start ``turn`` with ``text``; the lane must be idle."""
        await self.ensure_running()
        if self.turn is not None or self.state == "busy":
            raise Busy()
        self._cancel(self._sleep_task)
        self.turn = turn
        turn.t0 = time.monotonic()
        try:
            await self._write_user(text)
        except LaneError:
            self.turn = None
            raise
        self._set_state("busy", item=turn.item)
        if turn.kind in ("item", "steer"):
            self._emit("turn_start", item=turn.item)
        return turn

    async def send(self, item: str, text: str) -> Turn:
        return await self.begin(Turn(item, "item"), text)

    async def steer(self, text: str) -> dict:
        await self.ensure_running()
        if self.turn is not None:
            await self._write_user(text)
            return {"steered": True, "item": self.turn.item}
        await self.begin(Turn("steer", "steer"), text)
        return {"steered": False, "item": "steer"}

    async def interrupt(self, only: Turn | None = None) -> bool:
        """Interrupt the running turn (``only``: if it is that one)."""
        if self.turn is None or not self.running or (only is not None and self.turn is not only):
            return False
        self.turn.interrupted = True
        self._req += 1
        await self._write({"type": "control_request", "request_id": f"int-{self._req}",
                           "request": {"subtype": "interrupt"}})
        return True

    async def compact(self, instructions: str) -> Turn:
        text = "/compact" + (f" {instructions.strip()}" if (instructions or "").strip() else "")
        return await self.begin(Turn(None, "compact"), text)

    async def reload(self) -> str:
        """Restart at the next idle moment, resumed by id (new files, new spec)."""
        if not self.running:
            self.want_reload = False
            return "next start"
        self.want_reload = True
        if self.turn is None and self.state == "idle":
            await self._restart_now()
            return "restarted"
        return "at next idle"

    async def _restart_now(self) -> None:
        self.want_reload = False
        await self._stop_proc()
        await self.start()

    async def stop(self, state: str = "stopped", reason: str = STOPPED) -> None:
        self._cancel(self._restart_task)
        self._restart_task = None
        self._cancel(self._sleep_task)
        await self._stop_proc()
        self._fail_turn(reason)
        self._set_state(state)

    async def _stop_proc(self) -> None:
        proc = self.proc
        if proc is None:
            return
        self._stopping = True
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                await kill_group(proc)
        for t in self._tasks:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, 2)
        self._tasks = []
        self.proc = None

    def _arm_sleep(self) -> None:
        self._cancel(self._sleep_task)
        self._sleep_task = None
        if self.spec.idle_sleep_s > 0 and self.state == "idle":
            self._sleep_task = asyncio.create_task(self._sleep_after(self.spec.idle_sleep_s))

    async def _sleep_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.state == "idle" and self.turn is None and self.running:
            self._sleep_task = None
            await self.stop(state="sleeping")

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _fail_turn(self, error: str) -> None:
        turn, self.turn = self.turn, None
        if turn is None:
            return
        res = {"item": turn.item, "ok": False, "text": "".join(turn.deltas) or
               (turn.texts[-1] if turn.texts else ""), "error": error,
               "usage": {}, "context_tokens": self.context_tokens, "cost_usd": None,
               "duration_ms": round((time.monotonic() - turn.t0) * 1000), "num_turns": 0,
               "thinking_tokens": 0}
        if turn.kind in ("item", "steer"):
            self._emit("result", **res)
        turn.finish(res)

    # -- reading ------------------------------------------------------------------
    async def _read_err(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        with contextlib.suppress(Exception):
            async for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._err_tail.append(line[:300])

    async def _read_out(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError:       # a line over the stream limit
                    continue
                if not raw:
                    break
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    try:
                        self._on_event(ev)
                    except Exception:  # noqa: BLE001 - one bad event must not end the lane
                        log.exception("lane %s: event handling failed", self.key)
        finally:
            await proc.wait()
            if self.proc is proc and not self._stopping:
                # Let the stderr reader catch the last lines.
                await asyncio.sleep(0.05)
                self._exited(proc)

    def _exited(self, proc: asyncio.subprocess.Process) -> None:
        tail = " | ".join(list(self._err_tail)[-3:])[:500]
        if self._resume_id and not self._reported and re.search(
                r"no conversation found|session.*not found", tail, re.I):
            self._force_fresh = f"could not resume session {self._resume_id}: {tail[:200]}"
        self.proc = None
        self._fail_turn(RESTARTED)
        self._crashed(tail or f"claude exited {proc.returncode}")

    def _crashed(self, error: str) -> None:
        self.last_error = error
        self._cancel(self._sleep_task)
        delay = self._backoff
        self._backoff = min(self._backoff * 2, self.host.cfg.restart_backoff[1])
        self._set_state("crashed", error=error, restart_in_s=delay)
        self._restart_task = asyncio.create_task(self._restart_after(delay))

    async def _restart_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._restart_task = None
        await self.start()

    def _implicit_turn(self) -> Turn:
        # A turn nobody started here: a steer that arrived as the last turn
        # ended became its own turn.
        turn = Turn("steer", "steer")
        self.turn = turn
        self._cancel(self._sleep_task)
        self._set_state("busy", item="steer")
        self._emit("turn_start", item="steer")
        return turn

    def _on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        sub = ev.get("subtype")
        if kind == "system" and sub == "init":
            self._on_init(ev)
            if self.turn is None:
                self._implicit_turn()
        elif kind == "stream_event":
            if ev.get("parent_tool_use_id"):
                return
            inner = ev.get("event") if isinstance(ev.get("event"), dict) else {}
            turn = self.turn or self._implicit_turn()
            if inner.get("type") == "content_block_start":
                block = inner.get("content_block") or {}
                if block.get("type") == "text" and turn.deltas:
                    turn.streamed_block = True
            elif inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    text = str(delta["text"])
                    if turn.streamed_block:
                        turn.streamed_block = False
                        if not text[:1].isspace() and not turn.deltas[-1][-1:].isspace():
                            text = " " + text
                    if turn.first_ms is None:
                        turn.first_ms = round((time.monotonic() - turn.t0) * 1000)
                    turn.deltas.append(text)
                    if turn.sink:
                        turn.sink("delta", text=text)
        elif kind == "assistant":
            if ev.get("parent_tool_use_id"):
                return
            turn = self.turn or self._implicit_turn()
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            if isinstance(msg.get("usage"), dict):
                turn.usage = msg["usage"]
                self.context_tokens = context_of(msg["usage"])
            if ev.get("uuid"):
                turn.last_uuid = str(ev["uuid"])
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    text = str(block["text"])
                    turn.texts.append(text)
                    if turn.kind in ("item", "steer"):
                        self._emit("text", item=turn.item, text=text[:TEXT_CHARS])
                elif block.get("type") == "tool_use":
                    name = tool_name(block.get("name"))
                    turn.tools.append(name)
                    if turn.kind in ("item", "steer"):
                        self._emit("tool", item=turn.item, name=name)
                    if turn.sink:
                        turn.sink("tool", name=name)
        elif kind == "system" and sub == "compact_boundary":
            meta = ev.get("compact_metadata") if isinstance(ev.get("compact_metadata"),
                                                           dict) else {}
            post = meta.get("post_tokens")
            if isinstance(post, int):
                self.context_tokens = post
            self._emit("compacted", pre_tokens=meta.get("pre_tokens"), post_tokens=post,
                       trigger=meta.get("trigger"))
        elif kind == "result":
            self._on_result(ev)

    def _on_init(self, ev: dict) -> None:
        sid = str(ev.get("session_id") or "")
        if not sid:
            return
        if not self._reported:
            self._reported = True
            resumed = bool(self._resume_id)
            rec = self.sessions.get(self.name)
            if rec.get("id") == sid:
                rec["resumes"] = int(rec.get("resumes") or 0) + (1 if resumed else 0)
            else:
                rec = {"id": sid, "started": roledir.now(),
                       "resumes": 1 if resumed else 0,
                       "resets": int(rec.get("resets") or 0) + (1 if self._reset else 0)}
                self.last_completed_uuid = None
            self.session_id = sid
            with contextlib.suppress(OSError):
                self.sessions.put(self.name, rec)
            fields: dict[str, Any] = {"session_id": sid, "resumed": resumed}
            if self._reset:
                fields.update(self._reset)
            self._reset = None
            self._emit("session", **fields)
        elif sid != self.session_id:
            self.session_id = sid
            with contextlib.suppress(OSError):
                self.sessions.update(self.name, id=sid)

    def _on_result(self, ev: dict) -> None:
        turn = self.turn
        compact = (turn is not None and turn.kind == "compact") \
            or ev.get("local_command") == "compact"
        self.turn = None
        ok = not ev.get("is_error") and ev.get("subtype") in (None, "success")
        if compact:
            if not ok:
                self._emit("compacted", ok=False,
                           error=str(ev.get("result") or ev.get("subtype") or "failed")[:300])
            res = {"ok": ok, "compact": True}
            if turn is not None:
                turn.finish(res)
            self._after_turn(ok)
            return
        if turn is None:
            turn = Turn("steer", "steer")
        usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
        text = ev.get("result") if isinstance(ev.get("result"), str) else ""
        if not text and turn.texts:
            text = turn.texts[-1]
        error = None
        if not ok:
            if turn.interrupted:
                error = "interrupted"
            else:
                errs = ev.get("errors")
                error = (str(ev.get("result") or "") or
                         ("; ".join(map(str, errs)) if isinstance(errs, list) and errs else "")
                         or str(ev.get("subtype") or "the turn failed"))[:500]
        if ok and turn.last_uuid:
            self.last_completed_uuid = turn.last_uuid
        res = {"item": turn.item, "ok": ok, "text": text[:TEXT_CHARS], "usage": _usage(usage),
               "context_tokens": self.context_tokens, "cost_usd": ev.get("total_cost_usd"),
               "duration_ms": ev.get("duration_ms"), "num_turns": ev.get("num_turns"),
               "thinking_tokens": thinking_of(usage)}
        if error:
            res["error"] = error
        if turn.kind in ("item", "steer"):
            self._emit("result", **res)
        turn.finish(res)
        self._after_turn(ok)

    def _after_turn(self, ok: bool) -> None:
        if ok:
            self._backoff = self.host.cfg.restart_backoff[0]
        with contextlib.suppress(OSError):
            self.sessions.update(self.name, last_turn_uuid=self.last_completed_uuid,
                                 context_tokens=self.context_tokens)
        self._set_state("idle")
        if self.want_reload:
            self._reload_task = asyncio.create_task(self._restart_now())
        else:
            self._arm_sleep()
