"""Core's own caps as plugins installed by default (PL7): `net` (the network primitives, D3)
and `shell` (docs/inventory.md CAP-01, CAP-03, WRK4-070). A real hub, served, and a real
rook_worker over its real carrier (Telesthete over loopback WebSocket)."""

import asyncio
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import time

from rook_hub import Hub
from rook_hub.plugins import DEFAULTS
from rook_worker import Worker

SHELL_CAPS = ["shell.exec", "shell.open", "shell.which"]
NET_CAPS = ["net.http", "net.tcp", "net.udp"]


async def until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


def default_module(name):
    """A default plugin's worker part, run as a worker runs it, for its CAPS."""
    namespace = {}
    exec(compile((DEFAULTS / name / "worker.py").read_text(), name, "exec"), namespace)
    return namespace["CAPS"]


class Rig:
    """A served hub on a new data directory and one real worker, approved, over the real carrier."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    async def __aenter__(self):
        self.hub = Hub(self.tmp_path / "hub")
        host, port = await self.hub.serve("127.0.0.1", 0)
        self.alice = self.hub.auth.create_user("Alice", "a password")["guid"]
        self.worker = Worker(self.tmp_path / "box", f"http://{host}:{port}", name="box", macs=[], knock_every=0.02)
        self.task = asyncio.create_task(self.worker.run())
        await until(lambda: any(p["key"] == self.worker.key for p in self.hub.list_pending()))
        self.guid = self.hub.approve(self.worker.key, user=self.alice)["guid"]
        await until(lambda: set(SHELL_CAPS + NET_CAPS) <= set(self.announced()))
        return self

    def announced(self):
        return [c["name"] for c in self.hub.get_worker(self.guid)["caps"] or []]

    async def call(self, cap, **args):
        return await self.hub.call(self.guid, cap, args, "a test of the default plugins", user=self.alice)

    async def ok(self, cap, **args):
        reply = await self.call(cap, **args)
        assert reply["type"] == "result", reply
        return reply["result"]

    async def refused(self, cap, **args):
        reply = await self.call(cap, **args)
        assert reply["type"] == "error" and reply["message"] and reply["next"], reply
        return reply

    async def output(self, stream, until_text, timeout=10.0):
        """Everything the stream has said, read until `until_text` is in it."""
        text, position, deadline = "", 0, time.monotonic() + timeout
        while until_text not in text:
            assert time.monotonic() < deadline, f"timed out waiting for {until_text!r}; got {text!r}"
            read = await self.hub.streams.read(stream, position, wait=1)
            position = read["next"]
            text += "".join(e.get("out", "") for e in read["entries"])
        return text

    async def __aexit__(self, *exc):
        self.worker.stop()
        await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 5)
        await self.hub.stop_serving()
        self.hub.close()


# ------------------------------------------------------------------ installed by default

def test_a_new_hub_has_the_default_plugins_and_a_restart_keeps_them(tmp_path):
    hub = Hub(tmp_path / "hub")
    listed = hub.plugins.list()
    assert [p["name"] for p in listed] == ["net", "shell"]
    assert all(p["worker"] and p["caps"] == [] and not p["site"] for p in listed)   # worker parts only
    first = {p["name"]: p["guid"] for p in listed}
    hub.close()

    again = Hub(tmp_path / "hub")                       # present: loaded as installed, nothing reinstalled
    assert {p["name"]: p["guid"] for p in again.plugins.list()} == first
    assert len(list((tmp_path / "hub" / "plugins").iterdir())) == 2
    again.close()


def test_a_plugin_a_person_installed_in_a_defaults_place_is_kept(tmp_path):
    replacement = tmp_path / "my-shell"
    replacement.mkdir()
    (replacement / "plugin.json").write_text(json.dumps({"name": "shell", "version": "9.0.0"}))
    hub = Hub(tmp_path / "hub")
    hub.plugins.install(replacement, {"user": hub.auth.create_user("Alice", "a password")["guid"], "token": None})
    hub.close()
    again = Hub(tmp_path / "hub")
    assert {p["name"]: p["version"] for p in again.plugins.list()} == {"net": "0.1.0", "shell": "9.0.0"}
    again.close()


def test_a_data_directory_the_hub_did_not_just_make_gets_no_default_plugins(tmp_path):
    # A data directory made before this slice: it has a database and no default plugins.
    (tmp_path / "hub").mkdir()
    sqlite3.connect(tmp_path / "hub" / "hub.db").close()
    hub = Hub(tmp_path / "hub")
    assert hub.plugins.list() == []
    hub.close()


def test_each_default_cap_has_a_one_line_description_and_a_schema():
    for name, caps in (("net", NET_CAPS), ("shell", SHELL_CAPS)):
        declared = default_module(name)
        assert sorted(declared) == caps
        for cap in declared.values():
            assert cap.description and "\n" not in cap.description and len(cap.description.splitlines()) == 1
            assert cap.schema["type"] == "object" and cap.schema.get("additionalProperties") is False


# ------------------------------------------------------------- a real worker, its caps

def test_a_real_worker_gets_the_default_plugins_and_serves_them(tmp_path):
    asyncio.run(serves(tmp_path))


async def serves(tmp_path):
    async with Rig(tmp_path) as rig:
        # Announced with the schema and description the plugin declares (W3, W5).
        record = {c["name"]: c for c in rig.hub.get_worker(rig.guid)["caps"]}
        for name in ("net", "shell"):
            for cap_name, cap in default_module(name).items():
                assert record[cap_name]["description"] == cap.description
                assert record[cap_name]["schema"] == cap.schema
                assert record[cap_name]["returns"] == ("stream" if cap.opens_stream else "result")
        assert {p["name"] for p in rig.hub.get_worker(rig.guid)["plugins"]} == {"net", "shell"}

        # shell.exec: argv with no shell; cmd in sh, with a pipe; the code; stdin; env; cwd.
        assert await rig.ok("shell.exec", argv=["echo", "a b", "$HOME"]) == \
            {"ok": True, "code": 0, "stdout": "a b $HOME\n", "stderr": ""}
        assert await rig.ok("shell.exec", cmd="printf 'one\\ntwo\\n' | wc -l; echo oops >&2; exit 3") == \
            {"ok": False, "code": 3, "stdout": "2\n", "stderr": "oops\n"}
        assert (await rig.ok("shell.exec", argv=["cat"], stdin="fed in"))["stdout"] == "fed in"
        assert (await rig.ok("shell.exec", cmd="echo $ROOK_TEST", env={"ROOK_TEST": "set"}))["stdout"] == "set\n"
        assert (await rig.ok("shell.exec", argv=["pwd"], cwd=str(tmp_path)))["stdout"] == f"{tmp_path}\n"
        bash = await rig.ok("shell.exec", cmd="echo ${BASH_VERSION:+bash}", shell="bash")
        assert bash == {"ok": True, "code": 0, "stdout": "bash\n", "stderr": "", "shell": "bash"}

        # A file written, read, listed and found, through commands.
        target = tmp_path / "notes" / "a.txt"
        await rig.ok("shell.exec", argv=["mkdir", "-p", str(target.parent)])
        await rig.ok("shell.exec", cmd=f"cat > '{target}'", stdin="hello from shell.exec\n")
        assert (await rig.ok("shell.exec", argv=["cat", str(target)]))["stdout"] == "hello from shell.exec\n"
        assert (await rig.ok("shell.exec", argv=["ls", str(target.parent)]))["stdout"] == "a.txt\n"
        found = await rig.ok("shell.exec", argv=["grep", "-rl", "from shell", str(tmp_path / "notes")])
        assert found["stdout"] == f"{target}\n"

        # shell.which.
        assert await rig.ok("shell.which", name="sh") == shutil.which("sh")
        assert await rig.ok("shell.which", name="no-such-program-here") is None

        # Calls that cannot be served fail with a reason and a next step (C3, W5).
        for args, said in [
                ({"argv": ["echo"], "cmd": "echo"}, "give argv or cmd, not both"),
                ({}, "give argv"),
                ({"argv": ["echo"], "shell": "bash"}, "shell applies to cmd"),
                ({"cmd": "dir", "shell": "cmd"}, "cmd.exe only exists on Windows workers; use shell sh"),
                ({"cmd": "ls", "shell": "fish"}, "shell must be one of"),
                ({"argv": ["no-such-program-here"]}, "find it with shell.which"),
                ({"argv": ["ls"], "cwd": str(tmp_path / "missing")}, "does not exist on this worker"),
                ({"cmd": "sleep 100", "timeout": 1}, "takes no argument timeout")]:
            reply = await rig.refused("shell.exec", **args)
            assert reply["code"] == "cap_failed" and said in reply["message"], (args, reply)
        assert "give name" in (await rig.refused("shell.which"))["message"]
        unknown = await rig.refused("shell.nothing")
        assert unknown["code"] == "unknown_cap"

        # net: the primitives as before (D3), through the default plugin.
        assert "net.http" in rig.announced()


def test_powershell_needs_pwsh_and_runs_cmd_in_it(tmp_path, monkeypatch):
    """No PowerShell here, so: refused without pwsh; with a stand-in pwsh on PATH, the cmd
    reaches it with -Command, ending in the exit-code line, and the reply names the shell."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "pwsh").write_text('#!/bin/sh\nfor a in "$@"; do printf "[%s]" "$a"; done\n')
    (fake / "pwsh").chmod(stat.S_IRWXU)

    async def go():
        async with Rig(tmp_path) as rig:
            monkeypatch.setenv("PATH", "/usr/bin:/bin")
            if shutil.which("pwsh") is None:
                refused = await rig.refused("shell.exec", cmd="Get-Date", shell="powershell")
                assert "PowerShell (pwsh) is not installed" in refused["message"]
            monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
            ran = await rig.ok("shell.exec", cmd="Get-Date", shell="pwsh")
            assert ran["shell"] == "powershell" and ran["code"] == 0
            assert ran["stdout"].startswith("[-NoLogo][-NoProfile][-NonInteractive][-Command][")
            assert "Get-Date" in ran["stdout"] and "exit $LASTEXITCODE" in ran["stdout"]
    asyncio.run(go())


# ------------------------------------------------------------------- the terminal

def test_a_terminal_opened_typed_into_resized_and_closed(tmp_path):
    asyncio.run(terminal(tmp_path))


async def terminal(tmp_path):
    async with Rig(tmp_path) as rig:
        opened = await rig.call("shell.open", argv=["/bin/sh"], cols=100, rows=30, title="my terminal")
        assert opened["type"] == "stream_open", opened
        stream = opened["stream"]
        listed = await rig.hub.streams.read(stream, 0)
        assert listed["open"]

        # Typed into (the marker is worked out by the shell, so its echo of the typing does not match); the size it was opened at; resized; the new size reaches the program.
        await rig.hub.streams.write(stream, "stty size; echo mark-$((40+1))\n", user=rig.alice)
        assert "30 100" in await rig.output(stream, "mark-41")
        await rig.hub.streams.resize(stream, 80, 24, user=rig.alice)
        await asyncio.sleep(0.2)
        await rig.hub.streams.write(stream, "stty size; echo mark-$((40+2))\n", user=rig.alice)
        assert "24 80" in await rig.output(stream, "mark-42")

        # The command ends: the stream closes.
        await rig.hub.streams.write(stream, "exit\n", user=rig.alice)
        await until(lambda: closed(rig, stream))

        # A terminal closed by a client ends its command, and everything it started.
        pid_file = tmp_path / "pid"
        opened = await rig.call("shell.open", cmd=f"echo $$ > '{pid_file}'; sleep 1000; echo never")
        stream = opened["stream"]
        await until(lambda: pid_file.exists() and pid_file.read_text().strip())
        pid = int(pid_file.read_text())
        await rig.hub.streams.close(stream, "done with it", user=rig.alice)
        await until(lambda: gone(pid))

        # On pipes (pty false): input to its standard input, output and errors together.
        opened = await rig.call("shell.open", cmd="cat; echo to-stderr >&2", pty=False)
        stream = opened["stream"]
        await rig.hub.streams.write(stream, "through the pipe\n", user=rig.alice)
        assert "through the pipe" in await rig.output(stream, "through the pipe")
        await rig.hub.streams.close(stream, "done", user=rig.alice)

        # A login shell unless a command is given; its title is the command.
        opened = await rig.call("shell.open")
        assert opened["type"] == "stream_open" and opened["title"].endswith(" -l")
        await rig.hub.streams.close(opened["stream"], "done", user=rig.alice)

        # Refused with a reason and a next step.
        assert "cols must be a whole number" in (await rig.refused("shell.open", cols=0))["message"]
        assert "pty must be true or false" in (await rig.refused("shell.open", pty="yes"))["message"]


def closed(rig, stream):
    with rig.hub._lock:
        row = rig.hub.streams._row(stream)
    return row["closed_at"] is not None


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:                                       # a zombie no one has reaped yet counts as gone
        return pathlib.Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].startswith("Z")
    except OSError:
        return True


# ------------------------------------------------------------------- the opt-out

def test_opting_a_worker_out_of_plugins_withdraws_the_default_caps(tmp_path):
    asyncio.run(opt_out(tmp_path))


async def opt_out(tmp_path):
    async with Rig(tmp_path) as rig:
        answer = await rig.hub.call("hub", "worker.plugins", {"worker": rig.guid, "receive": False},
                                    "test the opt-out", user=rig.alice)
        assert answer["type"] == "result", answer
        await until(lambda: not set(SHELL_CAPS + NET_CAPS) & set(rig.announced()))
        assert (await rig.refused("shell.exec", argv=["true"]))["code"] == "unknown_cap"
        assert (await rig.refused("net.http", url="http://127.0.0.1:9/"))["code"] == "unknown_cap"
        await rig.hub.call("hub", "worker.plugins", {"worker": rig.guid, "receive": True},
                           "test the opt-out", user=rig.alice)
        await until(lambda: set(SHELL_CAPS + NET_CAPS) <= set(rig.announced()))
        assert (await rig.ok("shell.exec", argv=["true"]))["ok"] is True
