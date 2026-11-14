"""shell: run commands and open terminals on this worker, shipped as a plugin installed
by default (PL7). What the caps take and give comes from docs/inventory.md: CAP-01
(shell.exec, shell.which), CAP-03 (shell.open, a terminal as a stream: S1, S2) and
WRK4-070 (the shell model).

The shell model: `cmd` is a string run by a shell, `/bin/sh -c` unless `shell` names
another: sh, bash or powershell (PowerShell 7, `pwsh`). `argv` is a list run with no
shell at all. Only Linux and macOS are built: on Windows each cap answers that it is
not built there yet (W5).

Nothing here bounds a command: no timeout, no size cap, no list of what may run.
The plan defers all of that (Build rules). A result over the largest message is
answered `cap_failed` by the worker, as for every cap (docs/protocol.md, "Sizes")."""

import asyncio
import os
import shutil
import signal
import subprocess
import sys

from rook_protocol import MAX_STREAM_DATA
from rook_worker import Cap

WINDOWS = sys.platform == "win32"
SHELLS = ("sh", "bash", "powershell", "cmd")
SAME_SHELL = {"pwsh": "powershell", "pwsh.exe": "powershell", "powershell.exe": "powershell",
              "cmd.exe": "cmd", "/bin/sh": "sh", "sh.exe": "sh", "bash.exe": "bash"}
# PowerShell answers like `sh -c` where a caller reading the reply cares: no progress
# records, UTF-8 output, and the exit code of the last command.
PS_BEFORE = "$ProgressPreference = 'SilentlyContinue'\n[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
PS_AFTER = "\nif ($?) { exit 0 } elseif ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }"
_running = set()   # each open terminal's task, kept until it ends


# ------------------------------------------------------------------- arguments

def _check(args, cap, allowed):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    if WINDOWS:
        raise ValueError(f"{cap} is not built for Windows workers yet; call it on a Linux or macOS worker")
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed)}")


def _text(args, name):
    value = args.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _command(args):
    """(argv to start, the shell that runs cmd or None) from argv, or cmd and shell."""
    argv, cmd, shell = args.get("argv"), _text(args, "cmd"), _text(args, "shell")
    if argv is not None and cmd is not None:
        raise ValueError("give argv or cmd, not both")
    if argv is not None:
        if shell is not None:
            raise ValueError("shell applies to cmd; argv runs with no shell")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a list of strings: the program and its arguments, such as [\"git\", \"status\"]")
        return argv, None
    if cmd is None:
        raise ValueError("give argv (a list: the program and its arguments) or cmd (a string for a shell)")
    name = SAME_SHELL.get((shell or "sh").strip().lower(), (shell or "sh").strip().lower())
    if name not in SHELLS:
        raise ValueError(f"shell must be one of {', '.join(SHELLS)}")
    if name == "cmd":
        raise ValueError("cmd.exe only exists on Windows workers; use shell sh, bash or powershell")
    if name == "powershell":
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            raise ValueError("PowerShell (pwsh) is not installed on this worker; use shell sh or bash")
        return [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", PS_BEFORE + cmd + PS_AFTER], name
    if name == "bash":
        bash = shutil.which("bash")
        if bash is None:
            raise ValueError("bash is not installed on this worker; use shell sh")
        return [bash, "-c", cmd], name
    return ["/bin/sh", "-c", cmd], name


def _environment(args):
    """The worker's environment with `env` laid over it, or None for the worker's own."""
    env = args.get("env")
    if env is None:
        return None
    if not isinstance(env, dict):
        raise ValueError("env must be an object of variable names to values")
    return {**os.environ, **{str(k): str(v) for k, v in env.items()}}


async def _start(argv, args, **pipes):
    cwd = _text(args, "cwd")
    try:
        return await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=_environment(args), **pipes)
    except FileNotFoundError:
        if cwd is not None and not os.path.isdir(cwd):
            raise ValueError(f"the folder {cwd} does not exist on this worker; give cwd as an existing folder") from None
        raise ValueError(f"{argv[0]} was not found on this worker; find it with shell.which, or give its full path") from None
    except NotADirectoryError:
        raise ValueError(f"{cwd} is not a folder; give cwd as a folder") from None
    except PermissionError as e:
        raise ValueError(f"this worker's user may not run that: {e}") from None


def _decode(data):
    return data.decode("utf-8", errors="replace")


# ------------------------------------------------------------------- shell.exec

async def run(args):
    _check(args, "shell.exec", ("argv", "cmd", "shell", "stdin", "cwd", "env"))
    argv, ran = _command(args)
    stdin = _text(args, "stdin")
    proc = await _start(argv, args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await proc.communicate(stdin.encode("utf-8") if stdin is not None else None)
    reply = {"ok": proc.returncode == 0, "code": proc.returncode, "stdout": _decode(out), "stderr": _decode(err)}
    if ran is not None and ran != "sh":
        reply["shell"] = ran            # a reply names the shell that ran cmd, unless it was sh
    return reply


# ------------------------------------------------------------------ shell.which

async def which(args):
    _check(args, "shell.which", ("name",))
    name = _text(args, "name")
    if not name:
        raise ValueError("give name, a program's name, such as git")
    return shutil.which(name)


# ------------------------------------------------------------------- shell.open

def _size(args, name, default):
    value = args.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a whole number, 1 or more")
    return value


async def open_terminal(args, stream):
    _check(args, "shell.open", ("argv", "cmd", "shell", "cwd", "env", "pty", "cols", "rows", "title"))
    if args.get("argv") is None and args.get("cmd") is None:
        args = dict(args, argv=[os.environ.get("SHELL") or "/bin/sh", "-l"])    # an interactive login shell
    argv, _ = _command(args)
    pty = args.get("pty", True)
    if not isinstance(pty, bool):
        raise ValueError("pty must be true or false")
    cols, rows = _size(args, "cols", 120), _size(args, "rows", 32)
    title = _text(args, "title") or " ".join(argv)
    where = args.get("cwd") or os.getcwd()
    if pty:
        proc, master = await _on_terminal(argv, args, cols, rows)
        description = f"A terminal running {' '.join(argv)} in {where}."
    else:
        proc = await _start(argv, args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
        master = None
        description = f"{' '.join(argv)} in {where}, on pipes, its output and errors together."
    task = asyncio.create_task(_serve(stream, proc, master))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return {"title": title, "description": description}


async def _on_terminal(argv, args, cols, rows):
    """Start argv on a new pseudo-terminal, as its session's controlling terminal, so job
    control and a typed Ctrl-C work as in any terminal. Returns (process, the master end)."""
    import pty
    master, slave = pty.openpty()
    try:
        _resize(master, cols, rows)
        proc = await _start(argv, args, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                            preexec_fn=_take_terminal)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    os.set_blocking(master, False)
    return proc, master


def _take_terminal():
    import fcntl
    import termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _resize(fd, cols, rows):
    import fcntl
    import struct
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _serve(stream, proc, master):
    """Output to the stream and input from it, until the process's output ends; then the
    stream closes. A stream closed by a client (or by the plugin going) ends the process."""
    jobs = [asyncio.create_task(_typed(stream, proc, master))]
    if master is not None:
        jobs.append(asyncio.create_task(_sizes(stream, master)))
    try:
        if master is not None:
            await _terminal_output(stream, master)
        else:
            while data := await proc.stdout.read(MAX_STREAM_DATA):
                await stream.send(data)
        await proc.wait()
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if master is not None:
            os.close(master)
        _end(proc)
        await stream.close()


async def _terminal_output(stream, master):
    loop = asyncio.get_running_loop()
    got = asyncio.Queue()

    def readable():
        try:
            data = os.read(master, MAX_STREAM_DATA)
        except BlockingIOError:
            return
        except OSError:              # the terminal's other end is closed: the process is gone
            data = b""
        if not data:
            loop.remove_reader(master)
        got.put_nowait(data)

    loop.add_reader(master, readable)
    try:
        while data := await got.get():
            await stream.send(data)
    finally:
        loop.remove_reader(master)


async def _typed(stream, proc, master):
    """What clients type goes to the process. When the stream closes, the process ends."""
    while (data := await stream.read()) is not None:
        try:
            if master is not None:
                await _write(master, data)
            else:
                proc.stdin.write(data)
                await proc.stdin.drain()
        except (OSError, ConnectionError):
            pass                        # the process no longer reads its input; its output still comes
    _end(proc)


async def _write(fd, data):
    """All of `data` to a non-blocking descriptor, waiting while the process is not reading."""
    loop = asyncio.get_running_loop()
    while data:
        try:
            data = data[os.write(fd, data):]
        except BlockingIOError:
            ready = loop.create_future()
            loop.add_writer(fd, ready.set_result, None)
            try:
                await ready
            finally:
                loop.remove_writer(fd)


async def _sizes(stream, master):
    """The size a client shows the terminal at (stream_resize, Bake 13 Nov) reaches the process."""
    while (size := await stream.resized()) is not None:
        try:
            _resize(master, *size)
        except OSError:
            return


def _end(proc):
    """End the process and everything it started in its session."""
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# ------------------------------------------------------------------------- caps

_SHELL = {"type": "string", "enum": ["sh", "bash", "powershell"], "description": "The shell that runs cmd; sh unless given."}
_ARGV = {"type": "array", "items": {"type": "string"}, "minItems": 1,
         "description": "The program and its arguments, run with no shell, such as [\"git\", \"status\"]."}
_CMD = {"type": "string", "description": "A command line for a shell: pipes, redirection, globbing."}
_CWD = {"type": "string", "description": "The folder it runs in; the worker's own unless given."}
_ENV = {"type": "object", "additionalProperties": {"type": "string"},
        "description": "Variables added to the worker's environment."}

CAPS = {
    "shell.exec": Cap(
        "Run a command and wait for it to end: argv (a list, no shell) or cmd (a string for a shell); gives ok, code, stdout, stderr.",
        {"type": "object", "additionalProperties": False, "properties": {
            "argv": _ARGV, "cmd": _CMD, "shell": _SHELL, "cwd": _CWD, "env": _ENV,
            "stdin": {"type": "string", "description": "Text fed to the command's standard input."}}},
        run),
    "shell.which": Cap(
        "The full path of a program on this worker's PATH, or null if it is not there.",
        {"type": "object", "required": ["name"], "additionalProperties": False, "properties": {
            "name": {"type": "string", "description": "A program's name, such as git."}}},
        which),
    "shell.open": Cap(
        "Start a command (a login shell unless given) as a live stream: a terminal unless pty is false; it closes when the command ends.",
        {"type": "object", "additionalProperties": False, "properties": {
            "argv": _ARGV, "cmd": _CMD, "shell": _SHELL, "cwd": _CWD, "env": _ENV,
            "pty": {"type": "boolean", "description": "Run it on a terminal (true unless given); false runs it on pipes, output and errors together."},
            "cols": {"type": "integer", "minimum": 1, "description": "The terminal's columns at the start; 120 unless given."},
            "rows": {"type": "integer", "minimum": 1, "description": "The terminal's rows at the start; 32 unless given."},
            "title": {"type": "string", "description": "The stream's title; the command unless given."}}},
        open_terminal, opens_stream=True),
}
