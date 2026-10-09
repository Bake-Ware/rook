"""Session shim (docs/design/sessions.md §4 G): `claude`/`codex` typed in your
own terminal run in a Rook terminal the Sessions page can watch and type into.

Every test runs with a scratch HOME and ROOK_WORKER_HOME; nothing touches the
real home folder or a real shell configuration.
"""
import asyncio
import fcntl
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

import pytest
import pytest_asyncio

from rook.worker import shim, shim_client, termwire
from rook.worker.plugins.terminals import TerminalsPlugin
from rook.worker.plugins.sessions import record
from rook.remote.term_hub import TermStream

pytestmark = pytest.mark.skipif(os.name != "posix" or sys.platform == "win32",
                                reason="the session shim is POSIX-only")


@pytest.fixture
def home(monkeypatch):
    # Short paths: a Unix socket path must stay under ~100 bytes.
    base = Path(tempfile.mkdtemp(prefix="rsh"))
    h, state = base / "h", base / "w"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("ROOK_WORKER_HOME", str(state))
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.delenv("ZDOTDIR", raising=False)
    monkeypatch.delenv("ROOK_WORK_TERMINAL", raising=False)
    monkeypatch.delenv("ROOK_SHIM", raising=False)
    monkeypatch.setattr(shim, "login_shell", lambda: "sh")
    yield h
    shutil.rmtree(base, ignore_errors=True)


FAKE = r'''#!{py}
import fcntl, os, signal, struct, sys, termios
def size():
    rows, cols = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))[:2]
    return "%dx%d" % (cols, rows)
print("FAKE argv=[%s] tty=%s rt=%s size=%s" % (" ".join(sys.argv[1:]), os.isatty(0),
      os.environ.get("ROOK_WORK_TERMINAL", "-"), size()), flush=True)
signal.signal(signal.SIGWINCH, lambda *a: print("WINCH size=" + size(), flush=True))
while True:
    line = sys.stdin.readline()
    if not line:
        sys.exit(3)
    line = line.strip()
    if line == "quit":
        sys.exit(7)
    print("GOT " + line, flush=True)
'''


def fake_agent(dirpath: Path, name="claude") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text(FAKE.replace("{py}", sys.executable))
    p.chmod(0o755)
    return p


# -- which invocations attach ------------------------------------------------------------

@pytest.mark.parametrize("name,args,want", [
    ("claude", [], True),
    ("claude", ["fix the bug"], True),
    ("claude", ["--resume", "abc", "--model", "opus"], True),
    ("claude", ["attach", "x"], True),
    ("claude", ["-p", "hi"], False),
    ("claude", ["--print", "hi"], False),
    ("claude", ["--output-format=json", "-p", "x"], False),
    ("claude", ["--version"], False),
    ("claude", ["mcp", "list"], False),
    ("claude", ["doctor"], False),
    ("claude", ["update"], False),
    ("claude", ["--", "-p"], True),
    ("codex", [], True),
    ("codex", ["resume", "--last"], True),
    ("codex", ["exec", "do it"], False),
    ("codex", ["e", "do it"], False),
    ("codex", ["login"], False),
    ("codex", ["-V"], False),
    ("hermes", [], False),
])
def test_only_interactive_sessions_attach(monkeypatch, name, args, want):
    monkeypatch.setattr(os, "isatty", lambda fd: True)
    ok, why = shim_client.should_attach(name, args, environ={})
    assert ok is want, why


def test_opt_out_rook_terminal_and_non_ttys_never_attach(monkeypatch):
    monkeypatch.setattr(os, "isatty", lambda fd: True)
    assert shim_client.should_attach("claude", [], {"ROOK_SHIM": "0"})[0] is False
    assert shim_client.should_attach("claude", [], {"ROOK_SHIM": "off"})[0] is False
    assert shim_client.should_attach("claude", [], {"ROOK_WORK_TERMINAL": "t1"})[0] is False
    monkeypatch.setattr(os, "isatty", lambda fd: fd != 1)
    ok, why = shim_client.should_attach("claude", [], {})
    assert not ok and "fd 1" in why


def test_frames_parse_incrementally():
    f = shim_client.Frames()
    data = shim_client.jframe({"op": "x"}) + shim_client.frame(b"O", b"abc")
    got = []
    for i in range(len(data)):
        got += f.feed(data[i:i + 1])
    assert got == [(b"J", b'{"op":"x"}'), (b"O", b"abc")]
    with pytest.raises(ValueError):
        f.feed(b"O" + struct.pack(">I", shim_client.MAX_FRAME + 1))


# -- install / uninstall -----------------------------------------------------------------

def test_install_writes_shims_and_one_block_per_shell_and_uninstall_restores(home, monkeypatch):
    real = fake_agent(home / "bin")
    monkeypatch.setenv("PATH", f"{home / 'bin'}:/usr/bin:/bin")
    bashrc = home / ".bashrc"
    original = "export FOO=1\n# my settings"          # no trailing newline
    bashrc.write_text(original)
    (home / ".config" / "fish").mkdir(parents=True)
    zshrc = home / "dots" / "zshrc"
    zshrc.parent.mkdir()
    zshrc.write_text("setopt autocd\n")
    (home / ".zshrc").symlink_to(zshrc)       # a dotfile manager's symlink

    res = shim.install()
    assert res["agents"] == ["claude"] and res["real"]["claude"] == str(real)
    assert res["missing"] == []
    b = shim.bin_dir()
    assert (b / "claude").exists() and not (b / "codex").exists()
    assert (b / shim.MARKER_FILE).exists()
    assert os.stat(shim.root()).st_mode & 0o777 == 0o700
    assert os.stat(shim.client_path()).st_mode & 0o777 == 0o600
    assert shim.client_path().read_bytes() == shim.client_source()
    assert bashrc.read_text().count(shim.BEGIN) == 1
    assert bashrc.read_text().startswith(original + "\n\n" + shim.BEGIN)
    # The symlink is followed and kept.
    assert (home / ".zshrc").is_symlink() and shim.BEGIN in zshrc.read_text()
    fish = home / ".config" / "fish" / "conf.d" / shim.FISH_FILE
    assert "env.fish" in fish.read_text()

    # Installing again replaces the block instead of adding a second one.
    shim.install(agents=["claude", "codex"])
    assert bashrc.read_text().count(shim.BEGIN) == 1
    assert (b / "codex").exists()
    st = shim.status()
    assert st["installed"] and st["agents"] == ["claude", "codex"]
    assert all(r["present"] for r in st["rc"]) and st["client_current"]

    out = shim.uninstall()
    assert bashrc.read_text() == original + "\n"
    assert zshrc.read_text() == "setopt autocd\n" and (home / ".zshrc").is_symlink()
    assert not fish.exists() and not shim.root().exists()
    assert str(bashrc) in out["removed"]
    assert shim.status()["installed"] is False
    # Uninstalling twice is harmless.
    assert shim.uninstall()["removed"] == []


def test_install_needs_an_agent_and_a_shell(home, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ValueError, match="neither claude nor codex"):
        shim.install()
    with pytest.raises(ValueError, match="agents must be"):
        shim.install(agents=["bash"])
    with pytest.raises(ValueError, match="no shell configuration"):
        shim.install(agents=["claude"])
    res = shim.install(agents=["codex"], shells=["bash"])
    assert res["missing"] == ["codex"] and (home / ".bashrc").exists()
    # A file install created for its block alone goes away with it.
    shim.uninstall()
    assert not (home / ".bashrc").exists()


def test_env_snippets_put_the_shim_first(home, monkeypatch):
    fake_agent(home / "bin")
    monkeypatch.setenv("PATH", f"{home / 'bin'}:/usr/bin:/bin")
    shim.install(shells=["bash"])
    env_sh = shim.root() / "env.sh"
    out = subprocess.run(["/bin/sh", "-c", f". {env_sh}; . {env_sh}; echo $PATH"],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}).stdout.strip()
    assert out == f"{shim.bin_dir()}:/usr/bin:/bin"
    shim.uninstall()


def test_which_real_skips_shim_folders(home, monkeypatch):
    real = fake_agent(home / "bin")
    fake_agent(home / "other-shim")
    (home / "other-shim" / shim.MARKER_FILE).write_text("")
    path = f"{home / 'other-shim'}:{home / 'bin'}"
    assert shim.which_real("claude", path) == str(real)


def test_peer_uid_is_ours():
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert shim.peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


# -- the shell script falls through ---------------------------------------------------------

def run_script(script, args, env, timeout=10):
    return subprocess.run([str(script), *args], capture_output=True, text=True, env=env,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def test_script_runs_the_real_binary_when_not_a_terminal(home, monkeypatch):
    real_dir = home / "bin"
    fake = real_dir / "claude"
    real_dir.mkdir()
    fake.write_text("#!/bin/sh\necho \"REAL $0 [$*] rt=${ROOK_WORK_TERMINAL:-none}\"\nexit 5\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{real_dir}:/usr/bin:/bin")
    shim.install(shells=["bash"])
    env = {"PATH": f"{shim.bin_dir()}:{real_dir}:/usr/bin:/bin", "HOME": str(home)}
    r = run_script(shim.bin_dir() / "claude", ["-p", "a b"], env)
    assert r.returncode == 5 and r.stdout.strip() == f"REAL {fake} [-p a b] rt=none"
    # The shim folder twice on PATH, or another shim folder first: never itself.
    env["PATH"] = f"{shim.bin_dir()}:{shim.bin_dir()}:{real_dir}:/usr/bin:/bin"
    assert run_script(shim.bin_dir() / "claude", [], env).returncode == 5
    # No real binary at all: what the shell would say.
    env["PATH"] = f"{shim.bin_dir()}:/usr/bin:/bin"
    r = run_script(shim.bin_dir() / "claude", [], env)
    assert r.returncode == 127 and "command not found" in r.stderr
    shim.uninstall()


# -- end to end under a pty -----------------------------------------------------------------

class Outer:
    """A pseudo-terminal standing in for the person's terminal window."""

    def __init__(self, argv, env, cols=80, rows=24, cwd=None):
        import pty
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        def ctty():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                     cwd=cwd, start_new_session=True, preexec_fn=ctty)
        os.close(slave)
        os.set_blocking(self.master, False)
        self.out = b""

    def pump(self):
        while True:
            try:
                data = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                return
            if not data:
                return
            self.out += data

    async def expect(self, text, timeout=10.0):
        end = time.monotonic() + timeout
        while text.encode() not in self.out:
            self.pump()
            if time.monotonic() > end:
                raise AssertionError(f"{text!r} not in {self.out[-2000:]!r}")
            await asyncio.sleep(0.02)

    def type(self, text):
        os.write(self.master, text.encode())

    def resize(self, cols, rows):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def wait(self, timeout=10.0):
        end = time.monotonic() + timeout
        while self.proc.poll() is None:
            self.pump()
            if time.monotonic() > end:
                raise AssertionError("still running: " + repr(self.out[-2000:]))
            await asyncio.sleep(0.02)
        self.pump()
        return self.proc.returncode

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        try:
            os.close(self.master)
        except OSError:
            pass


@pytest_asyncio.fixture
async def rig(home, monkeypatch):
    real_dir = home / "bin"
    fake_agent(real_dir)
    fake_agent(real_dir, "codex")
    monkeypatch.setenv("PATH", f"{real_dir}:/usr/bin:/bin")
    monkeypatch.setenv("ROOK_WORK_TERM_DIR", str(home / "terms"))
    shim.install(shells=["bash"])
    plugin = TerminalsPlugin()
    server = shim.LocalTermServer(lambda: plugin)
    env = {"PATH": f"{shim.bin_dir()}:{real_dir}:/usr/bin:/bin", "HOME": str(home),
           "TERM": "xterm-256color", "LANG": "C.UTF-8"}
    outers = []

    def start(args=(), name="claude", **kw):
        o = Outer([str(shim.bin_dir() / name), *args], dict(env, **kw.pop("env", {})),
                  cwd=str(home), **kw)
        outers.append(o)
        return o

    class Rig:
        pass

    r = Rig()
    r.plugin, r.server, r.start, r.env, r.home = plugin, server, start, env, home
    yield r
    for o in outers:
        o.close()
    await server.stop()
    await plugin.stop()
    shim.uninstall()


def local_terms(plugin, running=True):
    return [t for t in plugin.terms.values() if t.link is not None and (t.running or not running)]


async def term_output(plugin, tid, text, timeout=10.0):
    end = time.monotonic() + timeout
    while True:
        r = await plugin.read(tid, 0, max_bytes=32768)
        raw = termwire.decode(r["enc"], r["data"])
        if text.encode() in raw:
            return raw, r
        if time.monotonic() > end:
            raise AssertionError(f"{text!r} not in {raw[-2000:]!r}")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_session_runs_in_a_local_rook_terminal(rig):
    await rig.server.start()
    assert os.stat(rig.server.path).st_mode & 0o777 == 0o600
    o = rig.start(["--model", "opus"], cols=90, rows=30)
    await o.expect("FAKE argv=[--model opus]")
    # Passthrough: the program sees a terminal of the person's size and a Rook terminal id.
    await o.expect("tty=True")
    assert b"size=90x30" in o.out
    terms = local_terms(rig.plugin)
    assert len(terms) == 1
    t = terms[0]
    assert f"rt={t.id}".encode() in o.out and t.harness == "claude" and t.cwd == str(rig.home)
    info = rig.plugin.list_terms()["terminals"][0]
    assert info["local"] is True and info["pid"]
    # The worker sees the same output (the Sessions page's tier 1)...
    await term_output(rig.plugin, t.id, "FAKE argv=")
    # ...and the person's typing goes in, echoed on both sides.
    o.type("hello\r")
    await o.expect("GOT hello")
    await term_output(rig.plugin, t.id, "GOT hello")
    # The page types too, and its output shows in the person's terminal.
    await rig.plugin.write(t.id, "from-web\r")
    await o.expect("GOT from-web")
    # The person's window owns the size: SIGWINCH reaches the program, the
    # worker learns the size, and a viewer's resize is ignored.
    o.resize(100, 33)
    await o.expect("WINCH size=100x33")
    for _ in range(100):
        if (t.cols, t.rows) == (100, 33):
            break
        await asyncio.sleep(0.02)
    assert (t.cols, t.rows) == (100, 33)
    r = rig.plugin.resize(t.id, 50, 10)
    assert r == {"ok": True, "id": t.id, "cols": 100, "rows": 33, "fixed": True}
    assert (await rig.plugin.read(t.id, 0))["fixed"] is True
    # Exit status propagates and the terminal ends.
    o.type("quit\r")
    assert await o.wait() == 7
    for _ in range(100):
        if not t.running:
            break
        await asyncio.sleep(0.02)
    assert not t.running and t.exit_code == 7


@pytest.mark.asyncio
async def test_falls_through_when_the_worker_is_down(rig):
    # No socket at all: the script runs the real binary without Python.
    o = rig.start(["x"])
    await o.expect("FAKE argv=[x] tty=True rt=-")
    o.type("quit\r")
    assert await o.wait() == 7
    # A stale socket nobody listens on: the client falls through too.
    path = shim.socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    s.close()
    o = rig.start(["y"], env={"ROOK_SHIM_DEBUG": "1"})
    await o.expect("FAKE argv=[y] tty=True rt=-")
    assert b"rook-shim: running claude directly" in o.out
    o.type("quit\r")
    assert await o.wait() == 7
    assert local_terms(rig.plugin, running=False) == []


@pytest.mark.asyncio
async def test_opt_out_and_refusal_fall_through(rig, monkeypatch):
    await rig.server.start()
    o = rig.start([], env={"ROOK_SHIM": "0"})
    await o.expect("rt=-")
    o.type("quit\r")
    assert await o.wait() == 7
    # The worker refuses (too many local terminals): the real binary runs.
    monkeypatch.setattr("rook.worker.plugins.terminals.MAX_LOCAL", 0)
    o = rig.start([])
    await o.expect("rt=-")
    o.type("quit\r")
    assert await o.wait() == 7


@pytest.mark.asyncio
async def test_stop_and_signal_from_the_page(rig):
    await rig.server.start()
    o = rig.start([], name="codex")
    await o.expect("FAKE argv=[]")
    (t,) = local_terms(rig.plugin)
    assert t.harness == "codex"
    rig.plugin.signal(t.id, "TERM")
    assert await o.wait() == -signal.SIGTERM      # the shim dies the way the program did
    o = rig.start([])
    await o.expect("FAKE argv=[]")
    (t,) = local_terms(rig.plugin)
    res = await rig.plugin.close(t.id)
    assert res["ok"]
    rc = await o.wait()
    assert rc == -signal.SIGHUP
    assert b"stopped from the Sessions page" in o.out


@pytest.mark.asyncio
async def test_session_survives_a_worker_restart(rig):
    await rig.server.start()
    o = rig.start([])
    await o.expect("FAKE argv=[]")
    (t1,) = local_terms(rig.plugin)
    o.type("one\r")
    await o.expect("GOT one")
    # The worker goes away: the session carries on in the person's terminal.
    await rig.server.stop()
    for _ in range(100):
        if not t1.running:
            break
        await asyncio.sleep(0.02)
    assert not t1.running
    o.type("two\r")
    await o.expect("GOT two")
    # It comes back: the shim registers again, replaying recent output.
    await rig.server.start()
    for _ in range(300):
        if local_terms(rig.plugin):
            break
        await asyncio.sleep(0.02)
    (t2,) = local_terms(rig.plugin)
    assert t2.id != t1.id
    await term_output(rig.plugin, t2.id, "GOT two")
    await rig.plugin.write(t2.id, "three\r")
    await o.expect("GOT three")
    o.type("quit\r")
    assert await o.wait() == 7


@pytest.mark.asyncio
async def test_plugin_stop_leaves_local_sessions_running(rig):
    await rig.server.start()
    o = rig.start([])
    await o.expect("FAKE argv=[]")
    await rig.plugin.stop()          # a worker shutting down
    await rig.server.stop()
    await asyncio.sleep(0.2)
    assert o.proc.poll() is None
    o.type("still\r")
    await o.expect("GOT still")
    o.type("quit\r")
    assert await o.wait() == 7


@pytest.mark.asyncio
async def test_server_refuses_another_uid(rig, monkeypatch):
    monkeypatch.setattr(shim, "peer_uid", lambda sock: os.getuid() + 1)
    await rig.server.start()
    o = rig.start([])
    await o.expect("rt=-")              # refused: the real binary ran directly
    o.type("quit\r")
    assert await o.wait() == 7


# -- the catalog and the hub ------------------------------------------------------------------

def test_record_marks_local_terminals():
    term = {"id": "t1", "harness": "claude", "running": True, "pid": 5, "local": True,
            "title": "claude in x", "cwd": "/x"}
    rec = record("claude", "sid", term=term, live=True)
    assert rec["local"] is True and rec["view"]["terminal"] == "t1" and rec["input"] == "pty"
    assert "local" not in record("claude", "sid", term=dict(term, local=None), live=True)


def test_hub_ignores_resizes_of_a_fixed_terminal():
    calls = []

    class Hub:
        def spawn(self, coro):
            calls.append(coro)
            coro.close()

    s = TermStream(Hub(), "w", "t")
    from rook.remote.term_hub import Viewer
    v = Viewer("me")
    s.viewers[v.id] = v
    s.holder = v.id
    s.fixed = True
    s.control(v, {"op": "resize", "cols": 100, "rows": 40})
    assert calls == [] and s.state()["fixed"] is True
    s.fixed = False
    s.control(v, {"op": "resize", "cols": 100, "rows": 40})
    assert len(calls) == 1


def test_session_shim_plugin_caps(home, monkeypatch):
    from rook.worker.plugins.session_shim import SessionShimPlugin
    p = SessionShimPlugin()
    assert sorted(p.caps()) == ["sessions.shim.install", "sessions.shim.status",
                                "sessions.shim.uninstall"]
    assert p.available()

    async def go():
        assert (await p.install())["ok"] is False      # no terminals plugin
        term = TerminalsPlugin()

        class W:
            plugins = [p, term]

        p.bind_worker(W())
        fake_agent(home / "bin")
        monkeypatch.setenv("PATH", f"{home / 'bin'}:/usr/bin:/bin")
        res = await p.install(shells=["bash"])
        assert res["ok"] and res["agents"] == ["claude"]
        st = await p.status()
        assert st["installed"] and st["listening"] and st["local_terminals"] == 0
        assert shim.socket_path().is_socket()
        await p.stop()
        await p.start()                                  # a restart listens again
        assert (await p.status())["listening"]
        out = await p.uninstall()
        assert out["ok"] and not shim.root().exists()
        assert not (await p.status())["listening"]
        await p.start()                                  # not installed: stays quiet
        assert not (await p.status())["listening"]

    asyncio.run(go())
