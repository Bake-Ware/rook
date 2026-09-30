"""work.stream.* / work.sessions / work.export — live PTY terminals for Work.

Real terminals, 1:1: a harness (claude, codex, hermes, or a login shell) runs
under a PTY on this worker and its raw output lands in a bounded ring. The hub
follows each terminal with a **long-poll** on ``work.stream.read``: the call
returns as soon as there are bytes past the caller's cursor (or after ``wait``
seconds of silence), so a live terminal costs one outstanding request instead
of a polling loop, and output rides back in compact, compressed frames (see
:mod:`rook.worker.termwire`). The cursor is an absolute byte offset, so a lost
reply costs one round trip and never data, and any number of readers can
follow one terminal independently.

Input is raw bytes (keystrokes, pastes) written verbatim to the PTY; resize
sets the PTY window size, which delivers SIGWINCH to the foreground job. The
child gets the PTY as its controlling terminal, so Ctrl-C and job control work
as they would locally.

``work.sessions`` lists live terminals plus the host's Claude/Codex history as
one resumable catalog; ``work.export`` returns a historical transcript in the
stable ``rook.transcript/1`` format (docs/web/worklog.md) for memory ingestion.

Build-167 workers lack these caps; the hub falls back to ``proc.*`` resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal as _signal
import sys
import time
import uuid
from pathlib import Path

from ..plugin import Plugin, capability, place
from .. import termwire

log = logging.getLogger("rook.worker.plugins.terminals")

HARNESSES = ("shell", "claude", "codex", "hermes")
MAX_LIVE = 8                        # concurrent live terminals per worker
DEFAULT_RING = 256 * 1024           # scrollback bytes kept per terminal
MAX_RING = 1024 * 1024
DONE_TTL_SECS = 900.0               # finished terminals stay readable this long
DEFAULT_READ = 16 * 1024            # raw bytes per read (compressed on the wire)
MAX_READ = 32 * 1024
MAX_WAIT = 25.0                     # long-poll ceiling, seconds
COALESCE_SECS = 0.012               # gather a burst before answering a long-poll
MAX_WRITE = 16 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
# Never hand the worker's own band secret to an agent's environment.
_STRIP_ENV = ("ROOK_BAND_PSK", "ROOK_PSK", "ROOK_MCP_STATIC_TOKEN")


def _check_id(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {what}")
    return value


def _binary(harness: str) -> str | None:
    if harness == "shell":
        return os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh")
    if harness == "claude":
        from .claude_history import _claude_bin
        return _claude_bin()
    return shutil.which(harness)


def available_harnesses() -> list[str]:
    return [h for h in HARNESSES if _binary(h)]


def persona_args(harness: str, persona: str) -> list[str]:
    """Hook for the persona plugin: extra argv that applies ``persona`` to a
    harness. Placeholder until that plugin lands; the persona id is still
    exported as ``ROOK_PERSONA`` so a wrapper script can act on it."""
    return []


def build_argv(harness: str, binary: str, *, model: str = "", resume: str = "",
               mcp_url: str = "", mcp_config: str = "", persona: str = "") -> list[str]:
    """The launch template for one harness. Pure, so it is unit-testable."""
    if harness == "shell":
        return [binary, "-l"] if os.path.basename(binary) in ("bash", "zsh", "fish", "sh") else [binary]
    argv = [binary]
    if harness == "claude":
        if resume:
            argv += ["--resume", resume]
        if model:
            argv += ["--model", model]
        if mcp_config:
            argv += ["--mcp-config", mcp_config]
    elif harness == "codex":
        if resume:
            argv += ["resume", resume]
        if model:
            argv += ["-m", model]
        if mcp_url:
            # The bearer stays in the environment, never on the command line.
            argv += ["-c", f"mcp_servers.rook.url={json.dumps(mcp_url)}",
                     "-c", 'mcp_servers.rook.bearer_token_env_var="ROOK_MCP_TOKEN"']
    elif harness == "hermes":
        if model:
            argv += ["--model", model]
    return argv + persona_args(harness, persona)


class _Term:
    """One PTY-backed process plus its output ring."""

    def __init__(self, tid: str, harness: str, title: str, cwd: str, ring: int) -> None:
        self.id = tid
        self.harness = harness
        self.title = title
        self.cwd = cwd
        self.resume = ""
        self.model = ""
        self.started = time.time()
        self.ended: float | None = None
        self.exit_code: int | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.master: int | None = None
        self.cols = 120
        self.rows = 32
        self.limit = ring
        self.buf = bytearray()
        self.buf_start = 0
        self.total = 0
        self.last_output = self.started
        self.last_input = 0.0
        self.waiters: set[asyncio.Future] = set()
        self.files: list[str] = []      # per-session files to remove at close
        self.waiter_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.ended is None

    def append(self, data: bytes) -> None:
        self.buf.extend(data)
        self.total += len(data)
        self.last_output = time.time()
        if len(self.buf) > self.limit:
            drop = len(self.buf) - self.limit
            del self.buf[:drop]
            self.buf_start += drop
        self.wake()

    def wake(self) -> None:
        for fut in list(self.waiters):
            if not fut.done():
                fut.set_result(None)
        self.waiters.clear()

    async def wait(self, timeout: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self.waiters.add(fut)
        try:
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self.waiters.discard(fut)

    def slice(self, cursor: int, n: int) -> tuple[bytes, int, int]:
        cursor = max(0, int(cursor))
        dropped = 0
        if cursor < self.buf_start:
            dropped, cursor = self.buf_start - cursor, self.buf_start
        off = cursor - self.buf_start
        chunk = bytes(self.buf[off:off + n])
        return chunk, cursor + len(chunk), dropped

    def info(self) -> dict:
        return {"id": self.id, "harness": self.harness, "title": self.title,
                "cwd": self.cwd, "resume": self.resume or None, "model": self.model or None,
                "pid": self.pid, "running": self.running, "exit_code": self.exit_code,
                "started": self.started, "ended": self.ended, "cols": self.cols,
                "rows": self.rows, "total": self.total, "first": self.buf_start,
                "last_output": self.last_output}


class TerminalsPlugin(Plugin):
    NAMESPACE = "work"
    NAME = "terminals"
    PLACEMENT = place("not is_hub and has('pty')")
    SKILL = ("Live terminals: `work.stream.open` starts claude/codex/hermes/shell under a "
             "PTY on a worker; follow it with `work.stream.read(id, cursor, wait=10)` "
             "(returns when output arrives), type with `work.stream.write` (raw bytes, "
             "include \\r for Enter), stop with `work.stream.close`. `work.sessions` lists "
             "live terminals and resumable history; `work.export` returns a transcript "
             "in rook.transcript/1.")

    def __init__(self) -> None:
        super().__init__()
        self.terms: dict[str, _Term] = {}
        self._worker = None
        self._harnesses: list[str] = []
        self._probed = -1e9

    def available(self) -> bool:
        if sys.platform == "win32":
            return False
        try:
            import pty  # noqa: F401
            import termios  # noqa: F401
        except ImportError:
            return False
        return True

    def bind_worker(self, worker) -> None:
        self._worker = worker

    async def stop(self) -> None:
        for t in list(self.terms.values()):
            await self._terminate(t)
            self._cleanup_files(t)
        self.terms.clear()

    def heartbeat(self) -> dict | None:
        # Which harnesses the launch form may offer for this host; re-probed
        # at most every 5 minutes so the announce stays cheap.
        now = time.monotonic()
        if now - self._probed > 300:
            self._harnesses, self._probed = available_harnesses(), now
        out = {"harnesses": self._harnesses}
        live = sum(1 for t in self.terms.values() if t.running)
        if live:
            out["terms"] = live
        return out

    # -- helpers -----------------------------------------------------------

    def _get(self, tid: str) -> _Term:
        t = self.terms.get(_check_id(tid))
        if t is None:
            raise ValueError(f"no such terminal: {tid}")
        return t

    def _reap(self) -> None:
        cut = time.time() - DONE_TTL_SECS
        for tid, t in list(self.terms.items()):
            if not t.running and (t.ended or 0) < cut:
                self.terms.pop(tid, None)

    @staticmethod
    def _winsize(fd: int, rows: int, cols: int) -> None:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _session_dir(self) -> Path:
        base = Path(os.environ.get("ROOK_WORK_TERM_DIR", "~/.rook-band-worker/terminals")).expanduser()
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        return base

    def _cleanup_files(self, t: _Term) -> None:
        for f in t.files:
            try:
                os.unlink(f)
            except OSError:
                pass
        t.files.clear()

    # -- lifecycle ---------------------------------------------------------

    @capability("stream.open", risk="exec")
    async def open(self, harness: str = "shell", cwd: str = "", title: str = "",
                   model: str = "", resume: str = "", persona: str = "",
                   mcp_url: str = "", mcp_token: str = "", session: str = "",
                   cols: int = 120, rows: int = 32,
                   buffer_bytes: int = DEFAULT_RING) -> dict:
        """Start a harness (shell|claude|codex|hermes) under a PTY and return
        its terminal ``id`` immediately. ``resume`` is a Claude/Codex session id
        to continue. ``mcp_url``/``mcp_token`` inject a Rook MCP connection
        (env ROOK_MCP_URL/ROOK_MCP_TOKEN; claude also gets --mcp-config, codex
        -c mcp_servers.rook.*). ``session`` is the hub's Work session id,
        exported as ROOK_WORK_SESSION. Follow output with work.stream.read."""
        self._reap()
        if harness not in HARNESSES:
            raise ValueError(f"harness must be one of {', '.join(HARNESSES)}")
        if sum(1 for t in self.terms.values() if t.running) >= MAX_LIVE:
            raise ValueError(f"too many live terminals (max {MAX_LIVE}); close one first")
        cwd = cwd or os.path.expanduser("~")
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise ValueError(f"working directory does not exist: {cwd}")
        if resume:
            _check_id(resume, "resume session id")
            if harness not in ("claude", "codex"):
                raise ValueError("resume is only supported for claude and codex")
            from ..agent_activity import active_sessions
            try:
                _paths, ids = active_sessions(harness)
            except Exception:
                ids = set()
            if resume.lower() in ids:
                raise ValueError("that session is already active on this host")
            for t in self.terms.values():
                if t.running and t.resume == resume:
                    raise ValueError(f"that session is already running in terminal {t.id}")
        if session:
            _check_id(session, "session")
        binary = _binary(harness)
        if not binary:
            raise ValueError(f"{harness} is not installed on this host")
        cols = max(20, min(int(cols), 500))
        rows = max(5, min(int(rows), 200))
        ring = max(16 * 1024, min(int(buffer_bytes), MAX_RING))

        tid = uuid.uuid4().hex[:12]
        t = _Term(tid, harness, (title or f"{harness} in {os.path.basename(cwd) or cwd}")[:160], cwd, ring)
        t.resume, t.model, t.cols, t.rows = resume, model[:100], cols, rows

        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env.update(TERM="xterm-256color", COLORTERM="truecolor", ROOK_WORK_TERMINAL=tid)
        if session:
            env["ROOK_WORK_SESSION"] = session
        if persona:
            env["ROOK_PERSONA"] = str(persona)[:100]
        mcp_config = ""
        if mcp_url and mcp_token:
            env.update(ROOK_MCP_URL=mcp_url, ROOK_MCP_TOKEN=mcp_token)
            if harness == "claude":
                path = self._session_dir() / f"mcp-{tid}.json"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump({"mcpServers": {"rook": {"type": "http", "url": mcp_url,
                               "headers": {"Authorization": f"Bearer {mcp_token}"}}}}, f)
                mcp_config = str(path)
                t.files.append(mcp_config)
        argv = build_argv(harness, binary, model=model, resume=resume,
                          mcp_url=mcp_url if mcp_token else "", mcp_config=mcp_config,
                          persona=persona)
        try:
            await self._spawn(t, argv, cwd, env)
        except Exception:
            self._cleanup_files(t)
            raise
        self.terms[tid] = t
        log.info("terminal %s started: %s (pid %s)", tid, harness, t.pid)
        return {"ok": True, **t.info()}

    async def _spawn(self, t: _Term, argv: list[str], cwd: str, env: dict) -> None:
        import pty
        import termios
        import fcntl
        master, slave = pty.openpty()
        try:
            self._winsize(slave, t.rows, t.cols)

            def _ctty() -> None:  # runs in the child after setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                start_new_session=True, preexec_fn=_ctty)
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        t.proc, t.pid, t.master = proc, proc.pid, master
        loop = asyncio.get_running_loop()

        def _readable() -> None:
            try:
                data = os.read(master, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                data = b""
            if data:
                t.append(data)
            else:
                try:
                    loop.remove_reader(master)
                except Exception:
                    pass

        loop.add_reader(master, _readable)
        t.waiter_task = asyncio.create_task(self._wait(t))

    async def _wait(self, t: _Term) -> None:
        try:
            code = await t.proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            code = -1
        # Drain what the kernel still holds for the master before closing it.
        if t.master is not None:
            for _ in range(64):
                try:
                    data = os.read(t.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                t.append(data)
        self._close_master(t)
        t.exit_code, t.ended = code, time.time()
        self._cleanup_files(t)
        t.wake()
        log.info("terminal %s exited (%s)", t.id, code)

    def _close_master(self, t: _Term) -> None:
        if t.master is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(t.master)
        except Exception:
            pass
        try:
            os.close(t.master)
        except OSError:
            pass
        t.master = None

    async def _terminate(self, t: _Term) -> None:
        if t.running and t.proc is not None:
            for sig, grace in ((_signal.SIGHUP, 1.5), (_signal.SIGKILL, 3.0)):
                try:
                    os.killpg(os.getpgid(t.pid), sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        t.proc.send_signal(sig)
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(asyncio.shield(t.proc.wait()), grace)
                    break
                except asyncio.TimeoutError:
                    continue
            if t.waiter_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(t.waiter_task), 2.0)
                except (asyncio.TimeoutError, Exception):
                    pass
        self._close_master(t)
        if t.ended is None and t.proc is not None and t.proc.returncode is not None:
            t.exit_code, t.ended = t.proc.returncode, time.time()
        t.wake()

    # -- io ----------------------------------------------------------------

    @capability("stream.read", risk="read")
    async def read(self, id: str, cursor: int = 0, max_bytes: int = DEFAULT_READ,
                   wait: float = 0, accept: str = "tbz") -> dict:
        """Output from byte ``cursor`` on. With ``wait`` > 0 the call holds (up
        to 25 s) until output past ``cursor`` exists or the process exits.
        Returns ``{enc, data, cursor, next, dropped, running, exit_code, eof}``;
        decode with enc t=text, b=base64, z=base64(zlib). ``accept`` limits the
        encodings (default "tbz"). Pass ``next`` back as ``cursor``."""
        t = self._get(id)
        cursor = max(0, int(cursor))
        wait = max(0.0, min(float(wait), MAX_WAIT))
        if wait and t.running and cursor >= t.total:
            await t.wait(wait)
            if t.total > cursor and t.running:
                await asyncio.sleep(COALESCE_SECS)
        n = max(1, min(int(max_bytes), MAX_READ))
        raw, nxt, dropped = t.slice(cursor, n)
        enc, data = termwire.encode(raw, tuple(accept or "b"))
        return {"ok": True, "id": t.id, "enc": enc, "data": data,
                "cursor": nxt - len(raw), "next": nxt, "dropped": dropped,
                "total": t.total, "running": t.running, "exit_code": t.exit_code,
                "eof": (not t.running) and nxt >= t.total,
                "cols": t.cols, "rows": t.rows}

    @capability("stream.write", risk="exec")
    async def write(self, id: str, data: str, enc: str = "t") -> dict:
        """Write raw input to the terminal (keystrokes, pastes; send "\\r" for
        Enter, "\\x03" for Ctrl-C). ``enc`` is t (text) or b (base64)."""
        t = self._get(id)
        if not t.running or t.master is None:
            raise ValueError("terminal has exited")
        payload = termwire.decode(enc, data, limit=MAX_WRITE)
        if len(payload) > MAX_WRITE:
            raise ValueError(f"input larger than {MAX_WRITE} bytes; split it")
        view = memoryview(payload)
        deadline = time.monotonic() + 5
        while view:
            try:
                n = os.write(t.master, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ValueError("terminal input is not being read")
                await asyncio.sleep(0.01)
        t.last_input = time.time()
        return {"ok": True, "id": t.id, "written": len(payload)}

    @capability("stream.resize", risk="write")
    def resize(self, id: str, cols: int, rows: int) -> dict:
        """Set the terminal size in character cells (sends SIGWINCH)."""
        t = self._get(id)
        cols = max(20, min(int(cols), 500))
        rows = max(5, min(int(rows), 200))
        if t.master is not None and (cols, rows) != (t.cols, t.rows):
            self._winsize(t.master, rows, cols)
        t.cols, t.rows = cols, rows
        return {"ok": True, "id": t.id, "cols": cols, "rows": rows}

    @capability("stream.signal", risk="exec")
    def signal(self, id: str, sig: str = "INT") -> dict:
        """Signal the terminal's process group: INT, TERM, HUP, KILL."""
        t = self._get(id)
        name = str(sig).upper().removeprefix("SIG")
        if name not in ("INT", "TERM", "HUP", "KILL", "QUIT"):
            raise ValueError("signal must be INT, TERM, HUP, QUIT or KILL")
        if t.running and t.pid:
            try:
                os.killpg(os.getpgid(t.pid), getattr(_signal, "SIG" + name))
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return {"ok": True, "id": t.id, "sent": "SIG" + name}

    @capability("stream.close", risk="exec")
    async def close(self, id: str) -> dict:
        """Stop the terminal's process (SIGHUP, then SIGKILL) and drop it."""
        t = self._get(id)
        await self._terminate(t)
        self._cleanup_files(t)
        self.terms.pop(t.id, None)
        return {"ok": True, "id": t.id, "exit_code": t.exit_code, "bytes": t.total}

    @capability("stream.list", risk="read")
    def list_terms(self) -> dict:
        """Live and recently finished terminals on this worker."""
        self._reap()
        return {"ok": True, "harnesses": available_harnesses(),
                "terminals": [t.info() for t in sorted(self.terms.values(), key=lambda x: x.started)]}

    # -- catalog -----------------------------------------------------------

    @capability("sessions", risk="read", limit=20)
    async def sessions(self, limit: int = 20, offset: int = 0, history: bool = True,
                       query: str = "") -> dict:
        """One catalog of this host's work: live terminals plus Claude/Codex
        history, newest first. Every history entry is resumable with
        work.stream.open(harness=agent, resume=session_id, cwd=cwd) unless
        ``active``. Page with ``limit``/``offset``; ``query`` filters titles/cwd."""
        self._reap()
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        live = [t.info() for t in sorted(self.terms.values(), key=lambda x: -x.started)]
        items: list[dict] = []
        total = 0
        if history and self._worker is not None:
            reg = self._worker.registry
            want = offset + limit
            for agent in ("claude", "codex"):
                cap = f"{agent}-history.pull"
                if not reg.has(cap):
                    continue
                try:
                    res = await reg.call(cap, limit=want if not query else 500)
                except Exception:
                    log.debug("history pull failed for %s", agent, exc_info=True)
                    continue
                if not isinstance(res, dict) or not res.get("ok"):
                    continue
                total += int(res.get("total") or 0)
                for s in res.get("sessions", []):
                    items.append({"agent": agent, "session_id": s.get("session_id"),
                                  "title": s.get("title"), "cwd": s.get("cwd"),
                                  "updated": s.get("last_modified"),
                                  "messages": s.get("message_count"),
                                  "active": bool(s.get("active")),
                                  "activity": s.get("activity"),
                                  "resumable": not s.get("active")})
        if query:
            q = query.lower()
            items = [i for i in items if q in f"{i.get('title')} {i.get('cwd')}".lower()]
            total = len(items)
        items.sort(key=lambda i: -(i.get("updated") or 0))
        return {"ok": True, "harnesses": available_harnesses(), "live": live,
                "items": items[offset:offset + limit], "total": total,
                "next_offset": offset + limit if offset + limit < total else None}

    @capability("export", risk="read")
    async def export(self, agent: str, session_id: str, offset: int = 0,
                     max_chars: int = 6000) -> dict:
        """A page of a historical Claude/Codex transcript in the stable
        ``rook.transcript/1`` format: ``{format, session, messages:[{index,
        role, ts, text}], next_offset, total}``. Page until next_offset is
        null. Intended for memory ingestion; nothing leaves the host unasked."""
        if agent not in ("claude", "codex"):
            raise ValueError("agent must be claude or codex")
        cap = f"{agent}-history.transcript"
        if self._worker is None or not self._worker.registry.has(cap):
            raise ValueError(f"{agent} history is not available on this host")
        return await self._worker.registry.call(cap, session_id=session_id,
                                                offset=offset, max_chars=max_chars)


PLUGIN = TerminalsPlugin
