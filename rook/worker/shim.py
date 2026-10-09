"""The session shim: `claude` / `codex` started in your own terminal run in a
Rook terminal the Sessions page can watch and type into (tier 1), with no
change to how they are started (docs/design/sessions.md §4 G).

Three parts:

* **Files** (this module): ``sessions.shim.install`` writes, under
  ``<worker state>/shim/``, one small POSIX ``sh`` script per agent in
  ``bin/`` (``claude``, ``codex``), the relay client (``client.py``, copied
  out of the worker bundle: :mod:`rook.worker.shim_client`), ``env.sh`` /
  ``env.fish`` (put ``bin/`` first on PATH) and ``installed.json`` (what was
  installed and which shell files were touched). Each shell's rc file gets
  one marked block that sources ``env.sh`` (fish: a file of its own in
  ``conf.d``); ``sessions.shim.uninstall`` removes exactly those and the
  folder. Nothing is installed unless asked: the default is off.
* **The scripts** resolve the real binary on PATH, skipping every folder
  that holds a ``.rook-shim-dir`` marker (so a shim never runs itself), and
  ``exec`` it unchanged unless this is an interactive session in a terminal
  (stdin, stdout and stderr ttys, ``ROOK_SHIM`` not ``0``, not already in a
  Rook terminal) and the worker's socket exists. Only then is Python started.
* **The local link** (:class:`LocalTermServer`): an ``AF_UNIX`` socket at
  ``<worker state>/shim/run/worker.sock``, in a 0700 folder, mode 0600, and
  every connection's peer uid checked against the worker's (SO_PEERCRED on
  Linux, LOCAL_PEERCRED on macOS). Each connection is one shim: the worker
  registers a *local* Rook terminal for it (``work.stream.*`` serve it like
  any other) whose output the shim streams in and whose input, signals and
  close the worker sends back. The shim owns the program and its PTY, so a
  worker restart never ends the session.

Windows is not covered yet (no AF_UNIX shell scripts there; a ``.cmd``/
PowerShell shim over a named pipe with ConPTY is the follow-up).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pkgutil
import shlex
import shutil
import socket
import struct
import sys
import time
from pathlib import Path

from .session_mirror import state_dir

log = logging.getLogger("rook.worker.shim")

AGENTS = ("claude", "codex")
SHELLS = ("bash", "zsh", "fish")
MARKER_FILE = ".rook-shim-dir"
BEGIN = "# >>> rook session shim >>>"
END = "# <<< rook session shim <<<"
SCRIPT_TAG = "rook-shim"
FISH_FILE = "rook-shim.fish"
MAX_FRAME = 1 << 20
MAX_SOCK_PATH = 100        # sun_path is 104 bytes on macOS, 108 on Linux
WRITE_TIMEOUT = 5.0


# -- paths -----------------------------------------------------------------------

def root() -> Path:
    return state_dir() / "shim"


def bin_dir() -> Path:
    return root() / "bin"


def socket_path() -> Path:
    return root() / "run" / "worker.sock"


def manifest_path() -> Path:
    return root() / "installed.json"


def client_path() -> Path:
    return root() / "client.py"


def _home(home: Path | None = None) -> Path:
    return Path(home) if home is not None else Path.home()


def login_shell() -> str:
    try:
        import pwd
        return os.path.basename(pwd.getpwuid(os.getuid()).pw_shell or "")
    except (ImportError, KeyError, OSError):
        return os.path.basename(os.environ.get("SHELL", ""))


def is_shim_dir(d: str) -> bool:
    try:
        return bool(d) and os.path.exists(os.path.join(d, MARKER_FILE))
    except (OSError, ValueError):
        return False


def which_real(name: str, path: str | None = None) -> str | None:
    """``shutil.which`` that skips shim folders: the real binary, never a
    shim (Rook terminals and the shim itself both use this rule)."""
    dirs = (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)
    return shutil.which(name, path=os.pathsep.join(d for d in dirs if not is_shim_dir(d)))


def find_real(agent: str, home: Path | None = None) -> str | None:
    """Where the real binary is, from the worker's PATH plus the usual
    per-user folders (a worker run by systemd has a short PATH)."""
    h = _home(home)
    extra = [h / ".local" / "bin", h / ".npm-global" / "bin", h / ".bun" / "bin",
             h / ".volta" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    return which_real(agent) or which_real(agent, os.pathsep.join(str(p) for p in extra))


# -- file templates ----------------------------------------------------------------

def client_source() -> bytes:
    data = pkgutil.get_data("rook.worker", "shim_client.py")
    if not data:
        raise RuntimeError("shim client missing from the worker bundle")
    return data


def shim_script(agent: str, *, python: str, client: str, sock: str, bindir: str) -> str:
    q = shlex.quote
    return f"""#!/bin/sh
# {SCRIPT_TAG} v1: {agent} (added by Rook: sessions.shim.install; removed by sessions.shim.uninstall)
# Runs the real {agent}. Interactive sessions in a terminal run in a Rook terminal
# so the Sessions page can watch them and type into them; everything else, or
# ROOK_SHIM=0, or no Rook worker here, runs the real {agent} directly.
n={q(agent)}
d={q(bindir)}
py={q(python)}
cl={q(client)}
sk={q(sock)}
r=
o=$IFS
IFS=:
set -f
for p in $PATH; do
  [ -n "$p" ] || p=.
  [ "$p" = "$d" ] && continue
  [ -e "$p/{MARKER_FILE}" ] && continue
  if [ -f "$p/$n" ] && [ -x "$p/$n" ]; then r=$p/$n; break; fi
done
IFS=$o
set +f
if [ -z "$r" ]; then
  echo "$n: command not found" >&2
  exit 127
fi
case ${{ROOK_SHIM-}} in 0|off|no|false) exec "$r" "$@" ;; esac
if [ -z "${{ROOK_WORK_TERMINAL-}}" ] && [ -t 0 ] && [ -t 1 ] && [ -t 2 ] && [ -S "$sk" ] && [ -x "$py" ] && [ -f "$cl" ]; then
  exec "$py" -I -S "$cl" "$sk" "$r" "$n" "$@"
fi
exec "$r" "$@"
"""


def env_sh(bindir: str) -> str:
    q = shlex.quote(bindir)
    return f"""# {SCRIPT_TAG}: puts Rook's session shim first on PATH (sessions.shim.install).
case "$PATH" in
  {q}|{q}:*) ;;
  *) PATH={q}"${{PATH:+:$PATH}}"; export PATH ;;
esac
"""


def env_fish(bindir: str) -> str:
    q = shlex.quote(bindir)
    return f"""# {SCRIPT_TAG}: puts Rook's session shim first on PATH (sessions.shim.install).
function __rook_shim_path
    if test "$PATH[1]" != {q}
        set -gx PATH {q} $PATH
    end
end
__rook_shim_path
# Again at the first prompt, after config.fish has set its own PATH.
function __rook_shim_path_late --on-event fish_prompt
    functions -e __rook_shim_path_late
    __rook_shim_path
end
"""


def rc_block(env_file: str) -> str:
    q = shlex.quote(env_file)
    return (f"{BEGIN}\n# Added by Rook (sessions.shim.install); sessions.shim.uninstall removes it.\n"
            f"[ -r {q} ] && . {q}\n{END}\n")


def fish_conf(env_file: str) -> str:
    q = shlex.quote(env_file)
    return (f"{BEGIN}\n# Added by Rook (sessions.shim.install); sessions.shim.uninstall removes this file.\n"
            f"test -r {q}; and source {q}\n{END}\n")


# -- install / uninstall -------------------------------------------------------------

def rc_targets(shells=None, home: Path | None = None, platform: str | None = None) -> list[tuple[str, Path]]:
    """The shell files to touch: for each shell asked for (default: those
    with a config here, plus the login shell)."""
    h = _home(home)
    platform = platform or sys.platform
    zdot = Path(os.environ.get("ZDOTDIR") or h)
    login = login_shell()
    if shells is None:
        shells = [s for s in SHELLS if s == login
                  or (s == "bash" and (h / ".bashrc").exists())
                  or (s == "zsh" and (zdot / ".zshrc").exists())
                  or (s == "fish" and (h / ".config" / "fish").is_dir())]
    out: list[tuple[str, Path]] = []
    for s in shells:
        if s not in SHELLS:
            raise ValueError(f"shells must be among {', '.join(SHELLS)}")
        if s == "bash":
            out.append(("bash", h / ".bashrc"))
            # Terminal.app starts login shells, which read only .bash_profile.
            if platform == "darwin" and (h / ".bash_profile").exists():
                out.append(("bash", h / ".bash_profile"))
        elif s == "zsh":
            out.append(("zsh", zdot / ".zshrc"))
        else:
            out.append(("fish", h / ".config" / "fish" / "conf.d" / FISH_FILE))
    return out


def _write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.rook-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _strip_block(text: str) -> tuple[str, bool]:
    """``text`` without the marked block (and the newline we put before it)."""
    lines = text.splitlines(keepends=True)
    out, inside, found = [], False, False
    for line in lines:
        s = line.rstrip("\r\n")
        if s == BEGIN:
            inside, found = True, True
            if out and out[-1] in ("\n", "\r\n"):
                out.pop()       # the blank line install added
            continue
        if inside:
            if s == END:
                inside = False
            continue
        out.append(line)
    return "".join(out), found


def _edit_rc(path: Path, block: str) -> tuple[str, bool]:
    """Append the block to an rc file (once). Returns the file actually
    written (a dotfile manager's symlink is followed, not replaced) and
    whether it was created."""
    real = Path(os.path.realpath(path))
    created = False
    try:
        text = real.read_text(encoding="utf-8")
        mode = real.stat().st_mode & 0o7777
    except FileNotFoundError:
        text, mode, created = "", 0o644, True
    text, _ = _strip_block(text)
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    _write_atomic(real, text + block, mode)
    return str(real), created


def read_manifest() -> dict:
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def installed() -> bool:
    return bool(read_manifest().get("agents"))


def write_client() -> bool:
    """(Re)write client.py from the running worker's bundle; True if changed."""
    src = client_source()
    path = client_path()
    try:
        if path.read_bytes() == src:
            return False
    except OSError:
        pass
    _write_atomic(path, src.decode("utf-8"), 0o600)
    return True


def install(agents=None, shells=None, python: str | None = None, home: Path | None = None) -> dict:
    """Write the shim and hook it into the shells. ``agents`` defaults to
    those found on this host (a shim for a program that is not installed
    would make ``command -v`` lie); naming one installs it regardless."""
    h = _home(home)
    python = python or sys.executable
    if not python or not os.path.isabs(python):
        raise ValueError("the worker's Python interpreter path is unknown")
    sock = str(socket_path())
    if len(sock.encode()) > MAX_SOCK_PATH:
        raise ValueError(f"the worker state folder is too deep for a local socket ({sock})")
    found = {a: find_real(a, h) for a in AGENTS}
    if agents is None:
        want = [a for a in AGENTS if found[a]]
        if not want:
            raise ValueError("neither claude nor codex was found on this host; "
                             "pass agents=[...] to install the shim anyway")
    else:
        want = list(agents) if isinstance(agents, (list, tuple)) else [str(agents)]
        bad = [a for a in want if a not in AGENTS]
        if bad or not want:
            raise ValueError(f"agents must be among {', '.join(AGENTS)}")
    if shells is not None and not isinstance(shells, (list, tuple)):
        shells = [str(shells)]
    targets = rc_targets(shells, h)
    if not targets:
        raise ValueError("no shell configuration found; pass shells=[\"bash\"|\"zsh\"|\"fish\"]")

    base = root()
    for d in (base, base / "run", bin_dir()):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    os.chmod(bin_dir(), 0o755)
    (bin_dir() / MARKER_FILE).write_text("Rook session shim folder: never resolved as a real binary.\n")
    write_client()
    old = read_manifest()
    for a in AGENTS:
        p = bin_dir() / a
        if a in want:
            _write_atomic(p, shim_script(a, python=python, client=str(client_path()), sock=sock,
                                         bindir=str(bin_dir())), 0o755)
        elif p.exists():
            p.unlink()
    _write_atomic(base / "env.sh", env_sh(str(bin_dir())))
    _write_atomic(base / "env.fish", env_fish(str(bin_dir())))
    touched = []
    for shell, path in targets:
        if shell == "fish":
            _write_atomic(path, fish_conf(str(base / "env.fish")))
            touched.append({"shell": shell, "path": str(path), "kind": "file"})
        else:
            real, created = _edit_rc(path, rc_block(str(base / "env.sh")))
            prev = next((t for t in old.get("rc") or [] if isinstance(t, dict) and t.get("path") == real), {})
            touched.append({"shell": shell, "path": real, "kind": "block",
                            "created": bool(created or prev.get("created"))})
    # Keep what an earlier install touched so uninstall still finds it.
    seen = {t["path"] for t in touched}
    touched += [t for t in old.get("rc") or [] if isinstance(t, dict) and t.get("path") not in seen]
    manifest = {"v": 1, "agents": want, "rc": touched, "python": python,
                "installed": time.time(), "socket": sock, "bin": str(bin_dir())}
    _write_atomic(manifest_path(), json.dumps(manifest, indent=1), 0o600)
    missing = [a for a in want if not found[a]]
    return {"ok": True, "agents": want, "real": {a: found[a] for a in want}, "missing": missing,
            "rc": touched, "bin": str(bin_dir()), "socket": sock,
            "note": ("Open a new terminal for it to take effect (or source the shell's rc file). "
                     "Set ROOK_SHIM=0 to bypass it for one command; sessions.shim.uninstall "
                     "removes it.")}


def uninstall(home: Path | None = None) -> dict:
    """Remove the rc blocks, the fish conf file and the shim folder."""
    h = _home(home)
    m = read_manifest()
    entries = [t for t in m.get("rc") or [] if isinstance(t, dict) and t.get("path")]
    # Also look where install would have written, in case the manifest is gone.
    for shell, path in rc_targets(list(SHELLS), h):
        if not any(t["path"] in (str(path), os.path.realpath(path)) for t in entries):
            entries.append({"shell": shell, "path": str(path),
                            "kind": "file" if shell == "fish" else "block"})
    removed = []
    for t in entries:
        path = Path(t["path"])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if t.get("kind") == "file":
            if BEGIN in text:
                path.unlink()
                removed.append(str(path))
            continue
        new, found = _strip_block(text)
        if found:
            real = Path(os.path.realpath(path))
            if t.get("created") and not new.strip():
                real.unlink()           # install created it for the block alone
            else:
                _write_atomic(real, new, real.stat().st_mode & 0o7777)
            removed.append(str(real))
    base = root()
    existed = base.exists()
    if existed:
        shutil.rmtree(base, ignore_errors=True)
    return {"ok": True, "removed": removed, "folder": str(base) if existed else None,
            "note": ("Shells already open keep the old PATH until restarted; run `hash -r` "
                     "(bash) or `rehash` (zsh) there if `claude` is not found.")}


def status(home: Path | None = None) -> dict:
    m = read_manifest()
    agents = list(m.get("agents") or [])
    shims = {}
    for a in AGENTS:
        p = bin_dir() / a
        shims[a] = {"shim": str(p) if p.exists() else None, "real": find_real(a, home)}
    rc = []
    for t in m.get("rc") or []:
        if not isinstance(t, dict) or not t.get("path"):
            continue
        try:
            present = BEGIN in Path(t["path"]).read_text(encoding="utf-8")
        except OSError:
            present = False
        rc.append({**t, "present": present})
    return {"ok": True, "installed": bool(agents), "agents": agents, "shims": shims, "rc": rc,
            "bin": str(bin_dir()), "socket": str(socket_path()),
            "client_current": _client_current() if agents else None}


def _client_current() -> bool:
    try:
        return client_path().read_bytes() == client_source()
    except (OSError, RuntimeError):
        return False


# -- the local link -----------------------------------------------------------------

def peer_uid(sock) -> int | None:
    """The uid of the process at the other end of a Unix socket."""
    try:
        if hasattr(socket, "SO_PEERCRED"):
            data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", data)[1]
        if sys.platform == "darwin":
            # struct xucred {u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16];}
            data = sock.getsockopt(0, 0x001, 76)        # SOL_LOCAL, LOCAL_PEERCRED
            return struct.unpack_from("I", data, 4)[0]
    except (OSError, struct.error):
        return None
    return None


def frame(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def jframe(obj: dict) -> bytes:
    return frame(b"J", json.dumps(obj, separators=(",", ":")).encode())


class LocalLink:
    """The worker's end of one shim: how a local terminal's input, signals
    and close reach the program (terminals.py calls these)."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.lock = asyncio.Lock()
        self.shim_pid: int | None = None

    @property
    def closed(self) -> bool:
        return self.writer.is_closing()

    async def _send(self, data: bytes) -> None:
        if self.closed:
            raise ValueError("terminal has exited")
        async with self.lock:
            self.writer.write(data)
            try:
                await asyncio.wait_for(self.writer.drain(), WRITE_TIMEOUT)
            except asyncio.TimeoutError:
                raise ValueError("terminal input is not being read") from None
            except (ConnectionError, OSError):
                raise ValueError("terminal has exited") from None

    async def input(self, data: bytes) -> None:
        await self._send(frame(b"I", data))

    def control(self, msg: dict) -> None:
        if not self.closed:
            self.writer.write(jframe(msg))

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass


class LocalTermServer:
    """Listens on the shim socket and turns each shim into a local terminal
    of the terminals plugin (``attach_local``)."""

    def __init__(self, terminals, path: Path | None = None) -> None:
        self.terminals = terminals          # () -> TerminalsPlugin | None
        self.path = Path(path) if path else socket_path()
        self.server: asyncio.AbstractServer | None = None
        self.links: set[LocalLink] = set()

    def _plugin(self):
        return self.terminals()

    @property
    def listening(self) -> bool:
        return self.server is not None

    async def start(self) -> None:
        if self.server is not None:
            return
        d = self.path.parent
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        # The folder is 0700 before the socket exists (no umask juggling: it
        # is process-wide and the worker has threads), then the socket 0600.
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        os.chmod(self.path, 0o600)
        log.info("session shim socket listening at %s", self.path)

    async def stop(self) -> None:
        if self.server is None:
            return
        self.server.close()
        for link in list(self.links):
            link.close()
        try:
            await asyncio.wait_for(self.server.wait_closed(), 2.0)
        except (asyncio.TimeoutError, Exception):
            pass
        self.server = None
        try:
            self.path.unlink()
        except OSError:
            pass

    @staticmethod
    async def _frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
        head = await reader.readexactly(5)
        n = struct.unpack(">I", head[1:])[0]
        if n > MAX_FRAME:
            raise ValueError("frame too large")
        return head[:1], await reader.readexactly(n)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        link, term = LocalLink(writer), None
        sock = writer.get_extra_info("socket")
        uid = peer_uid(sock) if sock is not None else None
        if uid is not None and uid != os.getuid():
            log.warning("session shim: refused a connection from uid %s", uid)
            link.close()
            return
        self.links.add(link)
        plugin = self._plugin()
        try:
            kind, payload = await asyncio.wait_for(self._frame(reader), 5.0)
            hello = json.loads(payload) if kind == b"J" else {}
            if hello.get("op") != "hello":
                raise ValueError("expected hello")
            if plugin is None:
                raise ValueError("this worker has no terminals")
            link.shim_pid = int(hello.get("shim_pid") or 0) or None
            try:
                term = plugin.attach_local(
                    agent=str(hello.get("agent") or ""), argv=hello.get("argv") or [],
                    cwd=str(hello.get("cwd") or ""), cols=int(hello.get("cols") or 0),
                    rows=int(hello.get("rows") or 0), link=link, pid=int(hello.get("pid") or 0))
            except ValueError as e:
                writer.write(jframe({"op": "welcome", "ok": False, "error": str(e)}))
                await writer.drain()
                return
            writer.write(jframe({"op": "welcome", "ok": True, "id": term.id}))
            await writer.drain()
            while True:
                kind, payload = await self._frame(reader)
                if kind == b"O":
                    term.append(payload)
                elif kind == b"J":
                    msg = json.loads(payload)
                    op = msg.get("op")
                    if op == "started":
                        term.pid = int(msg.get("pid") or 0) or None
                    elif op == "size":
                        plugin.local_size(term, int(msg.get("cols") or 0), int(msg.get("rows") or 0))
                    elif op == "exit":
                        code = msg.get("code")
                        plugin.local_exit(term, int(code) if isinstance(code, int) else None)
                        return
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        except (ValueError, TypeError) as e:
            log.debug("session shim connection: %s", e)
        except Exception:
            log.exception("session shim connection failed")
        finally:
            self.links.discard(link)
            if term is not None and term.running and plugin is not None:
                plugin.local_lost(term)
            link.close()
