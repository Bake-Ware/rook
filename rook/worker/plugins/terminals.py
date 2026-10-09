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

On Windows (10 1809+) the same interface runs on a ConPTY pseudoconsole
(:mod:`rook.worker.conpty`, pure ctypes): the child sits in a kill-on-close
Job Object, Ctrl-C is the 0x03 byte, hang-up closes the pseudoconsole and
kill terminates the job. The shell harness is PowerShell there.

``work.sessions`` lists live terminals plus the host's Claude/Codex history as
one resumable catalog (the richer ``sessions.list`` in sessions.py supersedes
it); ``work.export`` returns a historical transcript in the
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
import threading
import time
import uuid
from pathlib import Path

from ..plugin import Plugin, capability, place
from .. import conpty, termwire, winsec

log = logging.getLogger("rook.worker.plugins.terminals")

HARNESSES = ("shell", "claude", "codex", "hermes")
MAX_LIVE = 8                        # concurrent live terminals per worker
MAX_LOCAL = 32                      # shim terminals (sessions started in someone's own terminal)
LOCAL_AGENTS = ("claude", "codex")
DEFAULT_RING = 256 * 1024           # scrollback bytes kept per terminal
MAX_RING = 1024 * 1024
DONE_TTL_SECS = 900.0               # finished terminals stay readable this long
DEFAULT_READ = 16 * 1024            # raw bytes per read (compressed on the wire)
MAX_READ = 32 * 1024
MAX_WAIT = 25.0                     # long-poll ceiling, seconds
COALESCE_SECS = 0.012               # gather a burst before answering a long-poll
MAX_WRITE = 16 * 1024
HANDOFF_SECS = 120.0                # how long a handoff waits for the old process to exit
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
# A Rook task id or slug (the hub checks that it exists; the worker only records it).
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}")
# Never hand the worker's own band secret to an agent's environment.
_STRIP_ENV = ("ROOK_BAND_PSK", "ROOK_PSK", "ROOK_MCP_STATIC_TOKEN")
_IS_WIN = sys.platform == "win32"
# cmd.exe would interpret these inside arguments to a .cmd/.bat launcher.
_CMD_META = re.compile(r'[\r\n"%^&|<>!()]')
# The target of an npm cmd-shim: "%dp0%\node_modules\pkg\bin\cli.js" %*
_NPM_SHIM = re.compile(r'"%~?dp0%?\\([^"%*\r\n]+?\.(?:js|cjs|mjs|exe))"', re.I)


def _pid_alive(pid: int) -> bool | None:
    from ..session_mirror import pid_alive
    return pid_alive(pid)


def _check_id(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {what}")
    return value


def _binary(harness: str) -> str | None:
    if _IS_WIN:
        if harness == "shell":
            return (shutil.which("powershell.exe") or shutil.which("pwsh.exe")
                    or os.environ.get("COMSPEC") or shutil.which("cmd.exe"))
        if harness == "claude":
            from .claude_history import _claude_bin
            return _claude_bin()
        # PATHEXT order: a native .exe before an npm .cmd shim.
        return shutil.which(harness)
    if harness == "shell":
        return os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh")
    if harness == "claude":
        from .claude_history import _claude_bin
        return _claude_bin()
    from ..shim import which_real     # never the session shim itself
    return which_real(harness)


def available_harnesses() -> list[str]:
    return [h for h in HARNESSES if _binary(h)]


#: Longest persona text passed on a command line (argv is visible in ps).
MAX_PERSONA_ARG = 8000


def persona_args(harness: str, persona: str) -> list[str]:
    """Extra argv that applies the rendered persona text to a harness (the
    persona plugin; docs/design/persona.md): Claude Code appends it to its
    system prompt, Codex takes it as developer instructions. Hermes has no
    such flag: its persona lives in SOUL.md (``persona.apply``); the text is
    still exported as ROOK_PERSONA_FILE for wrapper scripts."""
    text = (persona or "").strip()
    if not text or len(text) > MAX_PERSONA_ARG:
        return []
    if harness == "claude":
        return ["--append-system-prompt", text]
    if harness == "codex":
        return ["-c", f"developer_instructions={json.dumps(text)}"]
    return []


def build_argv(harness: str, binary: str, *, model: str = "", resume: str = "",
               mcp_url: str = "", mcp_config: str = "", persona: str = "",
               remote_control: str = "") -> list[str]:
    """The launch template for one harness. Pure, so it is unit-testable."""
    if harness == "shell":
        name = os.path.basename(binary.replace("\\", "/")).lower().removesuffix(".exe")
        if name in ("powershell", "pwsh"):
            return [binary, "-NoLogo"]
        return [binary, "-l"] if name in ("bash", "zsh", "fish", "sh") else [binary]
    argv = [binary]
    if harness == "claude":
        if resume:
            argv += ["--resume", resume]
        if model:
            argv += ["--model", model]
        if mcp_config:
            argv += ["--mcp-config", mcp_config]
        if remote_control:
            argv += ["--remote-control", remote_control[:80]]
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


def _npm_shim_target(path: str) -> list[str] | None:
    """[node, script] (or [exe]) behind an npm cmd-shim, so the harness runs
    without cmd.exe re-parsing its arguments. None if it isn't one."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = _NPM_SHIM.search(f.read(16384))
    except OSError:
        return None
    if not m:
        return None
    base = os.path.dirname(path)
    target = os.path.normpath(os.path.join(base, *re.split(r"[\\/]", m.group(1))))
    if not os.path.isfile(target):
        return None
    if target.lower().endswith(".exe"):
        return [target]
    node = os.path.join(base, "node.exe")
    if not os.path.isfile(node):
        node = shutil.which("node")
    return [node, target] if node else None


def windows_command(argv: list[str]) -> str:
    """The CreateProcess command line for a harness argv on Windows. npm
    installs CLIs as .cmd shims, which only cmd.exe can run and which would
    let cmd.exe reinterpret our arguments (persona text, model names): run
    the shim's real target instead, and fall back to cmd.exe only when no
    argument contains a character cmd.exe treats specially."""
    argv = [str(a) for a in argv]
    if argv[0].lower().endswith((".cmd", ".bat")):
        target = _npm_shim_target(argv[0])
        if target:
            argv = target + argv[1:]
        else:
            if any(_CMD_META.search(a) for a in argv):
                raise ValueError(f"{os.path.basename(argv[0])} is a batch launcher; these launch "
                                 "options cannot be passed to it safely")
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            return f'"{comspec}" /d /s /c "{conpty.cmdline(argv)}"'
    return conpty.cmdline(argv)


class _Term:
    """One PTY-backed process plus its output ring."""

    def __init__(self, tid: str, harness: str, title: str, cwd: str, ring: int) -> None:
        self.id = tid
        self.harness = harness
        self.title = title
        self.cwd = cwd
        self.resume = ""
        self.model = ""
        self.session = ""               # the hub's Work session, if any
        self.task = ""                  # the Rook task it was started for, if any
        self.room = ""                  # the console room it feeds, if any
        self.cmd = ""                   # the command it runs, when not a harness
        self.started = time.time()
        self.ended: float | None = None
        self.exit_code: int | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.master: int | None = None
        self.conpty: conpty.ConPty | None = None      # Windows
        self.wlock: asyncio.Lock | None = None
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
        self.handoff_task: asyncio.Task | None = None   # waiting for the old process (handoff_pid)
        # A local terminal: the session shim (rook.worker.shim) runs the program
        # under its own PTY in someone's terminal and streams it here; input,
        # signals and close go back over this link, and that terminal owns the size.
        self.link = None

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
                "session": self.session or None, "task": self.task or None,
                "room": self.room or None, "cmd": self.cmd or None,
                "pid": self.pid, "running": self.running, "exit_code": self.exit_code,
                "started": self.started, "ended": self.ended, "cols": self.cols,
                "rows": self.rows, "total": self.total, "first": self.buf_start,
                "last_output": self.last_output, **({"local": True} if self.link else {})}


class TerminalsPlugin(Plugin):
    NAMESPACE = "work"
    NAME = "terminals"
    PLACEMENT = place("not is_hub and has('pty')")
    SKILL = ("Live terminals: `work.stream.open` starts claude/codex/hermes/shell (or a "
             "command: `argv`/`cmd`) under a PTY on a worker, shown on the Sessions page; "
             "`task=<id>` claims that task for you and links the terminal; follow it with `work.stream.read(id, cursor, wait=10)` "
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
        if _IS_WIN:
            return conpty.available()
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
            if t.link is not None:
                # Not ours to end: the shim keeps the program running and
                # registers again with the next worker.
                t.link.close()
                continue
            await self._terminate(t)
            self._cleanup_files(t)
        self.terms.clear()

    def heartbeat(self) -> dict | None:
        # Which harnesses the launch form may offer for this host; re-probed
        # at most every 5 minutes so the announce stays cheap.
        now = time.monotonic()
        if now - self._probed > 300:
            self._harnesses, self._probed = available_harnesses(), now
        # commands: work.stream.open takes argv/cmd/env/task/room, so the hub
        # runs console rooms here rather than on proc.* (docs/design/sessions.md).
        out = {"harnesses": self._harnesses, "commands": 1}
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

    @staticmethod
    def _command(harness: str, argv, cmd: str, resume: str) -> list[str] | None:
        """The argv for a command terminal (``argv``/``cmd``), or None for a
        harness launch."""
        if not argv and not cmd:
            return None
        if argv and cmd:
            raise ValueError("pass argv or cmd, not both")
        if harness != "shell" or resume:
            raise ValueError("argv/cmd run a command in a shell terminal: harness shell, no resume")
        if argv:
            if not isinstance(argv, list) or not all(isinstance(a, (str, int, float)) for a in argv):
                raise ValueError("argv must be a list of strings")
            return [str(a) for a in argv]
        if not isinstance(cmd, str):
            raise ValueError("cmd must be a string")
        if _IS_WIN:
            return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", cmd]
        return ["/bin/sh", "-c", cmd]

    def _session_dir(self) -> Path:
        base = Path(os.environ.get("ROOK_WORK_TERM_DIR", "~/.rook-band-worker/terminals")).expanduser()
        winsec.make_private_dir(base)
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
                   remote_control: str = "", cols: int = 120, rows: int = 32,
                   buffer_bytes: int = DEFAULT_RING, handoff_pid: int = 0,
                   argv: list | None = None, cmd: str = "", env: dict | None = None,
                   task: str = "", room: str = "", force: bool = False) -> dict:
        """Start a harness (shell|claude|codex|hermes) under a PTY and return
        its terminal ``id`` immediately. With ``argv`` (a list, no shell) or
        ``cmd`` (a string through /bin/sh -c, cmd.exe /c on Windows) it runs
        that command instead of the login shell (harness shell only); ``env``
        adds variables, and PAGER/GIT_PAGER default to cat so nothing waits
        on a pager. ``task`` (a Rook task id or slug) and ``room`` (a console
        room id) are recorded on the terminal and its session record; the
        hub claims the task. ``resume`` is a Claude/Codex session id
        to continue. ``mcp_url``/``mcp_token`` inject a Rook MCP connection
        (env ROOK_MCP_URL/ROOK_MCP_TOKEN; claude also gets --mcp-config, codex
        -c mcp_servers.rook.*). ``session`` is the hub's Work session id,
        exported as ROOK_WORK_SESSION. ``remote_control`` (claude only) is a
        Remote Control label, so the session also shows in claude.ai.
        ``handoff_pid`` (with ``resume``) is the
        process that holds the session now, such as a Claude Code moving itself
        here with /rook-move: the terminal is returned at once and the harness
        starts once that process has exited (up to 2 minutes). Without
        ``handoff_pid``, a resume is refused while the session's transcript
        changed in the last 2 minutes or a Claude Code with an unreadable PID
        marker probably holds it; ``force=true`` overrides that guess (never
        the process evidence). Follow output with work.stream.read."""
        force = force in (True, 1, "1", "true", "yes")
        self._reap()
        if harness not in HARNESSES:
            raise ValueError(f"harness must be one of {', '.join(HARNESSES)}")
        if sum(1 for t in self.terms.values() if t.running and not t.link) >= MAX_LIVE:
            raise ValueError(f"too many live terminals (max {MAX_LIVE}); close one first")
        cwd = cwd or os.path.expanduser("~")
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise ValueError(f"working directory does not exist: {cwd}")
        handoff_pid = int(handoff_pid or 0)
        if handoff_pid and not resume:
            raise ValueError("handoff_pid needs resume: it hands over a running session")
        if handoff_pid and _pid_alive(handoff_pid) is not True:
            handoff_pid = 0     # already gone: resume now, with the usual checks
        if resume:
            _check_id(resume, "resume session id")
            if harness not in ("claude", "codex"):
                raise ValueError("resume is only supported for claude and codex")
        if resume and not handoff_pid:
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
            if not force:
                # No process is known to hold it, but one may (sessions.md §3.1).
                from .sessions import resume_guard
                why = await asyncio.to_thread(resume_guard, harness, resume)
                if why:
                    raise ValueError(f"that session may still be running on this host: {why}. "
                                     "Resume it once it has been quiet for 2 minutes, or pass "
                                     "force=true if you are sure nothing holds it")
        if session:
            _check_id(session, "session")
        task, room = str(task or ""), str(room or "")
        if task and not _TASK.fullmatch(task):
            raise ValueError("invalid task (a Rook task id or slug)")
        if room:
            _check_id(room, "room")
        command = self._command(harness, argv, cmd, resume)
        if command is not None:
            binary = command[0]
        else:
            binary = _binary(harness)
        if not binary:
            raise ValueError(f"{harness} is not installed on this host")
        cols = max(20, min(int(cols), 500))
        rows = max(5, min(int(rows), 200))
        ring = max(16 * 1024, min(int(buffer_bytes), MAX_RING))

        tid = uuid.uuid4().hex[:12]
        shown = (str(cmd) if cmd else " ".join(command)) if command is not None else ""
        t = _Term(tid, harness, (title or shown or f"{harness} in {os.path.basename(cwd) or cwd}")[:160], cwd, ring)
        t.resume, t.model, t.cols, t.rows = resume, model[:100], cols, rows
        t.session, t.task, t.room, t.cmd = session, task, room, shown[:2000]

        extra = {str(k): str(v) for k, v in (env or {}).items()}
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env.update(TERM="xterm-256color", COLORTERM="truecolor", ROOK_WORK_TERMINAL=tid)
        if session:
            env["ROOK_WORK_SESSION"] = session
        if task:
            env["ROOK_TASK"] = task
        if command is not None:
            # Nobody may be at this terminal (a console room an agent reads):
            # a pager waiting for a key would hang it.
            env.update(PAGER="cat", GIT_PAGER="cat")
            env.update(extra)
            self.terms[tid] = t
            try:
                await self._spawn(t, command, cwd, env)
            except Exception:
                self.terms.pop(tid, None)
                raise
            log.info("terminal %s started: %s (pid %s)", tid, shown[:80], t.pid)
            return {"ok": True, **t.info()}
        env.update(extra)
        persona_text = await self._persona_text(harness, persona) if harness != "shell" else ""
        if persona:
            env["ROOK_PERSONA"] = str(persona)[:100]
        if persona_text:
            ppath = self._session_dir() / f"persona-{tid}.md"
            winsec.write_private_file(ppath, persona_text)
            env["ROOK_PERSONA_FILE"] = str(ppath)
            t.files.append(str(ppath))
        mcp_config = ""
        if mcp_url and mcp_token:
            env.update(ROOK_MCP_URL=mcp_url, ROOK_MCP_TOKEN=mcp_token)
            if harness == "claude":
                path = self._session_dir() / f"mcp-{tid}.json"
                winsec.write_private_file(path, json.dumps(
                    {"mcpServers": {"rook": {"type": "http", "url": mcp_url,
                     "headers": {"Authorization": f"Bearer {mcp_token}"}}}}))
                mcp_config = str(path)
                t.files.append(mcp_config)
        argv = build_argv(harness, binary, model=model, resume=resume,
                          mcp_url=mcp_url if mcp_token else "", mcp_config=mcp_config,
                          persona=persona_text,
                          remote_control=str(remote_control or "") if harness == "claude" else "")
        if handoff_pid:
            self.terms[tid] = t
            t.append(b"[rook] waiting for the session's current process to exit...\r\n")
            t.handoff_task = asyncio.create_task(self._handoff(t, handoff_pid, argv, cwd, env))
            log.info("terminal %s waiting for pid %s to hand over %s", tid, handoff_pid, resume)
            return {"ok": True, **t.info(), "waiting": True}
        try:
            await self._spawn(t, argv, cwd, env)
        except Exception:
            self._cleanup_files(t)
            raise
        self.terms[tid] = t
        log.info("terminal %s started: %s (pid %s)", tid, harness, t.pid)
        return {"ok": True, **t.info()}

    async def _handoff(self, t: _Term, pid: int, argv: list[str], cwd: str, env: dict) -> None:
        """Starts the harness once ``pid`` has exited; gives up after HANDOFF_SECS."""
        loop = asyncio.get_running_loop()
        end = loop.time() + HANDOFF_SECS
        why = ""
        try:
            while _pid_alive(pid) is True:
                if loop.time() >= end:
                    why = "the session's current process did not exit within 2 minutes; nothing was resumed"
                    break
                await asyncio.sleep(0.25)
            if not why:
                from ..agent_activity import active_sessions
                try:
                    _paths, ids = active_sessions(t.harness)
                except Exception:
                    ids = set()
                if t.resume.lower() in ids:
                    why = "another process still holds that session; nothing was resumed"
            if not why:
                await self._spawn(t, argv, cwd, env)
                log.info("terminal %s took over session %s (pid %s)", t.id, t.resume, t.pid)
                return
        except asyncio.CancelledError:
            why = "closed before the handover"
        except Exception as e:
            why = f"could not start {t.harness}: {e}"
        t.append(f"[rook] {why}\r\n".encode())
        t.exit_code, t.ended = None, time.time()
        self._cleanup_files(t)
        t.wake()

    async def _persona_text(self, harness: str, persona: str) -> str:
        """The rendered persona for this launch from the hub
        (``persona.render``): the named profile, else the one assigned to the
        harness family. Empty when there is none or the hub doesn't answer."""
        from .persona import fetch_persona
        got = await fetch_persona(self._worker, harness, persona, timeout=3.0)
        return str((got or {}).get("text") or "")

    async def _spawn(self, t: _Term, argv: list[str], cwd: str, env: dict) -> None:
        if _IS_WIN:
            return await self._spawn_conpty(t, argv, cwd, env)
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

    async def _spawn_conpty(self, t: _Term, argv: list[str], cwd: str, env: dict) -> None:
        command = windows_command(argv)
        pty = await asyncio.to_thread(conpty.ConPty.spawn, command, cwd, env, t.cols, t.rows)
        t.conpty, t.pid, t.wlock = pty, pty.pid, asyncio.Lock()
        loop = asyncio.get_running_loop()
        eof = asyncio.Event()
        exited: asyncio.Future = loop.create_future()

        def _post_exit(code) -> None:
            if not exited.done():
                exited.set_result(code)

        def _exit_watch() -> None:  # one blocking wait per terminal, off the loop
            try:
                code = pty.wait()
            except Exception:
                code = None
            loop.call_soon_threadsafe(_post_exit, -1 if code is None else code)

        pty.start_reader(lambda data: loop.call_soon_threadsafe(t.append, data),
                         lambda: loop.call_soon_threadsafe(eof.set))
        threading.Thread(target=_exit_watch, name=f"conpty-wait-{t.id}", daemon=True).start()
        t.waiter_task = asyncio.create_task(self._wait_conpty(t, exited, eof))

    async def _wait_conpty(self, t: _Term, exited: asyncio.Future, eof: asyncio.Event) -> None:
        code = await exited
        # Hang up and kill what is left in the job; the reader then drains the
        # remaining output to EOF (its chunks are queued before the EOF mark).
        await asyncio.to_thread(t.conpty.close)
        try:
            await asyncio.wait_for(eof.wait(), 3.0)
        except asyncio.TimeoutError:
            pass
        t.exit_code, t.ended = code, time.time()
        self._cleanup_files(t)
        t.wake()
        log.info("terminal %s exited (%s)", t.id, code)

    async def _terminate_conpty(self, t: _Term) -> None:
        # Hang-up first (CTRL_CLOSE_EVENT lets programs save), then the job.
        for step, grace in (("hangup", 1.5), ("kill", 3.0)):
            if not t.running:
                break
            try:
                await asyncio.to_thread(getattr(t.conpty, step))
            except OSError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(t.waiter_task), grace)
                break
            except asyncio.TimeoutError:
                continue
        t.wake()

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
        if t.link is not None:
            return await self._terminate_local(t)
        if t.handoff_task is not None and not t.handoff_task.done():
            t.handoff_task.cancel()
            try:
                await t.handoff_task
            except (asyncio.CancelledError, Exception):
                pass
            if t.proc is None and t.conpty is None and t.ended is None:  # cancelled before it ran
                t.exit_code, t.ended = None, time.time()
        if t.conpty is not None:
            return await self._terminate_conpty(t)
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

    # -- local terminals (the session shim) ----------------------------------

    def attach_local(self, agent: str, argv, cwd: str, cols: int, rows: int, link,
                     pid: int = 0) -> _Term:
        """Register a terminal the session shim runs in someone's own terminal
        (rook.worker.shim): ``agent`` (claude or codex) with ``argv`` in
        ``cwd``. Its output arrives through :meth:`_Term.append`; input,
        signals and close go back through ``link``. Raises ValueError to
        refuse (the shim then runs the program directly)."""
        self._reap()
        if agent not in LOCAL_AGENTS:
            raise ValueError(f"agent must be one of {', '.join(LOCAL_AGENTS)}")
        if sum(1 for t in self.terms.values() if t.running and t.link) >= MAX_LOCAL:
            raise ValueError(f"too many local terminals (max {MAX_LOCAL})")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a list of strings")
        if not cwd or not os.path.isabs(cwd):
            raise ValueError("cwd must be an absolute path")
        tid = uuid.uuid4().hex[:12]
        name = os.path.basename(cwd.rstrip("/")) or cwd
        t = _Term(tid, agent, f"{agent} in {name}"[:160], cwd, DEFAULT_RING)
        t.link = link
        t.cols = max(20, min(int(cols or 120), 500))
        t.rows = max(5, min(int(rows or 32), 200))
        t.pid = int(pid or 0) or None
        for i, arg in enumerate(argv[:-1]):
            if (agent == "claude" and arg in ("--resume", "-r")) or \
                    (agent == "codex" and i == 0 and arg == "resume"):
                if _ID.fullmatch(argv[i + 1]):
                    t.resume = argv[i + 1]
                break
        self.terms[tid] = t
        log.info("local terminal %s registered: %s in %s", tid, agent, cwd)
        return t

    def local_size(self, t: _Term, cols: int, rows: int) -> None:
        if cols > 0 and rows > 0:
            t.cols, t.rows = max(20, min(cols, 500)), max(5, min(rows, 200))
            t.wake()            # long-polls return, so viewers learn the size

    def local_exit(self, t: _Term, code: int | None) -> None:
        if t.running:
            t.exit_code, t.ended = code, time.time()
            t.wake()
            log.info("local terminal %s exited (%s)", t.id, code)

    def local_lost(self, t: _Term) -> None:
        """The shim went away without an exit: it may still run (it registers
        again, with a new id) or it was killed."""
        if t.running:
            t.append(b"\r\n[rook] The local terminal disconnected.\r\n")
            t.exit_code, t.ended = None, time.time()
            t.wake()

    async def _terminate_local(self, t: _Term) -> None:
        for op, grace in (("hangup", 1.5), ("kill", 3.0)):
            if not t.running:
                break
            t.link.control({"op": op})
            end = time.monotonic() + grace
            while t.running and time.monotonic() < end:
                await t.wait(0.1)
        if t.running:
            self.local_lost(t)
        t.link.close()

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
        out = {"ok": True, "id": t.id, "enc": enc, "data": data,
               "cursor": nxt - len(raw), "next": nxt, "dropped": dropped,
               "total": t.total, "running": t.running, "exit_code": t.exit_code,
               "eof": (not t.running) and nxt >= t.total,
               "cols": t.cols, "rows": t.rows}
        if t.link is not None:
            out["fixed"] = True     # the local terminal owns the size
        return out

    @capability("stream.write", risk="exec")
    async def write(self, id: str, data: str, enc: str = "t") -> dict:
        """Write raw input to the terminal (keystrokes, pastes; send "\\r" for
        Enter, "\\x03" for Ctrl-C). ``enc`` is t (text) or b (base64)."""
        t = self._get(id)
        if not t.running or (t.master is None and t.conpty is None and t.link is None):
            raise ValueError("terminal has exited")
        payload = termwire.decode(enc, data, limit=MAX_WRITE)
        if len(payload) > MAX_WRITE:
            raise ValueError(f"input larger than {MAX_WRITE} bytes; split it")
        if t.link is not None:
            await t.link.input(payload)
            t.last_input = time.time()
            return {"ok": True, "id": t.id, "written": len(payload)}
        if t.conpty is not None:
            async with t.wlock:  # keep pastes whole and in order
                try:
                    await asyncio.wait_for(asyncio.to_thread(t.conpty.write, payload), 5)
                except asyncio.TimeoutError:
                    raise ValueError("terminal input is not being read") from None
                except OSError:
                    raise ValueError("terminal has exited") from None
            t.last_input = time.time()
            return {"ok": True, "id": t.id, "written": len(payload)}
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
        """Set the terminal size in character cells (sends SIGWINCH). A local
        terminal (the session shim) keeps the size of the terminal it runs
        in: the reply has that size and ``fixed: true``."""
        t = self._get(id)
        if t.link is not None:
            return {"ok": True, "id": t.id, "cols": t.cols, "rows": t.rows, "fixed": True}
        cols = max(20, min(int(cols), 500))
        rows = max(5, min(int(rows), 200))
        if (cols, rows) != (t.cols, t.rows):
            if t.conpty is not None and t.running:
                t.conpty.resize(cols, rows)
            elif t.master is not None:
                self._winsize(t.master, rows, cols)
        t.cols, t.rows = cols, rows
        return {"ok": True, "id": t.id, "cols": cols, "rows": rows}

    @capability("stream.signal", risk="exec")
    def signal(self, id: str, sig: str = "INT") -> dict:
        """Signal the terminal's process group: INT, TERM, HUP, KILL. On
        Windows INT is Ctrl-C (0x03 to the console), HUP closes the console
        and TERM/QUIT/KILL terminate the terminal's job."""
        t = self._get(id)
        name = str(sig).upper().removeprefix("SIG")
        if name not in ("INT", "TERM", "HUP", "KILL", "QUIT"):
            raise ValueError("signal must be INT, TERM, HUP, QUIT or KILL")
        if t.link is not None:
            if t.running:
                t.link.control({"op": "signal", "sig": name})
            return {"ok": True, "id": t.id, "sent": "SIG" + name}
        if t.conpty is not None:
            if t.running:
                try:
                    if name == "INT":
                        t.conpty.interrupt()
                    elif name == "HUP":
                        # May wait for output to drain on older Windows: never on the loop.
                        threading.Thread(target=t.conpty.hangup, daemon=True).start()
                    else:
                        t.conpty.kill()
                except OSError:
                    pass
            return {"ok": True, "id": t.id, "sent": "SIG" + name}
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
        ``active``. Page with ``limit``/``offset``; ``query`` filters titles/cwd.
        Superseded by sessions.list (docs/design/sessions.md); this shape stays
        for the worklog page and older hubs."""
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
