"""Rook session shim client: `claude` / `codex` typed in your own terminal,
visible and steerable on the Sessions page (docs/design/sessions.md §4 G).

``sessions.shim.install`` puts small ``claude``/``codex`` shell scripts ahead
of the real ones on PATH. Each script falls through to the real binary for
anything that is not an interactive session in a terminal; otherwise it runs
this file with the worker's Python (``-I -S``: no site, no user path, so it
starts fast and cannot import anything planted in the current folder). It is
copied out of the worker bundle next to the scripts, so it imports nothing
from Rook: stdlib only, and it works on whatever Python the worker uses.

What it does, like ``script(1)`` with a second viewer:

1. Asks the local Rook worker over its owner-only Unix socket to register a
   terminal (``hello``). No answer within a fraction of a second, a refusal,
   or anything going wrong before the person's terminal is touched: exec the
   real binary exactly as the shell would have.
2. Runs the real binary under a pseudo-terminal it owns, with the same argv,
   cwd and environment (plus ``ROOK_WORK_TERMINAL``), the person's terminal
   settings and window size.
3. Relays: keystrokes from the person's terminal (raw mode) and from the
   Sessions page go in; output goes to the person's terminal and to the
   worker, which serves it as an ordinary Rook terminal (``work.stream.*``).
   The person's terminal owns the window size; SIGWINCH is forwarded.
4. When the program exits, restores the terminal and exits with the same
   status (re-raising the same signal when it was killed by one).

The shim owns the program, not the worker: a worker restart or update never
ends the session; the shim reconnects and re-registers (a new terminal id)
and carries on. Ctrl-Z suspends as usual (the shim stops itself when the
program stops, and continues it on ``fg``).

Wire (both directions): 1 byte kind + 4 bytes big-endian length + payload.
``J`` JSON control, ``O`` output (shim to worker), ``I`` input (worker to
shim). See rook/worker/shim.py for the worker side.
"""

import errno
import fcntl
import json
import os
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty

VERSION = 1
CONNECT_TIMEOUT = 0.08          # the worker is local: connect is instant or it is not there
WELCOME_TIMEOUT = 0.3           # its event loop may be busy; past this, run the real binary
RING_BYTES = 256 * 1024         # recent output replayed when re-registering
MAX_OUTQ = 2 * 1024 * 1024      # unsent output to the worker before the link is dropped
RETRY_SECS = 3.0                # reconnect after the worker went away
REFUSED_RETRY_SECS = 30.0
MAX_FRAME = 1 << 20

# Invocations that are not an interactive session: run the real binary as is.
NONINTERACTIVE = {
    "claude": {
        "flags": {"-p", "--print", "-v", "--version", "-h", "--help", "--bg",
                  "--output-format", "--input-format"},
        "commands": {"auth", "auto-mode", "config", "doctor", "gateway", "import",
                     "install", "logs", "mcp", "migrate-installer", "plugin", "plugins", "purge",
                     "respawn", "rm", "setup-token", "stop", "kill", "ultrareview", "update",
                     "upgrade", "api-key"},
    },
    "codex": {
        "flags": {"-h", "--help", "-V", "--version"},
        "commands": {"exec", "e", "review", "login", "logout", "mcp", "mcp-server", "plugin",
                     "app-server", "remote-control", "completion", "update", "doctor", "sandbox",
                     "debug", "apply", "a", "queue", "archive", "delete", "migrate-rollouts",
                     "unarchive", "exec-server", "features", "help", "proto", "generate-ts",
                     "responses-api-proxy", "stdio-to-uds"},
    },
}


def should_attach(name, args, environ=None):
    """(True, "") when this is an interactive session worth a Rook terminal,
    else (False, why). The shell script already checked the opt-out, the
    ttys and the socket; these are checked again here so the client is safe
    on its own."""
    env = os.environ if environ is None else environ
    if str(env.get("ROOK_SHIM", "")).strip().lower() in ("0", "off", "no", "false"):
        return False, "ROOK_SHIM is off"
    if env.get("ROOK_WORK_TERMINAL"):
        return False, "already in a Rook terminal"
    rules = NONINTERACTIVE.get(name)
    if rules is None:
        return False, "not a shimmed agent"
    for arg in args:
        if arg == "--":
            break
        if arg in rules["flags"] or arg.split("=", 1)[0] in rules["flags"]:
            return False, "non-interactive flag " + arg
    if args and not args[0].startswith("-") and args[0] in rules["commands"]:
        return False, "subcommand " + args[0]
    for fd in (0, 1, 2):
        if not os.isatty(fd):
            return False, "fd %d is not a terminal" % fd
    return True, ""


def debug(msg):
    if os.environ.get("ROOK_SHIM_DEBUG"):
        try:
            os.write(2, ("rook-shim: %s\r\n" % msg).encode("utf-8", "replace"))
        except OSError:
            pass


def exec_real(real, args):
    """Become the real binary, exactly as the shell script's fall-through."""
    try:
        os.execv(real, [real] + list(args))
    except OSError as e:
        sys.stderr.write("%s: %s\n" % (real, e.strerror))
        os._exit(126 if e.errno in (errno.EACCES, errno.ENOEXEC) else 127)


def frame(kind, payload):
    return kind + struct.pack(">I", len(payload)) + payload


def jframe(obj):
    return frame(b"J", json.dumps(obj, separators=(",", ":")).encode())


class Frames:
    """Incremental frame parser."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf.extend(data)
        out = []
        while len(self.buf) >= 5:
            n = struct.unpack(">I", bytes(self.buf[1:5]))[0]
            if n > MAX_FRAME:
                raise ValueError("frame too large")
            if len(self.buf) < 5 + n:
                break
            out.append((bytes(self.buf[:1]), bytes(self.buf[5:5 + n])))
            del self.buf[:5 + n]
        return out


def winsize(fd):
    try:
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
    except OSError:
        return 0, 0
    return cols, rows


def set_winsize(fd, cols, rows):
    if cols and rows:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass


def connect(path, hello, wait_welcome=True):
    """A connected socket after sending ``hello``; with ``wait_welcome`` it
    also returns the worker's answer (raises if refused or slow)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(CONNECT_TIMEOUT)
        s.connect(path)
        s.settimeout(WELCOME_TIMEOUT)
        s.sendall(jframe(hello))
        if not wait_welcome:
            s.setblocking(False)
            return s, None, Frames()
        frames, end = Frames(), time.monotonic() + WELCOME_TIMEOUT
        got = []
        while not got:
            left = end - time.monotonic()
            if left <= 0:
                raise socket.timeout("no answer from the worker")
            s.settimeout(left)
            data = s.recv(65536)
            if not data:
                raise ConnectionError("the worker closed the connection")
            got = frames.feed(data)
        kind, payload = got[0]
        msg = json.loads(payload) if kind == b"J" else {}
        if not msg.get("ok"):
            raise ConnectionRefusedError(str(msg.get("error") or "refused"))
        s.setblocking(False)
        # Anything that came with the welcome stays queued for the loop.
        rest = Frames()
        for k, p in got[1:]:
            rest.buf.extend(frame(k, p))
        rest.buf.extend(frames.buf)
        return s, msg, rest
    except BaseException:
        s.close()
        raise


class Session:
    """The relay between the person's terminal, the program and the worker."""

    def __init__(self, sock_path, real, name, args, sock, welcome, frames):
        self.sock_path, self.real, self.name, self.args = sock_path, real, name, list(args)
        self.sock, self.frames = sock, frames
        self.tid = str((welcome or {}).get("id") or "")
        self.outq = bytearray()
        self.inq = bytearray()           # to the program
        self.ring = bytearray()
        self.pending = []
        self.pid = 0
        self.master = -1
        self.status = None
        self.saved = None
        self.stdin_open = True
        self.stdout_ok = True
        self.master_open = True
        self.retry_at = None
        self.stopped_remotely = False

    # -- setup ---------------------------------------------------------------

    def spawn(self):
        import pty
        master, slave = pty.openpty()
        try:
            termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(0))
        except termios.error:
            pass
        cols, rows = winsize(0)
        set_winsize(slave, cols, rows)
        env = dict(os.environ)
        if self.tid:
            env["ROOK_WORK_TERMINAL"] = self.tid
        pid = os.fork()
        if pid == 0:  # child
            try:
                os.close(master)
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                for fd in (0, 1, 2):
                    os.dup2(slave, fd)
                if slave > 2:
                    os.close(slave)
                # Python ignores these at startup; exec keeps ignored signals
                # ignored, and the program must start as it would from a shell.
                for sig in ("SIGPIPE", "SIGXFSZ", "SIGXFZ"):
                    if hasattr(signal, sig):
                        signal.signal(getattr(signal, sig), signal.SIG_DFL)
                os.execve(self.real, [self.real] + self.args, env)
            except BaseException:
                pass
            os._exit(127)
        os.close(slave)
        os.set_blocking(master, False)
        self.pid, self.master = pid, master

    def raw(self):
        try:
            tty.setraw(0, termios.TCSANOW)
        except termios.error:
            pass

    def restore(self):
        if self.saved is not None:
            try:
                termios.tcsetattr(0, termios.TCSADRAIN, self.saved)
            except termios.error:
                pass

    # -- worker link -----------------------------------------------------------

    def send(self, data):
        if self.sock is None:
            return
        self.outq.extend(data)
        if len(self.outq) > MAX_OUTQ:
            debug("worker is not keeping up; reconnecting")
            self.drop(RETRY_SECS)

    def drop(self, retry):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock, self.frames = None, Frames()
        self.outq.clear()
        self.retry_at = time.monotonic() + retry if retry else None

    def hello(self, reattach=False):
        cols, rows = winsize(0)
        msg = {"op": "hello", "v": VERSION, "agent": self.name, "argv": self.args[:200],
               "cwd": os.getcwd(), "cols": cols, "rows": rows, "shim_pid": os.getpid(),
               "term": os.environ.get("TERM", "")}
        if reattach:
            msg.update(pid=self.pid, reattach=True, prev=self.tid)
        return msg

    def reconnect(self):
        self.retry_at = None
        try:
            sock, _w, frames = connect(self.sock_path, self.hello(reattach=True), wait_welcome=False)
        except OSError:
            self.retry_at = time.monotonic() + RETRY_SECS
            return
        self.sock, self.frames = sock, frames
        if self.ring:
            self.send(frame(b"O", bytes(self.ring)))
        debug("re-registered with the worker")

    def on_control(self, msg):
        op = msg.get("op")
        if op == "welcome":
            if not msg.get("ok"):
                self.drop(REFUSED_RETRY_SECS)
            else:
                self.tid = str(msg.get("id") or self.tid)
        elif op == "signal":
            name = "SIG" + str(msg.get("sig", "INT")).upper().replace("SIG", "")
            if name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT", "SIGKILL"):
                self.signal_fg(getattr(signal, name))
        elif op == "hangup":
            self.stopped_remotely = True
            self.kill_child(signal.SIGHUP)
        elif op == "kill":
            self.stopped_remotely = True
            self.kill_child(signal.SIGKILL)

    def signal_fg(self, sig):
        pgrp = 0
        try:
            pgrp = os.tcgetpgrp(self.master)
        except OSError:
            pass
        try:
            os.killpg(pgrp if pgrp > 0 else self.pid, sig)
        except OSError:
            pass

    def kill_child(self, sig):
        if self.pid and self.status is None:
            try:
                os.killpg(self.pid, sig)
            except OSError:
                try:
                    os.kill(self.pid, sig)
                except OSError:
                    pass

    # -- io ------------------------------------------------------------------------

    def output(self, data):
        if self.stdout_ok:
            view = memoryview(data)
            while view:
                try:
                    view = view[os.write(1, view):]
                except InterruptedError:
                    continue
                except BlockingIOError:
                    select.select([], [1], [], 1.0)
                except OSError:
                    self.stdout_ok = False
                    break
        self.ring.extend(data)
        if len(self.ring) > RING_BYTES:
            del self.ring[:len(self.ring) - RING_BYTES]
        self.send(frame(b"O", data))

    def read_master(self):
        while True:
            try:
                data = os.read(self.master, 65536)
            except BlockingIOError:
                return
            except InterruptedError:
                continue
            except OSError:      # EIO: the program and everything on its tty are gone
                self.master_open = False
                return
            if not data:
                self.master_open = False
                return
            self.output(data)

    def write_master(self):
        while self.inq:
            try:
                n = os.write(self.master, bytes(self.inq[:4096]))
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self.inq.clear()
                return
            del self.inq[:n]

    def read_stdin(self):
        try:
            data = os.read(0, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            # The person's terminal went away: what a closed window does to a
            # program running in it.
            self.stdin_open = False
            self.kill_child(signal.SIGHUP)
            return
        self.inq.extend(data)
        self.write_master()

    def read_sock(self):
        try:
            data = self.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            debug("lost the worker; will reconnect")
            self.drop(RETRY_SECS)
            return
        try:
            frames = self.frames.feed(data)
        except ValueError:
            self.drop(RETRY_SECS)
            return
        for kind, payload in frames:
            if kind == b"I":
                self.inq.extend(payload)
            elif kind == b"J":
                try:
                    self.on_control(json.loads(payload))
                except ValueError:
                    pass
        self.write_master()

    def write_sock(self):
        try:
            n = self.sock.send(bytes(self.outq[:262144]))
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.drop(RETRY_SECS)
            return
        del self.outq[:n]

    # -- signals -------------------------------------------------------------------

    def on_signal(self, sig, _frame=None):
        self.pending.append(sig)

    def handle_signals(self):
        while self.pending:
            sig = self.pending.pop(0)
            if sig == signal.SIGWINCH:
                cols, rows = winsize(0)
                set_winsize(self.master, cols, rows)
                self.send(jframe({"op": "size", "cols": cols, "rows": rows}))
            elif sig == signal.SIGCHLD:
                self.reap()
            elif sig == signal.SIGCONT:
                self.raw()
                self.pending.append(signal.SIGWINCH)
            elif sig == signal.SIGHUP:
                self.stdin_open = self.stdout_ok = False
                self.kill_child(signal.SIGHUP)
            else:  # TERM, INT, QUIT, TSTP sent to the shim with kill(1)
                self.kill_child(sig)

    def reap(self):
        while self.status is None and self.pid:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG | os.WUNTRACED)
            except ChildProcessError:
                self.status = 0
                return
            if pid == 0:
                return
            if os.WIFSTOPPED(status):
                self.suspend()
                continue
            self.status = status

    def suspend(self):
        """The program stopped itself (Ctrl-Z): give the terminal back and
        stop too, so the shell's job control sees it; on fg carry on."""
        self.restore()
        os.kill(os.getpid(), signal.SIGSTOP)
        # Continued.
        self.raw()
        cols, rows = winsize(0)
        set_winsize(self.master, cols, rows)
        try:
            os.killpg(self.pid, signal.SIGCONT)
        except OSError:
            pass

    # -- main loop -----------------------------------------------------------------

    def run(self):
        try:
            self.saved = termios.tcgetattr(0)
        except termios.error:
            self.saved = None
        try:
            self.spawn()
        except Exception as e:
            # Nothing has touched the person's terminal yet.
            debug("could not start under a terminal (%s); running directly" % e)
            self.drop(0)
            exec_real(self.real, self.args)
        if self.sock is not None:
            self.send(jframe({"op": "started", "pid": self.pid}))
        wake_r, wake_w = os.pipe()
        for fd in (wake_r, wake_w):
            os.set_blocking(fd, False)
        signal.set_wakeup_fd(wake_w)
        for sig in (signal.SIGWINCH, signal.SIGCHLD, signal.SIGHUP, signal.SIGTERM,
                    signal.SIGINT, signal.SIGQUIT, signal.SIGCONT, signal.SIGTSTP):
            signal.signal(sig, self.on_signal)
        self.reap()       # a program that exited before the handlers were in place
        self.raw()
        try:
            self.loop(wake_r)
        finally:
            self.restore()
        self.finish()
        return self.exit()

    def loop(self, wake_r):
        while True:
            self.handle_signals()
            if self.status is None:
                self.reap()
            if self.status is not None:
                self.read_master()    # whatever the program wrote last
                return
            if self.sock is None and self.retry_at is not None and time.monotonic() >= self.retry_at:
                self.reconnect()
            rl = [wake_r]
            if self.master_open:
                rl.append(self.master)
            if self.stdin_open:
                rl.append(0)
            wl = []
            if self.inq and self.master_open:
                wl.append(self.master)
            if self.sock is not None:
                rl.append(self.sock)
                if self.outq:
                    wl.append(self.sock)
            timeout = None
            if self.sock is None and self.retry_at is not None:
                timeout = max(0.0, self.retry_at - time.monotonic())
            if not self.master_open:
                timeout = 0.05 if timeout is None else min(timeout, 0.05)
            try:
                r, w, _x = select.select(rl, wl, [], timeout)
            except InterruptedError:
                continue
            if wake_r in r:
                try:
                    while os.read(wake_r, 512):
                        pass
                except OSError:
                    pass
            if self.master in r:
                self.read_master()
            if 0 in r:
                self.read_stdin()
            if self.sock is not None and self.sock in r:
                self.read_sock()
            if self.sock is not None and self.sock in w:
                self.write_sock()
            if self.master in w:
                self.write_master()

    def finish(self):
        if self.sock is None:
            return
        code, sig = self.code()
        self.send(jframe({"op": "exit", "code": code, "signal": sig}))
        end = time.monotonic() + 0.5
        while self.outq and self.sock is not None and time.monotonic() < end:
            try:
                select.select([], [self.sock], [], 0.1)
            except InterruptedError:
                pass
            self.write_sock()
        self.drop(0)
        if self.stopped_remotely:
            try:
                os.write(2, b"[rook] This session was stopped from the Sessions page.\r\n")
            except OSError:
                pass

    def code(self):
        status = self.status or 0
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status), os.WTERMSIG(status)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status), None
        return 1, None

    def exit(self):
        code, sig = self.code()
        if sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGKILL,
                   signal.SIGPIPE, signal.SIGALRM, signal.SIGUSR1, signal.SIGUSR2):
            # Die the same way, so the shell reports what it would have.
            try:
                sys.stdout.flush()
            except Exception:
                pass
            if sig != signal.SIGKILL:
                signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)
            time.sleep(0.1)
        return code


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 4:
        sys.stderr.write("usage: shim_client.py SOCKET REAL NAME [ARGS...]\n")
        return 2
    sock_path, real, name, args = argv[1], argv[2], argv[3], argv[4:]
    sock = welcome = frames = None
    try:
        ok, why = should_attach(name, args)
        if ok:
            cols, rows = winsize(0)
            hello = {"op": "hello", "v": VERSION, "agent": name, "argv": args[:200],
                     "cwd": os.getcwd(), "cols": cols, "rows": rows, "shim_pid": os.getpid(),
                     "term": os.environ.get("TERM", "")}
            sock, welcome, frames = connect(sock_path, hello)
    except Exception as e:
        sock, why = None, "worker: %s" % (e or type(e).__name__)
    if sock is None:
        debug("running %s directly (%s)" % (name, why))
        exec_real(real, args)
        return 127
    debug("terminal %s" % welcome.get("id"))
    return Session(sock_path, real, name, args, sock, welcome, frames).run()


if __name__ == "__main__":
    sys.exit(main())
