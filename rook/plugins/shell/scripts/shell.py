"""shell: run commands, look at the environment, and open live terminals.

Every process starts through the worker's launch path
(rook.core2.worker.security.launch, spec 7.1): as the worker's normal user,
or elevated when the call holds a lease. Nothing here may start a process
any other way.

``shell.open`` is the producer behind terminal and process streams (proc
merged into it, castling-streams): it starts the process on a PTY (or on
pipes with ``pty=false``), appends its output as ``bytes`` events and feeds
it ``input`` events (``text``, ``signal``, ``resize``) until it exits.

The ``cmd`` shell model is 0.4's (rook/worker/shells.py): ``/bin/sh -c`` on
Linux and macOS, PowerShell on Windows, or the ``shell`` named.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys

from rook.core2.worker.security import launch, looks_denied

IS_WIN = sys.platform == "win32"
SHELLS = ("sh", "bash", "powershell", "cmd")
_ALIASES = {"pwsh": "powershell", "powershell.exe": "powershell", "pwsh.exe": "powershell",
            "cmd.exe": "cmd", "/bin/sh": "sh", "sh.exe": "sh", "bash.exe": "bash"}
CREATE_NO_WINDOW = 0x08000000      # the Windows worker has no console: no window per child
MAX_CMDLINE = 32767
CHUNK = 64 * 1024                  # one bytes event at most (well under the stream's 1 MiB)
OUT_MAX = 8 << 20                  # bytes of stdout (and of stderr) shell.exec keeps; the rest is read and dropped
# Names whose values shell.env never shows: the worker's own keys and anything credential-like.
_SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSW|PSK|API_?KEY|PRIVATE|CREDENTIAL|_KEY$|^ROOK_)", re.I)
_SIGNALS = {"INT": signal.SIGINT, "TERM": signal.SIGTERM, "KILL": getattr(signal, "SIGKILL", signal.SIGTERM),
            "HUP": getattr(signal, "SIGHUP", signal.SIGTERM), "QUIT": getattr(signal, "SIGQUIT", signal.SIGTERM)}
_RUNNING: set[asyncio.Task] = set()   # shell.open pumps, kept alive past the call

_PS_PRELUDE = (
    "$ProgressPreference='SilentlyContinue'\n"
    "try{$__u=New-Object Text.UTF8Encoding $false;[Console]::OutputEncoding=$__u;"
    "[Console]::InputEncoding=$__u;$OutputEncoding=$__u}catch{}\n")
# $? first: it is the status of the caller's last statement.
_PS_EPILOGUE = "\nif($?){exit 0}elseif($LASTEXITCODE){exit $LASTEXITCODE}else{exit 1}"


# -- caps -----------------------------------------------------------------------

async def exec_(ctx, argv=None, cmd=None, stdin=None, timeout=30.0, cwd=None, env=None, shell=None):
    command, ran = _command(ctx, argv, cmd, shell)
    proc = await _start(ctx, command, cwd, env, stdin=stdin is not None)
    try:
        out, err, cut = await asyncio.wait_for(_collect(proc, stdin.encode() if stdin is not None else None),
                                               timeout)
    except asyncio.TimeoutError:
        await _kill(proc)
        raise ctx.Error(f"timed out after {timeout:g}s and was killed; anything it already did "
                        "still happened. Use shell.open for long jobs.") from None
    except BaseException:   # the call was cancelled: the process goes too
        await _kill(proc)
        raise
    reply = {"ok": proc.returncode == 0, "code": proc.returncode,
             "stdout": _decode(out), "stderr": _decode(err)}
    if cut:
        reply["truncated"] = True
    if ran and (ran != "sh" or IS_WIN):
        reply["shell"] = ran
    if getattr(ctx, "lease", None) is None and looks_denied(proc.returncode, err):
        # Never re-run on our own: the command may have half-run. The caller
        # asks again with elevation if that is what it wants.
        reply["denied"] = True
    return reply


async def _collect(proc, stdin: bytes | None) -> tuple[bytes, bytes, bool]:
    """communicate() with a ceiling: each stream keeps its first OUT_MAX
    bytes and the rest is drained and dropped, so ``yes`` for 30 s can't run
    the worker out of memory. The process isn't stopped for it."""
    cut = False

    async def feed() -> None:
        if stdin is None:
            return
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    async def drain(stream) -> bytes:
        nonlocal cut
        buf = bytearray()
        while chunk := await stream.read(CHUNK):
            if len(buf) + len(chunk) > OUT_MAX:
                cut = True
            buf += chunk[:max(0, OUT_MAX - len(buf))]
        return bytes(buf)

    _, out, err = await asyncio.gather(feed(), drain(proc.stdout), drain(proc.stderr))
    await proc.wait()
    return out, err, cut


def which(ctx, name):
    for candidate in (name, *((name + ".exe", name + ".cmd") if IS_WIN and not os.path.splitext(name)[1] else ())):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def env(ctx, name=None, prefix=None, default=None):
    if (name is None) == (prefix is None):
        raise ctx.Error("give name or prefix")
    if name is not None:
        if name not in os.environ:
            return default
        return "***" if _SECRET_NAME.search(name) else os.environ[name]
    return {k: ("***" if _SECRET_NAME.search(k) else v)
            for k, v in sorted(os.environ.items()) if k.startswith(prefix)}


async def open_(ctx, argv=None, cmd=None, shell=None, cwd=None, env=None, pty=True,
                cols=120, rows=32, title=""):
    if argv is None and cmd is None:   # an interactive shell
        argv = ([_powershell() or "powershell.exe", "-NoLogo"] if IS_WIN
                else [os.environ.get("SHELL") or "/bin/sh", "-l"])
    command, ran = _command(ctx, argv, cmd, shell)
    if pty and IS_WIN:
        # TODO(castling:ports): ConPTY (0.4 rook/worker/conpty.py) isn't ported; pty=false works.
        raise ctx.Error("pty is not available on Windows workers yet; use pty=false")
    handle = ctx.stream("terminal" if pty else "process", title=title or " ".join(command)[:200],
                        attrs={"cwd": cwd or os.getcwd(), "pty": bool(pty), "argv": command,
                               **({"shell": ran} if ran else {})})
    master = None
    try:
        if pty:
            import pty as ptys
            master, slave = ptys.openpty()
            os.set_blocking(master, False)
            try:
                _winsize(master, cols, rows)
                proc = await _start(ctx, command, cwd, env, slave=slave)
            finally:
                os.close(slave)
        else:
            proc = await _start(ctx, command, cwd, env, stdin=True, merge=True)
    except BaseException as e:
        if master is not None:
            os.close(master)
        handle.close(error=getattr(e, "message", None) or str(e) or type(e).__name__)
        raise
    handle.streams.update(handle.id, attrs={"pid": proc.pid})
    handle.append("status", {"state": "started", "pid": proc.pid})
    task = asyncio.get_running_loop().create_task(_pump(handle, proc, master))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return handle


async def stop(ctx):
    """Plugin unload: the open terminals' processes end with it."""
    for task in list(_RUNNING):
        task.cancel()
    await asyncio.gather(*_RUNNING, return_exceptions=True)


# -- processes ------------------------------------------------------------------

def _command(ctx, argv, cmd, shell):
    if (argv is None) == (cmd is None):
        raise ctx.Error("give argv or cmd, not both" if argv is not None else "give argv or cmd")
    if argv is not None:
        if shell is not None:
            raise ctx.Error("shell applies to cmd; argv runs with no shell")
        if not argv:
            raise ctx.Error("argv is empty")
        return _resolve(argv), None
    try:
        return _shell_argv(cmd, shell)
    except ValueError as e:
        raise ctx.Error(str(e)) from None


async def _start(ctx, argv, cwd, env, *, stdin=False, merge=False, slave=None):
    child_env = None
    if env:
        child_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}}
    if slave is not None:
        # setsid (start_new_session), then the PTY becomes the controlling
        # terminal: job control and a typed Ctrl-C work as in a real one.
        io = {"stdin": slave, "stdout": slave, "stderr": slave, "preexec_fn": _take_tty}
    else:
        io = {"stdin": subprocess.PIPE if stdin else subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.STDOUT if merge else subprocess.PIPE}
    extra = {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {"start_new_session": True}
    try:
        return await launch(argv, getattr(ctx, "lease", None), cwd=cwd, env=child_env, **io, **extra)
    except FileNotFoundError:
        raise ctx.Error(f"not found: {cwd if cwd and not os.path.isdir(cwd) else argv[0]}") from None
    except NotADirectoryError:
        raise ctx.Error(f"not a directory: {cwd}") from None


async def _kill(proc) -> None:
    """End the process and everything in its session (sh -c's children too).
    TERM first: an elevated process is sudo, which passes TERM on to the
    command it runs as root, where our KILL could only reach sudo itself."""
    for sig, grace in ((signal.SIGTERM, 2), (getattr(signal, "SIGKILL", None), 5)):
        if proc.returncode is not None:
            return
        try:
            if IS_WIN:
                proc.kill()
            else:
                os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), grace)
            return
        except asyncio.TimeoutError:
            pass


def _take_tty() -> None:
    import fcntl
    import termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _signal(proc, name: str) -> None:
    try:
        if IS_WIN:
            proc.terminate()     # no process groups to signal on Windows: every signal ends it
        else:
            os.killpg(proc.pid, _SIGNALS[name])
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _pump(handle, proc, master) -> None:
    """Output to the stream, input from it, until the process exits."""
    loop = asyncio.get_running_loop()
    feeder = loop.create_task(_feed(handle, proc, master))
    try:
        if master is not None:
            await _read_pty(handle, master, loop)
        else:
            while chunk := await proc.stdout.read(CHUNK):
                handle.append("bytes", chunk)
        code = await proc.wait()
    except BaseException as e:   # cancelled, or the stream was closed under it (stream.close)
        await _kill(proc)
        code = proc.returncode
        if not isinstance(e, Exception):
            raise
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        if master is not None:
            os.close(master)
        if handle.live:
            handle.close(exit=code if proc.returncode is not None else None)


async def _read_pty(handle, master, loop) -> None:
    got: asyncio.Queue = asyncio.Queue()

    def readable():
        try:
            data = os.read(master, CHUNK)
        except BlockingIOError:
            return
        except OSError:          # EIO: the other end closed
            data = b""
        got.put_nowait(data)
        if not data:
            loop.remove_reader(master)

    loop.add_reader(master, readable)
    try:
        while data := await got.get():
            handle.append("bytes", data)
    finally:
        loop.remove_reader(master)


async def _feed(handle, proc, master) -> None:
    async for ev in handle.inputs():
        body = ev.body
        if "signal" in body:
            _signal(proc, body["signal"])
        elif "resize" in body:
            if master is not None:
                _winsize(master, *body["resize"])
        else:
            data = body["text"].encode()
            try:
                if master is not None:
                    # Non-blocking: a child that stops reading must not stall the worker's loop.
                    while data:
                        try:
                            data = data[os.write(master, data):]
                        except BlockingIOError:
                            await asyncio.sleep(0.05)
                else:
                    proc.stdin.write(data)
                    await proc.stdin.drain()
            except (OSError, ConnectionError, AttributeError):
                return            # the process closed its input


def _winsize(fd: int, cols: int, rows: int) -> None:
    import fcntl
    import struct
    import termios
    cols, rows = max(1, min(int(cols), 1000)), max(1, min(int(rows), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if IS_WIN:   # ipconfig and friends write the OEM code page into a pipe
        try:
            return data.decode("oem")
        except (LookupError, UnicodeDecodeError):
            pass
    return data.decode("utf-8", errors="replace")


# -- the cmd shell model (0.4 rook/worker/shells.py) ----------------------------

def _shell_argv(cmd: str, shell: str | None) -> tuple[list[str], str]:
    name = str(shell or "").strip().lower() or ("powershell" if IS_WIN else "sh")
    name = _ALIASES.get(name, name)
    if name not in SHELLS:
        raise ValueError(f"shell must be one of {', '.join(SHELLS)}")
    if name == "powershell":
        binary = _powershell()
        if not binary:
            raise ValueError("PowerShell is not installed on this worker")
        aliases = "".join(f"Set-Alias {n} '{p}'\n" for n, p in _python_aliases().items())
        argv = [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                _PS_PRELUDE + aliases + cmd + _PS_EPILOGUE]
        if IS_WIN and len(subprocess.list2cmdline(argv)) >= MAX_CMDLINE:
            raise ValueError("cmd is too long for a Windows command line; write a .ps1 file and run that")
        return argv, name
    if name == "cmd":
        if not IS_WIN:
            raise ValueError("cmd.exe only exists on Windows workers")
        # Known limit: launch takes argv, so the line is MSVCRT-quoted; a cmd
        # line with double quotes in it is better sent to PowerShell.
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", cmd], name
    if IS_WIN:
        binary = _git_sh(name)
        if not binary:
            raise ValueError(f"no {name} on this worker (install Git for Windows; WSL's bash is not used)")
        return [binary, "-c", cmd], name
    if name == "bash":
        binary = shutil.which("bash")
        if not binary:
            raise ValueError("bash is not installed on this worker")
        return [binary, "-c", cmd], name
    return ["/bin/sh", "-c", cmd], name


def _powershell() -> str | None:
    for name in (("pwsh", "powershell") if IS_WIN else ("pwsh",)):
        found = shutil.which(name)
        if found:
            return found
    if IS_WIN:
        path = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                            "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(path):
            return path
    return None


def _git_sh(name: str) -> str | None:
    """Git for Windows' sh/bash; a bare bash on PATH is usually WSL's launcher."""
    roots = []
    git = shutil.which("git")
    if git:
        roots += [os.path.dirname(os.path.dirname(git)), os.path.dirname(os.path.dirname(os.path.dirname(git)))]
    for var in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        if os.environ.get(var):
            roots += [os.path.join(os.environ[var], "Git"), os.path.join(os.environ[var], "Programs", "Git")]
    for root in roots:
        for sub in ("bin", os.path.join("usr", "bin")):
            path = os.path.join(root, sub, name + ".exe")
            if os.path.isfile(path):
                return path
    found = shutil.which(name)
    if found and not re.search(r"[\\/](System32|WindowsApps)[\\/]", found, re.I):
        return found
    return None


_STORE_STUB = re.compile(r"[\\/]Microsoft[\\/]WindowsApps[\\/]", re.I)
_NPM_SHIM = re.compile(r'"%~?dp0%?\\([^"%*\r\n]+?\.(?:js|cjs|mjs|exe))"', re.I)


def _python_aliases() -> dict[str, str]:
    """python3/python -> a real interpreter where PATH finds only the Store stub. Windows only."""
    if not IS_WIN:
        return {}
    out = {}
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and not _STORE_STUB.search(found):
            continue
        for d in os.environ.get("PATH", "").split(os.pathsep):
            exe = os.path.join(d, "python.exe")
            if d and not _STORE_STUB.search(d + os.sep) and os.path.isfile(exe):
                out[name] = exe
                break
        else:
            base = getattr(sys, "_base_executable", "") or sys.executable
            exe = os.path.join(os.path.dirname(base), "python.exe")
            if os.path.isfile(exe):
                out[name] = exe
    return out


def _resolve(argv) -> list[str]:
    """argv[0] found as a shell would on Windows (PATHEXT, npm shims, the
    python Store stub); unchanged elsewhere."""
    argv = [str(a) for a in argv]
    if not IS_WIN:
        return argv
    head = argv[0]
    base = os.path.basename(head).lower().removesuffix(".exe")
    if base in ("python3", "python") and not os.path.dirname(head):
        real = _python_aliases().get(base)
        if real:
            return [real, *argv[1:]]
    found = head if os.path.dirname(head) else shutil.which(head)
    if found and found.lower().endswith((".cmd", ".bat")):
        try:
            with open(found, encoding="utf-8", errors="replace") as f:
                m = _NPM_SHIM.search(f.read(16384))
        except OSError:
            m = None
        if m:
            target = os.path.normpath(os.path.join(os.path.dirname(found), *re.split(r"[\\/]", m.group(1))))
            if os.path.isfile(target):
                if target.lower().endswith(".exe"):
                    return [target, *argv[1:]]
                node = os.path.join(os.path.dirname(found), "node.exe")
                node = node if os.path.isfile(node) else shutil.which("node")
                if node:
                    return [node, target, *argv[1:]]
    return [found or head, *argv[1:]]
