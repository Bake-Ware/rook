"""shell through the worker host: exec, which, env, and shell.open streams
(PTY and pipes) on a real Streams store. POSIX only (real sh, real PTYs)."""

import asyncio
import os
import sys
import time

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker import security
from rook.core2.worker.host import CallError, Host
from rook.streams import Streams

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells and PTYs")


def run(cap, args=None, lease=None):
    async def body():
        host = Host(os_name="linux")
        await host.load(PLUGINS / "shell")
        try:
            return await host.call(f"shell.{cap}", args or {}, ME, lease=lease)
        finally:
            await host.close()
    return go(body)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


# -- exec -------------------------------------------------------------------------

def test_exec_argv_takes_arguments_as_data():
    r = run("exec", {"argv": ["printf", "%s|", "a b", "$(reboot)", "-x"]})
    assert r == {"ok": True, "code": 0, "stdout": "a b|$(reboot)|-x|", "stderr": ""}


def test_exec_cmd_stdin_env_cwd(tmp_path):
    r = run("exec", {"cmd": "cat | tr a-z A-Z; echo $ROOK_T; pwd >&2; exit 4",
                     "stdin": "hello\n", "env": {"ROOK_T": "set"}, "cwd": str(tmp_path)})
    assert r["code"] == 4 and r["ok"] is False
    assert r["stdout"] == "HELLO\nset\n"
    assert r["stderr"].strip() == str(tmp_path)
    assert "shell" not in r          # sh on Linux isn't named
    assert run("exec", {"cmd": "echo $0", "shell": "bash"})["shell"] == "bash"


def test_exec_bad_args():
    with pytest.raises(CallError, match="give argv or cmd, not both"):
        run("exec", {"argv": ["true"], "cmd": "true"})
    with pytest.raises(CallError, match="give argv or cmd"):
        run("exec", {})
    with pytest.raises(CallError, match="shell applies to cmd"):
        run("exec", {"argv": ["true"], "shell": "bash"})
    with pytest.raises(CallError, match="only exists on Windows"):
        run("exec", {"cmd": "dir", "shell": "cmd"})
    with pytest.raises(CallError, match="not found: no-such-program-xyz"):
        run("exec", {"argv": ["no-such-program-xyz"]})
    with pytest.raises(CallError, match="bad args"):
        run("exec", {"argv": ["true"], "timeout": 0})


def test_exec_timeout_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    with pytest.raises(CallError, match="timed out after 0.5s and was killed"):
        run("exec", {"cmd": f"sleep 30 & echo $! > {pidfile}; wait", "timeout": 0.5})
    assert time.monotonic() - started < 10
    pid = int(pidfile.read_text())
    for _ in range(50):
        if _gone(pid):
            break
        time.sleep(0.05)
    assert _gone(pid), "the background child outlived the timeout"


def test_exec_flags_access_denied_but_never_reruns(tmp_path):
    marker = tmp_path / "ran"
    r = run("exec", {"cmd": f"echo x >> {marker}; echo 'touch: Permission denied' >&2; exit 1"})
    assert r["denied"] is True and r["code"] == 1
    assert marker.read_text() == "x\n"       # ran once
    assert "denied" not in run("exec", {"cmd": "exit 1"})


def test_every_process_goes_through_launch(monkeypatch):
    seen = []
    real = security.launch

    async def spy(argv, lease=None, **kw):
        seen.append((list(argv), lease))
        return await real(argv, None, **kw)   # not elevated for real in a test

    monkeypatch.setattr(security, "launch", spy)
    lease = {"id": "0" * 32}
    run("exec", {"argv": ["true"]}, lease=lease)
    run("exec", {"cmd": "true"})
    assert seen == [(["true"], lease), (["/bin/sh", "-c", "true"], None)]


def test_exec_sudo_when_leased(monkeypatch):
    # The real plan for an elevated call on Linux, as a normal user: sudo -n.
    monkeypatch.setattr(security, "_is_admin", lambda platform: False)
    argv, extra = security.launch_plan(["id"], {"id": "x"}, platform="linux")
    assert argv == ["sudo", "-n", "--", "id"] and extra == {}


# -- which, env -------------------------------------------------------------------

def test_which():
    assert run("which", {"name": "sh"}).endswith("/sh")
    assert run("which", {"name": "no-such-program-xyz"}) is None
    with pytest.raises(CallError, match="bad args"):
        run("which", {"name": "/bin/sh"})


def test_env_masks_credentials(monkeypatch):
    monkeypatch.setenv("ROOKTEST_PLAIN", "visible")
    monkeypatch.setenv("ROOKTEST_API_TOKEN", "hunter2")
    assert run("env", {"name": "ROOKTEST_PLAIN"}) == "visible"
    assert run("env", {"name": "ROOKTEST_API_TOKEN"}) == "***"
    assert run("env", {"name": "ROOKTEST_MISSING", "default": "d"}) == "d"
    assert run("env", {"prefix": "ROOKTEST_"}) == {"ROOKTEST_API_TOKEN": "***", "ROOKTEST_PLAIN": "visible"}
    with pytest.raises(CallError, match="give name or prefix"):
        run("env", {})


def test_env_old_name():
    async def body():
        host = Host(os_name="linux")
        await host.load(PLUGINS / "shell")
        try:
            return await host.call("shell.env.get", {"name": "PATH"}, ME)
        finally:
            await host.close()
    assert go(body) == os.environ["PATH"]


# -- open: terminal and process streams --------------------------------------------

async def _open(args, streams=None):
    streams = streams or Streams(host="w1")
    host = Host(os_name="linux", streams=streams)
    await host.load(PLUGINS / "shell")
    res = await host.call("shell.open", args, ME)
    return host, streams, res["stream"]


async def _until(streams, sid, want: bytes, timeout=10) -> bytes:
    out, after = b"", 0
    deadline = asyncio.get_running_loop().time() + timeout
    while want not in out:
        left = deadline - asyncio.get_running_loop().time()
        assert left > 0, f"never saw {want!r} in {out!r}"
        got = await streams.read(sid, after, wait=min(left, 1))
        after = got["next"]
        out += b"".join(e.body for e in got["events"] if e.type == "bytes")
        if got["closed"] and want not in out:
            raise AssertionError(f"closed before {want!r}: {out!r}")
    return out


async def _closed(streams, sid, timeout=10):
    async def wait():
        async for ev in streams.follow(sid):
            if ev.type == "status" and ev.body.get("state") == "closed":
                return ev.body
    return await asyncio.wait_for(wait(), timeout)


def test_open_pty_takes_keystrokes_and_reports_the_exit_code():
    async def body():
        host, streams, sid = await _open({"cmd": "read x; echo got:$x; tty; exit 3", "cols": 100})
        try:
            meta = streams.get(sid)
            assert meta.kind == "terminal" and meta.attrs["pty"] is True and meta.attrs["pid"] > 0
            assert meta.participants == ["bake", "claude"]
            streams.append(sid, "input", {"text": "hello\n"}, by="claude")
            out = await _until(streams, sid, b"got:hello")
            assert b"/dev/pts/" in out + await _until(streams, sid, b"/dev/")
            status = await _closed(streams, sid)
            assert status["exit"] == 3
        finally:
            await host.close()
    go(body)


def test_open_pty_resize_and_ctrl_c():
    async def body():
        host, streams, sid = await _open({"argv": ["sh"], "cols": 80, "rows": 24})
        try:
            streams.append(sid, "input", {"resize": [101, 41]})
            streams.append(sid, "input", {"text": "stty size\n"})
            await _until(streams, sid, b"41 101")
            # A typed Ctrl-C reaches the foreground job through the terminal.
            streams.append(sid, "input", {"text": "sleep 30; echo after\n"})
            await asyncio.sleep(0.3)
            streams.append(sid, "input", {"text": "\x03"})
            streams.append(sid, "input", {"text": "echo back\n"})
            await _until(streams, sid, b"back")
            streams.append(sid, "input", {"text": "exit 0\n"})
            assert (await _closed(streams, sid))["exit"] == 0
        finally:
            await host.close()
    go(body)


def test_open_pipes_is_a_process_stream_and_takes_signals():
    async def body():
        host, streams, sid = await _open({"argv": ["cat"], "pty": False, "title": "cat"})
        try:
            meta = streams.get(sid)
            assert meta.kind == "process" and meta.title == "cat"
            streams.append(sid, "input", {"text": "echoed\n"})
            await _until(streams, sid, b"echoed\n")
            streams.append(sid, "input", {"signal": "TERM"})
            assert (await _closed(streams, sid))["exit"] == -15
        finally:
            await host.close()
    go(body)


def test_open_failure_closes_the_stream():
    async def body():
        host, streams, _ = await _open({"argv": ["true"], "pty": False})
        with pytest.raises(CallError, match="not found"):
            await host.call("shell.open", {"argv": ["no-such-program-xyz"]}, ME)
        failed = [m for m in streams.list() if m.attrs.get("argv") == ["no-such-program-xyz"]]
        assert failed and failed[0].closed
        await host.close()
    go(body)


def test_open_needs_streams():
    with pytest.raises(CallError, match="streams are not available here"):
        run("open", {"argv": ["true"]})


def test_unloading_ends_open_terminals():
    async def body():
        host, streams, sid = await _open({"cmd": "echo up; sleep 60"})
        await _until(streams, sid, b"up")
        pid = streams.get(sid).attrs["pid"]
        await host.close()
        assert streams.get(sid).closed
        for _ in range(50):
            if _gone(pid):
                break
            await asyncio.sleep(0.05)
        assert _gone(pid)
    go(body)


def test_exec_timeout_kills_a_command_that_ignores_term():
    started = time.monotonic()
    with pytest.raises(CallError, match="timed out after 0.3s"):
        run("exec", {"cmd": "trap '' TERM; sleep 30", "timeout": 0.3})
    assert time.monotonic() - started < 8      # TERM, a 2 s grace, then KILL


def test_input_to_a_child_that_never_reads_does_not_stall_the_worker():
    # A blocked loop can't run go()'s timeout: SIGALRM is the hard stop.
    import signal

    def hung(*_):
        raise TimeoutError("the event loop was blocked writing to the PTY")

    old = signal.signal(signal.SIGALRM, hung)
    signal.alarm(15)
    try:
        _never_reads()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _never_reads():
    async def body():
        host, streams, sid = await _open({"cmd": "stty raw -echo; sleep 30"})
        try:
            for _ in range(4):                  # far more than a PTY buffer holds
                streams.append(sid, "input", {"text": "x" * 200_000})
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            for _ in range(5):
                await asyncio.sleep(0.05)
            assert loop.time() - t0 < 1.0, "the event loop was blocked"
        finally:
            await host.close()
        assert streams.get(sid).closed
    go(body)
