"""proc.* — long-running processes as durable, cursor-readable sessions.

``shell.exec`` is call/response: it holds the band reply open for the whole
run, so anything slower than the caller's timeout comes back as nothing at all
while the command keeps going invisibly. This plugin decouples the two —
``proc.start`` returns a **handle** immediately and the process outlives any
single call. Output lands in a per-session ring buffer that callers drain by
**cursor** (an absolute byte offset), so many readers can follow the same
session independently, at their own pace, and reconnect after a gap.

That cursor shape is deliberate: it matches the band's request/reply transport
(no streaming, no retransmit) and lets the site fan a session out to agents and
the dashboard as a **console room** without pushing a byte-per-packet firehose
through the hub's broadcast relay.

stdout and stderr are merged, as a terminal would. With ``pty=True`` the child
gets a real tty (POSIX only) — needed for password prompts, REPLs and anything
that checks ``isatty``.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal as _signal
import sys
import time
import uuid

from ..plugin import Plugin, capability

# Per-session output kept in memory. Older bytes fall off the front; a reader
# whose cursor has fallen behind is told exactly how much it missed.
DEFAULT_BUFFER_BYTES = 256 * 1024
MAX_BUFFER_BYTES = 4 * 1024 * 1024
# Concurrent sessions per worker — a runaway agent shouldn't be able to fork
# the box into the ground.
MAX_SESSIONS = 16
# How long a finished session sticks around so the tail can still be drained.
DONE_TTL_SECS = 900.0
# Hard ceiling on a single read. The band fragments at 1003 bytes per packet
# with no retransmit, so a reply's odds of arriving whole fall off a cliff as it
# grows: 32 KB is ~33 fragments that must ALL land. Keep replies small and poll
# more often instead — a lost reply costs one cycle, never data, because the
# cursor only advances on a reply we actually received.
MAX_READ_BYTES = 32 * 1024
DEFAULT_READ_BYTES = 8192

_IS_WIN = sys.platform == "win32"


def _trim_partial_utf8(buf: bytes) -> int:
    """Length of ``buf`` with any incomplete trailing UTF-8 sequence removed."""
    for back in range(1, min(4, len(buf)) + 1):
        b = buf[-back]
        if b < 0x80:                      # ASCII: nothing pending
            return len(buf)
        if b >= 0xC0:                     # start byte: is its sequence complete?
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return len(buf) - back if back < need else len(buf)
    return len(buf)


class _Session:
    """One running (or recently finished) process plus its output ring."""

    def __init__(self, handle: str, label: str, argv_repr: str,
                 buffer_bytes: int) -> None:
        self.handle = handle
        self.label = label
        self.argv_repr = argv_repr
        self.started = time.time()
        self.ended: float | None = None
        self.exit_code: int | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.pty_master: int | None = None
        self._buf = bytearray()
        self._limit = buffer_bytes
        # Absolute offset of _buf[0] in the full output stream. Everything
        # before this has been dropped off the front of the ring.
        self.buf_start = 0
        self.total = 0
        self._pump: asyncio.Task | None = None
        self._waiter: asyncio.Task | None = None

    # -- output ------------------------------------------------------------

    def append(self, data: bytes) -> None:
        self._buf.extend(data)
        self.total += len(data)
        if len(self._buf) > self._limit:
            drop = len(self._buf) - self._limit
            del self._buf[:drop]
            self.buf_start += drop

    def slice(self, cursor: int, max_bytes: int) -> tuple[bytes, int, int]:
        """Bytes from ``cursor`` onward. Returns (data, next_cursor, dropped)
        where ``dropped`` is how many bytes were lost before the data starts
        because the ring had already discarded them."""
        cursor = max(0, int(cursor))
        dropped = 0
        if cursor < self.buf_start:
            dropped = self.buf_start - cursor
            cursor = self.buf_start
        off = cursor - self.buf_start
        if off >= len(self._buf):
            return b"", cursor, dropped
        chunk = bytes(self._buf[off:off + max_bytes])
        return chunk, cursor + len(chunk), dropped

    @property
    def running(self) -> bool:
        return self.exit_code is None and self.ended is None

    def info(self) -> dict:
        return {"handle": self.handle, "label": self.label,
                "cmd": self.argv_repr, "pid": self.pid,
                "running": self.running, "exit_code": self.exit_code,
                "pty": self.pty_master is not None,
                "started": self.started,
                "age_secs": round(time.time() - self.started, 1),
                "bytes": self.total, "buffered": len(self._buf),
                "first_available_cursor": self.buf_start}


class ProcPlugin(Plugin):
    NAMESPACE = "proc"

    def __init__(self) -> None:
        super().__init__()
        self._sessions: dict[str, _Session] = {}

    async def stop(self) -> None:
        for s in list(self._sessions.values()):
            await self._terminate(s, hard=True)
        self._sessions.clear()

    # -- lifecycle ---------------------------------------------------------

    @capability("start")
    async def _start(self, cmd: str | None = None, argv: list | None = None,
                     label: str | None = None, cwd: str | None = None,
                     env: dict | None = None, pty: bool = False,
                     buffer_bytes: int = DEFAULT_BUFFER_BYTES) -> dict:
        """Start a process and return a handle immediately — it keeps running
        after this call returns.

        Pass ``argv`` (a list, exec'd with no shell — no quoting needed) or
        ``cmd`` (a string via ``/bin/sh -c``). ``label`` is a short human name
        for what this session is *for*; it becomes the console room title, so
        write it like a task ("install cuda driver"), not like a command.

        Set ``pty=True`` for anything that needs a real terminal — sudo/ssh
        password prompts, REPLs, programs that check ``isatty`` (POSIX only).

        Read the output with ``proc.read(handle, cursor)``, feed its stdin with
        ``proc.write``, and stop it with ``proc.signal`` / ``proc.close``.
        """
        self._reap()
        if len([s for s in self._sessions.values() if s.running]) >= MAX_SESSIONS:
            return {"ok": False, "error": f"too many live sessions "
                                          f"(max {MAX_SESSIONS}); close some"}
        if argv and cmd:
            return {"ok": False, "error": "pass argv or cmd, not both"}
        if not argv and not cmd:
            return {"ok": False, "error": "argv or cmd required"}
        if pty and _IS_WIN:
            return {"ok": False, "error": "pty=True is not supported on Windows"}

        buffer_bytes = max(4096, min(int(buffer_bytes), MAX_BUFFER_BYTES))
        argv_list = [str(a) for a in argv] if argv else None
        argv_repr = " ".join(shlex.quote(a) for a in argv_list) if argv_list else str(cmd)

        child_env = None
        if env:
            child_env = dict(os.environ)
            child_env.update({str(k): str(v) for k, v in env.items()})

        handle = uuid.uuid4().hex[:12]
        sess = _Session(handle, str(label or argv_repr)[:200], argv_repr, buffer_bytes)

        try:
            if pty:
                await self._spawn_pty(sess, argv_list, cmd, cwd, child_env)
            else:
                await self._spawn_pipe(sess, argv_list, cmd, cwd, child_env)
        except FileNotFoundError as e:
            return {"ok": False, "error": f"command not found: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        self._sessions[handle] = sess
        return {"ok": True, "handle": handle, "pid": sess.pid,
                "label": sess.label, "cmd": argv_repr, "pty": bool(pty),
                "started": sess.started}

    async def _spawn_pipe(self, sess, argv_list, cmd, cwd, child_env) -> None:
        kwargs = dict(stdin=asyncio.subprocess.PIPE,
                      stdout=asyncio.subprocess.PIPE,
                      stderr=asyncio.subprocess.STDOUT,
                      cwd=cwd, env=child_env)
        if not _IS_WIN:
            # Own process group, so signalling kills the whole tree and not
            # just the shell that spawned it.
            kwargs["start_new_session"] = True
        if argv_list:
            proc = await asyncio.create_subprocess_exec(*argv_list, **kwargs)
        else:
            proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
        sess.proc = proc
        sess.pid = proc.pid
        sess._pump = asyncio.create_task(self._pump_pipe(sess))
        sess._waiter = asyncio.create_task(self._wait(sess))

    async def _spawn_pty(self, sess, argv_list, cmd, cwd, child_env) -> None:
        import pty as _pty
        master, slave = _pty.openpty()
        try:
            kwargs = dict(stdin=slave, stdout=slave, stderr=slave,
                          cwd=cwd, env=child_env, start_new_session=True)
            if argv_list:
                proc = await asyncio.create_subprocess_exec(*argv_list, **kwargs)
            else:
                proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
        finally:
            # The child owns the slave end now; holding it open here would
            # keep the master readable forever after the child exits.
            os.close(slave)
        os.set_blocking(master, False)
        sess.proc = proc
        sess.pid = proc.pid
        sess.pty_master = master
        sess._pump = asyncio.create_task(self._pump_pty(sess))
        sess._waiter = asyncio.create_task(self._wait(sess))

    async def _pump_pipe(self, sess: _Session) -> None:
        try:
            while True:
                data = await sess.proc.stdout.read(8192)
                if not data:
                    break
                sess.append(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _pump_pty(self, sess: _Session) -> None:
        loop = asyncio.get_running_loop()
        master = sess.pty_master
        queue: asyncio.Queue = asyncio.Queue()

        def _on_readable() -> None:
            try:
                data = os.read(master, 8192)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                # EIO on Linux is the normal "child closed the tty" signal.
                data = b""
            queue.put_nowait(data)

        loop.add_reader(master, _on_readable)
        try:
            while True:
                data = await queue.get()
                if not data:
                    break
                sess.append(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            try:
                loop.remove_reader(master)
            except Exception:
                pass

    async def _wait(self, sess: _Session) -> None:
        try:
            code = await sess.proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            code = -1
        # Let the pump drain whatever is still buffered before we mark it done,
        # so a reader polling right after exit still sees the tail.
        if sess._pump is not None:
            try:
                await asyncio.wait_for(asyncio.shield(sess._pump), 5.0)
            except Exception:
                pass
        sess.exit_code = code
        sess.ended = time.time()
        if sess.pty_master is not None:
            try:
                os.close(sess.pty_master)
            except Exception:
                pass
            sess.pty_master = None

    # -- io ----------------------------------------------------------------

    @capability("read")
    async def _read(self, handle: str, cursor: int = 0,
                    max_bytes: int = DEFAULT_READ_BYTES) -> dict:
        """Read output from ``cursor`` onward (0 = from the beginning).

        Returns ``{chunk, cursor, next_cursor, dropped, running, exit_code,
        eof}``. Pass ``next_cursor`` back as ``cursor`` next time to page
        forward. ``dropped`` > 0 means the ring discarded that many bytes
        before your cursor caught up — output was lost, not reordered.
        ``eof`` is true once the process has exited AND you've read everything
        it produced.
        """
        sess = self._sessions.get(handle)
        if sess is None:
            return {"ok": False, "error": f"no such handle: {handle}"}
        max_bytes = max(1, min(int(max_bytes), MAX_READ_BYTES))
        chunk, next_cursor, dropped = sess.slice(cursor, max_bytes)
        # A byte slice can land mid-character; hand back only whole ones and
        # let the remainder start the next read, so no chunk boundary ever
        # turns a valid character into replacement junk.
        if chunk and not (not sess.running and next_cursor >= sess.total):
            trimmed = _trim_partial_utf8(chunk)
            if trimmed != len(chunk):
                next_cursor -= (len(chunk) - trimmed)
                chunk = chunk[:trimmed]
        return {"ok": True, "handle": handle, "label": sess.label,
                "chunk": chunk.decode(errors="replace"),
                "cursor": int(cursor), "next_cursor": next_cursor,
                "dropped": dropped, "running": sess.running,
                "exit_code": sess.exit_code, "total": sess.total,
                "eof": (not sess.running) and next_cursor >= sess.total}

    @capability("write")
    async def _write(self, handle: str, data: str, newline: bool = True) -> dict:
        """Write to the process's stdin. ``newline`` appends "\\n" (what you
        want when answering a prompt). Sent verbatim — no shell involved."""
        sess = self._sessions.get(handle)
        if sess is None:
            return {"ok": False, "error": f"no such handle: {handle}"}
        if not sess.running:
            return {"ok": False, "error": "process has exited",
                    "exit_code": sess.exit_code}
        payload = (data + ("\n" if newline else "")).encode()
        try:
            if sess.pty_master is not None:
                os.write(sess.pty_master, payload)
            else:
                sess.proc.stdin.write(payload)
                await sess.proc.stdin.drain()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "handle": handle, "written": len(payload)}

    @capability("signal")
    async def _signal(self, handle: str, sig: str = "TERM") -> dict:
        """Send a signal to the process group: TERM (polite), KILL (hard),
        INT (ctrl-C), HUP. The session and its output stay readable."""
        sess = self._sessions.get(handle)
        if sess is None:
            return {"ok": False, "error": f"no such handle: {handle}"}
        if not sess.running:
            return {"ok": True, "handle": handle, "note": "already exited",
                    "exit_code": sess.exit_code}
        name = str(sig).upper().removeprefix("SIG")
        try:
            signum = getattr(_signal, f"SIG{name}")
        except AttributeError:
            return {"ok": False, "error": f"unknown signal: {sig}"}
        try:
            self._kill(sess, signum)
        except ProcessLookupError:
            pass
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "handle": handle, "sent": f"SIG{name}"}

    def _kill(self, sess: _Session, signum) -> None:
        if _IS_WIN or sess.pid is None:
            sess.proc.send_signal(signum)
            return
        try:
            os.killpg(os.getpgid(sess.pid), signum)
        except (ProcessLookupError, PermissionError, OSError):
            sess.proc.send_signal(signum)

    @capability("close")
    async def _close(self, handle: str) -> dict:
        """Kill the process if it's still running and drop the session. Its
        output is gone from the worker afterwards — read anything you still
        want first (the site keeps its own copy in the console room)."""
        sess = self._sessions.pop(handle, None)
        if sess is None:
            return {"ok": False, "error": f"no such handle: {handle}"}
        await self._terminate(sess, hard=True)
        return {"ok": True, "handle": handle, "exit_code": sess.exit_code,
                "bytes": sess.total}

    @capability("list")
    async def _list(self) -> dict:
        """Every session on this worker — live and recently finished."""
        self._reap()
        return {"ok": True, "sessions": [s.info() for s in
                                         sorted(self._sessions.values(),
                                                key=lambda x: x.started)]}

    # -- housekeeping ------------------------------------------------------

    async def _terminate(self, sess: _Session, hard: bool = False) -> None:
        if sess.running and sess.proc is not None:
            try:
                self._kill(sess, _signal.SIGKILL if hard else _signal.SIGTERM)
            except Exception:
                pass
            try:
                await asyncio.wait_for(sess.proc.wait(), 3.0)
            except Exception:
                pass
        for t in (sess._pump, sess._waiter):
            if t is not None and not t.done():
                t.cancel()
        if sess.pty_master is not None:
            try:
                os.close(sess.pty_master)
            except Exception:
                pass
            sess.pty_master = None

    def _reap(self) -> None:
        """Drop finished sessions whose tail nobody came back for."""
        cut = time.time() - DONE_TTL_SECS
        for h, s in list(self._sessions.items()):
            if not s.running and (s.ended or 0) < cut:
                self._sessions.pop(h, None)

    def heartbeat(self) -> dict | None:
        live = sum(1 for s in self._sessions.values() if s.running)
        return {"live": live} if live else None


PLUGIN = ProcPlugin
