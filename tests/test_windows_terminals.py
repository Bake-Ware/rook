"""Windows terminals (ConPTY) and the Windows Claude inbox, tested on Linux.

The Win32 side is faked at the ctypes boundary: ``FakeKernel32`` receives the
same calls, structs and byref() arguments the real kernel32 would, and
emulates a pseudoconsole with OS pipes and a real child process, so
:mod:`rook.worker.conpty` and the terminals plugin's Windows branch run end
to end. What only a Windows box can prove (real ConPTY rendering, Ctrl-C,
job teardown, the named-pipe inbox) is in
``tests/integration/windows_conpty_check.py``.
"""
import asyncio
import ctypes
import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess

import pytest
import pytest_asyncio

from rook.core import facts as facts_mod
from rook.worker import conpty, session_messages as messages, termwire, winsec
from rook.worker.plugins import terminals
from rook.worker.plugins.terminals import TerminalsPlugin, build_argv, windows_command

pytestmark = pytest.mark.skipif(os.name != "posix", reason="emulates ConPTY with POSIX pipes")

SID = "8f8a3c39-3d9f-4d34-8f1b-2b9a1f0e7c11"


class FakeKernel32:
    """kernel32 as conpty.py calls it, backed by pipes and a subprocess."""

    def __init__(self):
        self.calls = []
        self.handles = {}           # handle -> ("r"|"w", fd) | ("pc",) | ("proc", Popen) | ...
        self.next = 0x100
        self.input = bytearray()    # every byte written to the console
        self.fail_create = False

    def _new(self, obj):
        self.next += 4
        self.handles[self.next] = obj
        return self.next

    def CreatePipe(self, r, w, sa, size):
        rfd, wfd = os.pipe()
        r._obj.value, w._obj.value = self._new(("r", rfd)), self._new(("w", wfd))
        self.calls.append(("CreatePipe",))
        return 1

    def CreatePseudoConsole(self, size, h_in, h_out, flags, out):
        # The console keeps its own duplicates of the two ends.
        pc = {"in": os.dup(self.handles[h_in][1]), "out": os.dup(self.handles[h_out][1]), "proc": None}
        out._obj.value = self._new(("pc", pc))
        self.calls.append(("CreatePseudoConsole", size.X, size.Y, flags))
        return 0

    def ResizePseudoConsole(self, hpc, size):
        self.calls.append(("ResizePseudoConsole", size.X, size.Y))
        return 0

    def ClosePseudoConsole(self, hpc):
        self.calls.append(("ClosePseudoConsole",))
        pc = self.handles[hpc][1]
        proc = pc["proc"]
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGHUP)       # CTRL_CLOSE_EVENT
        for k in ("in", "out"):
            if pc[k] is not None:
                os.close(pc[k])
                pc[k] = None

    def InitializeProcThreadAttributeList(self, buf, count, flags, size):
        if buf is None:
            size._obj.value = 48
            return 0
        self.calls.append(("InitializeProcThreadAttributeList", count))
        return 1

    def UpdateProcThreadAttribute(self, buf, flags, attr, value, size, prev, ret):
        self.calls.append(("UpdateProcThreadAttribute", attr, value.value, size))
        return 1

    def DeleteProcThreadAttributeList(self, buf):
        self.calls.append(("DeleteProcThreadAttributeList",))

    def CreateProcessW(self, app, cmd, pa, ta, inherit, flags, env, cwd, si, pi):
        si = si._obj
        block = env[:]
        environ = dict(e.split("=", 1) for e in block.split("\0") if e)
        self.calls.append(("CreateProcessW", cmd.value, flags, inherit, si.StartupInfo.cb,
                           si.StartupInfo.dwFlags, bool(si.lpAttributeList)))
        if self.fail_create:
            return 0
        pc = next(o[1] for o in self.handles.values() if o[0] == "pc")
        proc = subprocess.Popen(shlex.split(cmd.value), stdin=pc["in"], stdout=pc["out"],
                                stderr=subprocess.STDOUT, cwd=cwd, env=environ, start_new_session=True)
        pc["proc"] = proc
        pi._obj.hProcess = self._new(("proc", proc))
        pi._obj.hThread = self._new(("thread",))
        pi._obj.dwProcessId = proc.pid
        return 1

    def CreateJobObjectW(self, sa, name):
        return self._new(("job", []))

    def SetInformationJobObject(self, job, cls, info, size):
        self.calls.append(("SetInformationJobObject", cls, info._obj.BasicLimitInformation.LimitFlags))
        return 1

    def AssignProcessToJobObject(self, job, proc):
        self.handles[job][1].append(self.handles[proc][1])
        self.calls.append(("AssignProcessToJobObject",))
        return 1

    def TerminateJobObject(self, job, code):
        self.calls.append(("TerminateJobObject", code))
        for proc in self.handles[job][1]:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
        return 1

    def TerminateProcess(self, proc, code):
        self.calls.append(("TerminateProcess", code))
        return 1

    def ResumeThread(self, thread):
        self.calls.append(("ResumeThread",))
        return 1

    def ReadFile(self, h, buf, n, nread, overlapped):
        try:
            data = os.read(self.handles[h][1], n)
        except OSError:
            data = b""
        if not data:
            return 0                                  # ERROR_BROKEN_PIPE
        ctypes.memmove(buf, data, len(data))
        nread._obj.value = len(data)
        return 1

    def WriteFile(self, h, data, n, written, overlapped):
        data = bytes(data[:n])
        self.input.extend(data)
        written._obj.value = os.write(self.handles[h][1], data)
        return 1

    def WaitForSingleObject(self, h, ms):
        proc = self.handles[h][1]
        try:
            proc.wait(None if ms == conpty.INFINITE else ms / 1000)
            return 0
        except subprocess.TimeoutExpired:
            return 0x102

    def GetExitCodeProcess(self, h, code):
        rc = self.handles[h][1].returncode
        code._obj.value = 1 if rc is None or rc < 0 else rc
        return 1

    def CloseHandle(self, h):
        obj = self.handles.pop(h, None)
        self.calls.append(("CloseHandle", obj[0] if obj else None))
        if obj and obj[0] in ("r", "w"):
            os.close(obj[1])
        return 1

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def k32(monkeypatch):
    fake = FakeKernel32()
    monkeypatch.setattr(conpty, "kernel32", lambda: fake)
    yield fake
    for obj in fake.handles.values():
        if obj[0] == "proc" and obj[1].poll() is None:
            os.killpg(obj[1].pid, signal.SIGKILL)
            obj[1].wait()


# -- conpty.py at the ctypes boundary ----------------------------------------------

def test_spawn_wires_pseudoconsole_job_and_suspended_start(k32, tmp_path):
    out = bytearray()
    pty = conpty.ConPty.spawn("sh -c 'echo hi; read x; echo got-$x; exit 3'", str(tmp_path),
                              {"PATH": os.environ["PATH"], "TERM": "xterm-256color"}, cols=90, rows=20)
    pty.start_reader(out.extend)
    pty.write(b"abc\n")
    assert pty.wait(10) == 3
    pty.close()
    assert b"hi" in out and b"got-abc" in out

    names = k32.names()
    assert ("CreatePseudoConsole", 90, 20, 0) in k32.calls
    upd = next(c for c in k32.calls if c[0] == "UpdateProcThreadAttribute")
    assert upd[1] == conpty.PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE and upd[2] is not None
    create = next(c for c in k32.calls if c[0] == "CreateProcessW")
    flags = create[2]
    assert flags & conpty.EXTENDED_STARTUPINFO_PRESENT and flags & conpty.CREATE_SUSPENDED
    assert flags & conpty.CREATE_UNICODE_ENVIRONMENT and create[3] is False
    assert create[4] == ctypes.sizeof(conpty.STARTUPINFOEXW) and create[5] == conpty.STARTF_USESTDHANDLES
    assert create[6]
    assert ("SetInformationJobObject", conpty.JobObjectExtendedLimitInformation,
            conpty.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) in k32.calls
    # Into the job before it runs; the console's pipe ends closed right after creation.
    assert names.index("AssignProcessToJobObject") < names.index("ResumeThread")
    first_pc = names.index("CreatePseudoConsole")
    assert names[first_pc + 1:first_pc + 3] == ["CloseHandle", "CloseHandle"]
    assert names.count("ClosePseudoConsole") == 1 and "DeleteProcThreadAttributeList" in names
    assert not k32.handles or all(o[0] == "pc" for o in k32.handles.values())


def test_failed_create_releases_everything(k32, tmp_path):
    k32.fail_create = True
    with pytest.raises(OSError):
        conpty.ConPty.spawn("nothing", str(tmp_path), {})
    assert "ClosePseudoConsole" in k32.names()
    assert all(o[0] == "pc" for o in k32.handles.values())


def test_env_block_and_command_line():
    block = conpty.env_block({"b": "2", "A": "1", "Path": "C:\\x", "BAD\0": "x", "=C:": "C:\\"})
    # "=C:" (per-drive cwd) is a legal Windows entry; NULs never are.
    assert block == "=C:=C:\\\0A=1\0b=2\0Path=C:\\x\0\0"
    assert conpty.cmdline(["C:\\Program Files\\x.exe", "a b", 'q"t']) == '"C:\\Program Files\\x.exe" "a b" q\\"t'
    with pytest.raises(ValueError):
        conpty.cmdline(["x", "y" * 40000])


def test_conpty_unavailable_off_windows():
    assert conpty.available() is False
    assert facts_mod._has_conpty() is False
    assert facts_mod.detect_facts()["pty"] is True


# -- harness launch on Windows -------------------------------------------------------

def test_windows_shell_and_shim_resolution(tmp_path, monkeypatch):
    assert build_argv("shell", "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe") == \
        ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoLogo"]
    assert build_argv("shell", "C:\\Program Files\\PowerShell\\7\\pwsh.exe")[1:] == ["-NoLogo"]
    assert build_argv("shell", "C:\\Windows\\system32\\cmd.exe") == ["C:\\Windows\\system32\\cmd.exe"]
    monkeypatch.setattr(terminals, "_IS_WIN", True)
    found = {"powershell.exe": "C:\\ps\\powershell.exe", "codex": "C:\\npm\\codex.CMD"}
    monkeypatch.setattr(terminals.shutil, "which", lambda name: found.get(name))
    assert terminals._binary("shell") == "C:\\ps\\powershell.exe"
    assert terminals._binary("codex") == "C:\\npm\\codex.CMD"

    # An npm cmd-shim runs its real target, so cmd.exe never parses our args.
    npm = tmp_path / "npm"
    script = npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("//")
    (npm / "node.exe").write_text("")
    shim = npm / "codex.cmd"
    shim.write_text('@ECHO off\r\nGOTO start\r\n:start\r\nendLocal & goto #_undefined_# 2>NUL || '
                    'title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
    persona = 'Be "careful" & 100% <sure>'
    line = windows_command([str(shim), "-c", f"developer_instructions={json.dumps(persona)}"])
    assert line.startswith(f"{npm / 'node.exe'} {script} -c ")
    assert "cmd" not in line.split()[0].lower()

    # Not a recognisable shim: cmd.exe only for arguments it cannot misread.
    plain = tmp_path / "tool.bat"
    plain.write_text("@echo off\r\nsomething %*\r\n")
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\system32\\cmd.exe")
    assert windows_command([str(plain), "--model", "m1"]) == \
        f'"C:\\Windows\\system32\\cmd.exe" /d /s /c "{plain} --model m1"'
    with pytest.raises(ValueError):
        windows_command([str(plain), "--append-system-prompt", persona])
    assert windows_command(["C:\\bin\\claude.exe", "--resume", "abc"]) == "C:\\bin\\claude.exe --resume abc"


# -- the terminals plugin on the Windows backend --------------------------------------

@pytest_asyncio.fixture
async def winplugin(monkeypatch, tmp_path, k32):
    monkeypatch.setattr(terminals, "_IS_WIN", True)
    monkeypatch.setattr(terminals, "_binary", lambda h: "/bin/sh")
    monkeypatch.setenv("ROOK_WORK_TERM_DIR", str(tmp_path / "terms"))
    p = TerminalsPlugin()
    yield p
    await p.stop()


async def follow(p, tid, text, cursor=0, timeout=10):
    out = b""
    async with asyncio.timeout(timeout):
        while text.encode() not in out:
            r = await p.read(tid, cursor, wait=2)
            out += termwire.decode(r["enc"], r["data"])
            cursor = r["next"]
            if r["eof"]:
                break
    return out, cursor


@pytest.mark.asyncio
async def test_plugin_streams_writes_resizes_and_signals_through_conpty(winplugin, k32, tmp_path):
    r = await winplugin.open(harness="shell", cwd=str(tmp_path), cols=80, rows=24)
    tid = r["id"]
    t = winplugin.terms[tid]
    assert t.conpty is not None and r["pid"] == t.pid
    await winplugin.write(tid, "echo ready-$((6*7)) $TERM\n")
    out, cur = await follow(winplugin, tid, "ready-42")
    assert b"ready-42 xterm-256color" in out
    winplugin.resize(tid, 100, 30)
    assert ("ResizePseudoConsole", 100, 30) in k32.calls
    assert winplugin.signal(tid, "INT")["sent"] == "SIGINT"
    assert k32.input.endswith(b"\x03")                # Ctrl-C is a keystroke on Windows
    winplugin.signal(tid, "KILL")
    async with asyncio.timeout(10):
        while t.running:
            await asyncio.sleep(0.02)
    assert ("TerminateJobObject", 1) in k32.calls and t.exit_code == 1
    last = await winplugin.read(tid, t.total)
    assert last["eof"]
    with pytest.raises(ValueError):
        await winplugin.write(tid, "late\n")


@pytest.mark.asyncio
async def test_plugin_exit_drains_output_and_close_hangs_up(winplugin, k32, tmp_path):
    tid = (await winplugin.open(harness="shell", cwd=str(tmp_path)))["id"]
    t = winplugin.terms[tid]
    await winplugin.write(tid, "i=0; while [ $i -lt 500 ]; do echo line-$i; i=$((i+1)); done; exit 4\n")
    async with asyncio.timeout(10):
        while t.running:
            await asyncio.sleep(0.02)
    assert t.exit_code == 4 and b"line-499" in bytes(t.buf)
    assert k32.names().count("ClosePseudoConsole") == 1

    tid2 = (await winplugin.open(harness="shell", cwd=str(tmp_path)))["id"]
    t2 = winplugin.terms[tid2]
    closed = await winplugin.close(tid2)
    assert closed["ok"] and not t2.running and tid2 not in winplugin.terms
    assert k32.names().count("ClosePseudoConsole") == 2


@pytest.mark.asyncio
async def test_plugin_writes_private_mcp_config_before_any_secret(winplugin, tmp_path, monkeypatch):
    monkeypatch.setattr(winsec, "_IS_WIN", True)
    restricted = []
    monkeypatch.setattr(winsec, "restrict_to_owner",
                        lambda path, directory=False: restricted.append((str(path), directory, os.path.getsize(path))))
    r = await winplugin.open(harness="claude", cwd=str(tmp_path), mcp_url="https://hub.example.com/mcp",
                             mcp_token="tok-123")
    t = winplugin.terms[r["id"]]
    cfg = t.files[0]
    assert restricted[0][:2] == (str(tmp_path / "terms"), True)
    assert (cfg, False, 0) in restricted                  # ACL set while the file was still empty
    assert "tok-123" in open(cfg).read() and stat.S_IMODE(os.stat(cfg).st_mode) == 0o600
    await winplugin.close(r["id"])
    assert not os.path.exists(cfg)


# -- Claude inbox on Windows ----------------------------------------------------------

ME = "S-1-5-21-1000-2000-3000-1001"
PIPE = "\\\\.\\pipe\\LOCAL\\cc-msg-0123456789abcdef0123456789abcdef"
FT = "134300000000000000"


@pytest.fixture
def wininbox(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    (home / "sessions").mkdir(parents=True)
    pid = 4321
    marker = home / "sessions" / f"{pid}.json"
    data = {"pid": pid, "sessionId": SID, "procStart": FT, "peerProtocol": 1,
            "pidDomain": "win32:host", "messagingSocketPath": PIPE, "kind": "interactive"}
    marker.write_text(json.dumps(data))
    key = home / "sessions" / f"{pid}.{hashlib.sha256(PIPE.lower().encode()).hexdigest()}.key"
    key.write_text(json.dumps({"peerToken": "b" * 32, "procStartFt": FT, "pidDomain": "win32:host"}))
    state = {"public": set(), "proc": {"image": "claude.exe", "created": int(FT), "alive": True, "sid": ME}}
    monkeypatch.setattr(messages, "_IS_WIN", True)
    monkeypatch.setattr(winsec, "current_user_sid", lambda: ME)
    monkeypatch.setattr(winsec, "is_private", lambda path, me=None: str(path) not in state["public"])
    monkeypatch.setattr(winsec, "process_info", lambda p: dict(state["proc"]) if p == pid else None)
    return home, marker, key, data, state


def test_windows_claude_endpoint_matches_only_a_live_private_session(wininbox):
    home, marker, key, data, state = wininbox
    assert messages.claude_endpoint(SID, home) == (PIPE, "b" * 32, 4321)

    def refused(**proc):
        state["proc"].update(proc)
        try:
            return messages.claude_endpoint(SID, home) is None
        finally:
            state["proc"].update(image="claude.exe", created=int(FT), alive=True, sid=ME)
    assert refused(created=int(FT) + 1)            # pid reused by a newer process
    assert refused(image="node.exe")
    assert refused(sid="S-1-5-21-9")               # someone else's Claude
    assert refused(alive=False)
    for public in (key, marker):
        state["public"] = {str(public)}
        assert messages.claude_endpoint(SID, home) is None
        state["public"] = set()
    for bad in ("\\\\server\\pipe\\cc-msg-x", "\\\\.\\pipe\\..\\C:\\x", "/tmp/cc.sock",
                "\\\\.\\pipe\\LOCAL\\..\\x"):
        marker.write_text(json.dumps({**data, "messagingSocketPath": bad}))
        assert messages.claude_endpoint(SID, home) is None
    marker.write_text(json.dumps({**data, "pidDomain": "win32:other"}))
    assert messages.claude_endpoint(SID, home) is None
    marker.write_text(json.dumps(data))
    key.write_text(json.dumps({"peerToken": "b" * 32, "procStartFt": "1", "pidDomain": "win32:host"}))
    assert messages.claude_endpoint(SID, home) is None
    key.write_text(json.dumps({"peerToken": "../x", "procStartFt": FT}))
    assert messages.claude_endpoint(SID, home) is None


@pytest.mark.asyncio
async def test_windows_delivery_checks_the_pipe_server(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_WORK_DB", str(tmp_path / "worker.sqlite3"))
    monkeypatch.setattr(messages, "_IS_WIN", True)
    monkeypatch.setattr(messages, "claude_endpoint", lambda sid: (PIPE, "c" * 32, 4242))
    server = {"pid": 4242, "written": b"", "closed": 0, "opened": []}
    monkeypatch.setattr(winsec, "open_pipe", lambda path: server["opened"].append(path) or 77)
    monkeypatch.setattr(winsec, "pipe_server_pid", lambda h: server["pid"])
    monkeypatch.setattr(winsec, "write_all", lambda h, data: server.update(written=server["written"] + data))
    monkeypatch.setattr(winsec, "close_handle", lambda h: server.update(closed=server["closed"] + 1))
    result = await messages.deliver("claude", SID, "command-123", "hello\n💌")
    assert result["ok"] and result["delivery"] == "forwarded" and server["opened"] == [PIPE]
    frames = [json.loads(line) for line in server["written"].decode().splitlines()]
    assert frames[0] == {"type": "auth", "token": "c" * 32}
    assert frames[1]["session_id"] == SID and frames[1]["message"]["content"] == "hello\n💌"
    assert frames[1]["uuid"] == "command-123" and server["closed"] == 1

    server.update(pid=999, written=b"")
    result = await messages.deliver("claude", SID, "command-456", "again")
    assert not result["ok"] and "owner changed" in result["error"]
    assert server["written"] == b"" and server["closed"] == 2


def test_codex_control_socket_is_off_on_windows(monkeypatch, tmp_path):
    from rook.worker import codex_input
    monkeypatch.setattr(codex_input.sys, "platform", "win32")
    assert codex_input.control_socket() is None
