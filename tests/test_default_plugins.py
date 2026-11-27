"""Core's own caps as plugins installed by default (PL7): `net` (the network primitives, D3)
and `shell` (docs/inventory.md CAP-01, CAP-03, WRK4-070). A real hub, served, and a real
rook_worker over its real carrier (Telesthete over loopback WebSocket)."""

import asyncio
import hashlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import time

import pytest

import rook_hub.plugins
from rook_hub import Hub, records
from rook_hub.plugins import DEFAULTS, older
from rook_worker import Worker

SHELL_CAPS = ["shell.env", "shell.exec", "shell.open", "shell.which"]
NET_CAPS = ["net.http", "net.tcp", "net.udp"]
NEW_CAPS = ["agents-claude-code.turn", "battery.status", "file.list", "file.read", "file.search", "file.stat", "file.write",
            "shell.env", "worker.ping"]


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
        return self.hub.joined(reply)      # a result over about a megabyte comes in pieces (brief 54)

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
    assert [p["name"] for p in listed] == ["agents", "agents-claude-code", "app-android", "battery", "clipboard-android", "file", "media-android", "net", "notification-android", "phone", "shell", "sound-android", "worker"]
    assert all(p["worker"] and p["caps"] == [] and not p["site"] for p in listed[1:])   # worker parts only
    assert not listed[0]["worker"] and "agent.chat.send" in listed[0]["caps"]          # but agents: the hub's (HA2)
    assert listed[0]["site"]                                                          # and its site part, the dock (HA1)
    first = {p["name"]: p["guid"] for p in listed}
    hub.close()

    again = Hub(tmp_path / "hub")                       # present: loaded as installed, nothing reinstalled
    assert {p["name"]: p["guid"] for p in again.plugins.list()} == first
    assert len(list((tmp_path / "hub" / "plugins").iterdir())) == 13
    again.close()


def test_a_plugin_a_person_installed_in_a_defaults_place_is_kept(tmp_path):
    replacement = tmp_path / "my-shell"
    replacement.mkdir()
    (replacement / "plugin.json").write_text(json.dumps({"name": "shell", "version": "9.0.0"}))
    hub = Hub(tmp_path / "hub")
    hub.plugins.install(replacement, {"user": hub.auth.create_user("Alice", "a password")["guid"], "token": None})
    hub.close()
    again = Hub(tmp_path / "hub")
    assert {p["name"]: p["version"] for p in again.plugins.list()} == {"agents": "0.6.1", "agents-claude-code": "0.2.2",
                                                                     "app-android": "0.1.0", "battery": "0.2.0",
                                                                     "clipboard-android": "0.1.0", "file": "0.2.0",
                                                                     "media-android": "0.1.0", "net": "0.1.0", "notification-android": "0.1.0",
                                                                     "phone": "0.1.1", "shell": "9.0.0",
                                                                     "sound-android": "0.1.0", "worker": "0.1.0"}
    again.close()


def test_a_data_directory_made_before_the_default_plugins_gets_them_on_its_next_start(tmp_path):
    # A data directory made before there were default plugins: it has a database and no plugins.
    (tmp_path / "hub").mkdir()
    sqlite3.connect(tmp_path / "hub" / "hub.db").close()
    hub = Hub(tmp_path / "hub")
    assert [p["name"] for p in hub.plugins.list()] == ["agents", "agents-claude-code", "app-android", "battery", "clipboard-android", "file", "media-android", "net", "notification-android", "phone", "shell", "sound-android", "worker"]
    hub.close()


def test_a_person_plugin_at_an_older_version_is_replaced_and_one_not_in_dotted_numbers_is_left(tmp_path):
    hub = Hub(tmp_path / "hub")
    alice = {"user": hub.auth.create_user("Alice", "a password")["guid"], "token": None}
    for name, version in (("worker", "0.0.9"), ("battery", "beta")):
        folder = tmp_path / f"my-{name}"
        folder.mkdir()
        (folder / "plugin.json").write_text(json.dumps({"name": name, "version": version}))
        hub.plugins.install(folder, alice)
    hub.close()
    again = Hub(tmp_path / "hub")
    versions = {p["name"]: p["version"] for p in again.plugins.list()}
    assert versions["worker"] == "0.1.0" and versions["battery"] == "beta"
    again.close()


def test_a_default_that_cannot_be_installed_is_logged_and_the_hub_starts(tmp_path, monkeypatch, caplog):
    shipped = tmp_path / "defaults"
    shutil.copytree(DEFAULTS / "worker", shipped / "worker")
    (shipped / "broken").mkdir()
    (shipped / "broken" / "plugin.json").write_text(json.dumps({"name": "broken", "version": "1.0.0",
                                                                "hub": {"module": "hub.py"}}))
    (shipped / "broken" / "hub.py").write_text("def setup(plugin):\n    raise RuntimeError('no')\n")
    monkeypatch.setattr(rook_hub.plugins, "DEFAULTS", shipped)
    hub = Hub(tmp_path / "hub")
    assert [p["name"] for p in hub.plugins.list()] == ["worker"]
    assert "Plugin broken 1.0.0 was not installed" in caplog.text
    hub.close()


def test_older_compares_dotted_numbers_and_nothing_else():
    assert older("0.1.0", "0.2.0") and older("0.9", "0.10") and older("1", "1.0.1")
    assert not older("0.2.0", "0.2.0") and not older("0.1", "0.1.0") and not older("1.0.0", "0.9.9")
    assert not older("beta", "0.2.0") and not older("0.1.0-rc1", "0.2.0") and not older("0.1.0", "")


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
        assert {p["name"] for p in rig.hub.get_worker(rig.guid)["plugins"]} == {"agents-claude-code", "app-android", "battery", "clipboard-android", "file", "media-android", "net", "notification-android", "phone", "shell", "sound-android", "worker"}

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


# ------------------------------------------------------- a hub made by an older core (PL7)

def kept(data):
    """What a start must leave as it was: users, tokens, workers and their keys, records, secrets."""
    db = sqlite3.connect(data / "hub.db")
    try:
        return {table: db.execute(f"SELECT {columns} FROM {table} ORDER BY 1, 2").fetchall()
                for table, columns in (("users", "*"), ("tokens", "*"), ("keys", "*"), ("records", "*"),
                                       ("secrets", "*"), ("plugin_optouts", "worker, 1"),
                                       ("workers", "guid, moniker, name, owner, approved_at, interval"))}
    finally:
        db.close()


def plugin_rows(data):
    db = sqlite3.connect(data / "hub.db")
    try:
        return db.execute("SELECT guid, name, version, folder, installed_at FROM plugins ORDER BY name").fetchall()
    finally:
        db.close()


def test_a_hub_made_by_an_older_core_gets_the_new_defaults_on_its_next_start(tmp_path, monkeypatch):
    asyncio.run(older_core(tmp_path, monkeypatch))


async def older_core(tmp_path, monkeypatch):
    # The defaults an older core shipped: net, and shell 0.1.0 without shell.env.
    old = tmp_path / "old-defaults"
    for name in ("net", "shell"):
        shutil.copytree(DEFAULTS / name, old / name)
    (old / "shell" / "plugin.json").write_text(json.dumps({"name": "shell", "version": "0.1.0",
                                                           "worker": {"python": {"module": "worker.py"}}}))
    with open(old / "shell" / "worker.py", "a") as f:
        f.write('\nCAPS.pop("shell.env")\n')
    data, key = tmp_path / "hub", tmp_path / "keys" / "vault.key"

    monkeypatch.setattr(rook_hub.plugins, "DEFAULTS", old)
    hub = Hub(data, vault_key_file=key)
    host, port = await hub.serve("127.0.0.1", 0)
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    hub.auth.mint("agent", user=alice)
    hub.vault.create({"scope": "hub", "name": "deploy-key", "value": "s3cret"}, {"user": alice, "token": None})
    records.store_blob(hub, b"kept bytes", "text/plain", "Kept", user=alice)
    assert {p["name"]: p["version"] for p in hub.plugins.list()} == {"net": "0.1.0", "shell": "0.1.0"}
    worker = Worker(tmp_path / "box", f"http://{host}:{port}", name="box", macs=[], knock_every=0.02)
    task = asyncio.create_task(worker.run())
    try:
        await until(lambda: any(p["key"] == worker.key for p in hub.list_pending()))
        guid = hub.approve(worker.key, user=alice)["guid"]

        def announced():
            return {c["name"] for c in hub.get_worker(guid)["caps"] or []}

        await until(lambda: set(NET_CAPS) | {"shell.exec"} <= announced())
        assert not set(NEW_CAPS) & announced()
        await hub.stop_serving()
        hub.close()
        before = kept(data)

        # The next start, by this core: file, battery and worker go in, shell goes to its version now.
        monkeypatch.setattr(rook_hub.plugins, "DEFAULTS", DEFAULTS)
        hub = Hub(data, vault_key_file=key)
        assert {p["name"]: p["version"] for p in hub.plugins.list()} == {
            name: version for name, (version, _) in SHIPPED.items()}
        await hub.serve(host, port)                  # the same address: the worker's hub URL
        # The worker, never restarted, links again and is sent the new worker parts, as on any install.
        await until(lambda: set(NEW_CAPS + NET_CAPS + SHELL_CAPS) <= announced(), timeout=60)
        reply = await hub.call(guid, "worker.ping", {}, "the new default answers", user=alice)
        assert reply["type"] == "result" and reply["result"]["pong"] is True, reply
        reply = await hub.call(guid, "shell.env", {"name": "HOME"}, "the updated default answers", user=alice)
        assert reply["type"] == "result", reply
        await hub.stop_serving()
        hub.close()
        after = kept(data)
        made = [r for r in after["records"] if r[3] == "agent"]          # the house agent, made at that start (HA2)
        assert len(made) == 1 and dict(after, records=[r for r in after["records"] if r[3] != "agent"]) == before
        rows = plugin_rows(data)
        assert len(list((data / "plugins").iterdir())) == 13   # one copy per plugin: shell 0.1.0's is gone

        # A second start with nothing new changes nothing.
        hub = Hub(data, vault_key_file=key)
        hub.close()
        assert plugin_rows(data) == rows and kept(data) == after
        assert len(list((data / "plugins").iterdir())) == 13
    finally:
        worker.stop()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 30)
        if hub._runner is not None:
            await hub.stop_serving()


# ---------------------------------------------- a changed built-in carries a new version (brief 55)

# Each built-in plugin's version and a SHA-256 of its files, as shipped. A hub that already exists
# updates a built-in only when the shipped version is newer (brief 45), so a built-in whose files
# change at the same version never reaches it. When a test below says so, raise the version in the
# plugin's plugin.json and set its line here to what the test prints.
# Every version of every built-in plugin that has been merged, with a SHA-256 of its files (files_hash),
# oldest first (brief 55; made stricter after the dock's merge, when agents 0.1.0 was merged with new
# files and a hub that had the old 0.1.0 kept it). A line here is never changed once merged: a
# built-in whose files change gets a newer version and a new line, which the guard prints. The
# versions before house-agent's tip were read from its history, each with the files it last had.
RECORDED = {
    "agents": {"0.1.0": "a848c4503bd9e67f25b9bfb9c58de5851dc5ad36e1a3bd49af3b7de3041d72a4",
               "0.2.0": "909084182684abe95242f81f66aa119ea74810d347693557fe2e96a23234e76a",
               "0.3.0": "a21cae8e3453a4b38a9242f235c14df7ea3a1750ac779aee0bf7fd6de374fa68",
               "0.4.0": "ba113ee293f5b629cf888c673a9a96cb217841b75a9d7b1bee0106ae68527618",    # memory (MEM5)
               "0.5.0": "b162812e1d3a8d05545d7ce41bc0629cea66ae28d30ae5719a6a0c8b7871b996",    # the dock's read let go of on pagehide (HA7)
               "0.6.0": "054d95158063f1b1cb7748a9c5e15c063b82c79dc123e8863e53e609b55fe130",    # the chat is a phone's whole screen; [[open: app]] (AP1)
               "0.6.1": "3c6304ec5676565f422aef981aac0cfca6825c4c3612d5b892ae63228c8c9cbe"},   # the chat open or closed is decided once it has loaded (AP6)
    "agents-claude-code": {"0.1.0": "79653aecb79c275eb65a7f1d5d88081c22f33e3f31767d3ef963aa47c26b8eeb",
                           "0.2.0": "8ba8ef43fb7160bc68a51939eb74dd0d074349f965c6e1f52ce15caa88e784e9",   # fork, schema, precompact
                           "0.2.1": "7d348fd4df50731a87406c363072e095badb72d58b24266f9f7fa3a7e2ef0b3a",   # the hook names itself (User-Agent)
                           "0.2.2": "b1fe650c04a51b06eb8a6a1f4026f7735ece517aa122d4d1c92a870d5c0b4453"},  # announces nothing on a phone (AP6)
    "app-android": {"0.1.0": "186e318f162c98e13f358d4f3bd568c0de8923038b93a987967d3e47e16be06c"},   # the phone's apps (AP2)
    "battery": {"0.1.0": "eb0cab1ebe8c526ac06b274905516c07cff649a18cf65c61f7708e31641cdd5a",
                "0.2.0": "51134f64c5cc2d0ec08126c0ca81b967deb2010763506f930c791f8c5e0f8a53"},   # the phone's battery (AP2)
    "clipboard-android": {"0.1.0": "a0f37225aa25fdca13be975ae5f9921f2cf0c4f3514a4d2fd3433922d0c2629c"},   # AP2
    "file": {"0.1.0": "fabb70ed8cd611a826a9eae004f255cf3c282f7e20fb6853990780182655d216",
             "0.2.0": "be10d9d7242587b70941b096d90b21b8ea498ca0befa0c1f873dd5ec8b770bca"},
    "media-android": {"0.1.0": "da73d33004317ee8de21ee0aa3a99cdbd287ea5655376560e299b0c1c33a71b8"},   # the phone's media (AP4)
    "net": {"0.1.0": "34e3b171d08b20d38388157725989321aa6ddd2131356eba1ef9421c4eada8f7"},
    "notification-android": {"0.1.0": "575ded1fb642196c6bcb3798002df77fa3f16d99f679d26f8bf4bfec31dc9c5f"},   # AP4
    "phone": {"0.1.0": "08a6199454b9e2daa784b6233e183bff6bfebda3ece190491b5198c54a009806",   # find, vibrate, torch (AP2)
              "0.1.1": "624242a8be87a87b6d471331659841c06ed072b38f30c2ce4435bb371976dbb7"},  # vibrate without ms: 300 ms (PR 465)
    "shell": {"0.1.0": "df540c027d5e5da20fa01465b0426033570f62fe8b59a6cc2edcc10f574817b7",
              "0.2.0": "95a8ed17f73d6d3e5baa8107031c8ff1b5ed7ed3230accb1493c37372259f633",
              "0.3.0": "31b456adeaa73e0063c4a8e18b19268d8a21bdba41cd5acdfd8d3770f14b09db",
              "0.4.0": "d40819a1ad1615bd82667077ebeb5ee6cc14be84684a2832b5d6ed09b157b37f",
              "0.5.0": "f57565e2f29bb6eb1ba20b4d33ab3efa329f9699bd9280d3e27b54dac8f73733"},
    "sound-android": {"0.1.0": "bd87cf134638d18ae8e91520805709e32ad19fbdd4aa43cd15439d6c962d88e3"},   # AP2
    "worker": {"0.1.0": "451d156a0e1d8df3db76493f3626f89bba69e4cff287ccab8ad84abac1444dc0"},
}
SHIPPED = {name: next(reversed(versions.items())) for name, versions in RECORDED.items()}   # each one's newest
MERGED = "origin/house-agent"   # the branch whose merged built-ins the record must keep


def files_hash(folder):
    """SHA-256 over every file in a plugin's folder (its path and its bytes), __pycache__ left out."""
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        for part in (path.relative_to(folder).as_posix().encode("utf-8"), path.read_bytes()):
            digest.update(len(part).to_bytes(8, "big") + part)
    return digest.hexdigest()


def built_ins(defaults):
    """{name: (version, files hash)} of the built-in plugins in a defaults folder."""
    return {f.name: (json.loads((f / "plugin.json").read_text())["version"], files_hash(f))
            for f in defaults.iterdir() if (f / "plugin.json").is_file()}


def unversioned_changes(defaults, recorded):
    """Each way the built-ins in `defaults` differ from the record, saying what to do."""
    found = built_ins(defaults)
    problems = []
    for name in sorted(set(found) | set(recorded)):
        if name not in found:
            problems.append(f"{name} is in RECORDED but not under {defaults}: take it out of RECORDED.")
            continue
        version, digest = found[name]
        versions = recorded.get(name, {})
        if not versions:
            problems.append(f"{name} {version} is a new built-in: add it to RECORDED in tests/test_default_plugins.py:\n"
                            f"    {name!r}: {{{version!r}: {digest!r}}},")
        elif version in versions:
            if versions[version] != digest:
                problems.append(f"The files of the built-in plugin {name} differ from those recorded for its version "
                                f"{version}, so a hub that already has {version} keeps its old files (briefs 45, 55). "
                                f"Raise the version in hub/rook_hub/defaults/{name}/plugin.json and add the line this "
                                f"test then prints; never change a recorded line.")
        elif any(not older(was, version) for was in versions):
            newest = next(reversed(versions))
            problems.append(f"The built-in plugin {name} is {version}, which is not newer than {newest}, recorded "
                            f"before, so a hub that has {newest} keeps it. Give it a version newer than {newest}.")
        else:
            problems.append(f"The built-in plugin {name} is now {version}: add this to its versions in RECORDED in "
                            f"tests/test_default_plugins.py, after the others:\n    {version!r}: {digest!r},")
    return problems


def not_kept(merged, recorded, where):
    """Each built-in `merged` (from built_ins) whose version the record does not keep with those files."""
    return [f"{where} has the built-in plugin {name} {version} with files hashing {digest}, but RECORDED "
            f"{'says ' + recorded[name][version] if version in recorded.get(name, {}) else 'has no ' + version} for it. "
            f"Once merged, a version's files and its line never change: put the line back and give your changes a "
            f"newer version."
            for name, (version, digest) in sorted(merged.items()) if recorded.get(name, {}).get(version) != digest]


def test_a_built_in_plugin_whose_files_changed_has_a_new_version():
    problems = unversioned_changes(DEFAULTS, RECORDED)
    assert not problems, "\n\n".join(problems)
    for name, versions in RECORDED.items():      # oldest first, each newer than the one before
        listed = list(versions)
        assert all(older(a, b) for a, b in zip(listed, listed[1:])), (name, listed)


def test_the_record_keeps_what_was_merged(tmp_path):
    """The built-ins on house-agent, as merged, are in the record with their files unchanged, so a
    pull request cannot change a merged version's files by changing its line. Needs a git checkout
    that has origin/house-agent; a git archive export has none, and skips it."""
    root = DEFAULTS.parents[2]
    git = ["git", "-C", str(root)]
    if subprocess.run([*git, "rev-parse", "--verify", "--quiet", MERGED + "^{commit}"], capture_output=True).returncode:
        pytest.skip(f"no git checkout with {MERGED} here (a git archive export, say): nothing merged to compare with")
    archive = subprocess.run([*git, "archive", MERGED, "hub/rook_hub/defaults"], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path, filter="data")
    problems = not_kept(built_ins(tmp_path / "hub" / "rook_hub" / "defaults"), RECORDED, MERGED)
    assert not problems, "\n\n".join(problems)


def test_the_version_guard_catches_what_it_should(tmp_path):
    shipped = tmp_path / "defaults"
    shutil.copytree(DEFAULTS, shipped, ignore=shutil.ignore_patterns("__pycache__"))
    assert unversioned_changes(shipped, RECORDED) == []

    # Core cec3ea0e's state: brief 54's files, at the versions from before them.
    for name, version in (("shell", "0.2.0"), ("file", "0.1.0")):
        manifest = json.loads((shipped / name / "plugin.json").read_text())
        (shipped / name / "plugin.json").write_text(json.dumps(dict(manifest, version=version)))
    problems = unversioned_changes(shipped, RECORDED)
    assert len(problems) == 2 and all("differ from those recorded for its version" in p for p in problems), problems
    shutil.rmtree(shipped)
    shutil.copytree(DEFAULTS, shipped, ignore=shutil.ignore_patterns("__pycache__"))

    # A file changed at the same version: raise the version.
    with open(shipped / "net" / "worker.py", "a") as f:
        f.write("# changed\n")
    [problem] = unversioned_changes(shipped, RECORDED)
    assert "files of the built-in plugin net differ from those recorded for its version 0.1.0" in problem
    assert "Raise the version in hub/rook_hub/defaults/net/plugin.json" in problem

    # Raised: the line to add is given; going back to an older version is refused.
    (shipped / "net" / "plugin.json").write_text((shipped / "net" / "plugin.json").read_text().replace("0.1.0", "0.1.1"))
    [problem] = unversioned_changes(shipped, RECORDED)
    assert f"'0.1.1': '{files_hash(shipped / 'net')}'," in problem
    (shipped / "shell" / "plugin.json").write_text((shipped / "shell" / "plugin.json").read_text().replace("0.5.0", "0.4.5"))
    assert any("shell is 0.4.5, which is not newer than 0.5.0" in p for p in unversioned_changes(shipped, RECORDED))

    # A new built-in, and one gone.
    shutil.copytree(shipped / "worker", shipped / "extra")
    shutil.rmtree(shipped / "battery")
    problems = unversioned_changes(shipped, RECORDED)
    assert any(p.startswith("battery is in RECORDED but not under") for p in problems)
    assert any(p.startswith("extra 0.1.0 is a new built-in") for p in problems)

    # The fault this was made stricter for: house-agent merged agents 0.1.0 (HA2), then a merge
    # changed its files and wrote the new hash over 0.1.0's line, and a hub with the first 0.1.0
    # kept it. The record no longer keeps what was merged, so the check of the merged branch fails.
    merged = built_ins(DEFAULTS)
    version = merged["agents"][0]                             # agents' version now stands in for that 0.1.0
    changed = {name: dict(versions) for name, versions in RECORDED.items()}
    changed["agents"][version] = "0" * 64                     # the line written over
    [problem] = not_kept(merged, changed, MERGED)
    assert problem.startswith(f"{MERGED} has the built-in plugin agents {version} with files hashing {merged['agents'][1]}")
    assert "never change" in problem
    assert not_kept(merged, RECORDED, MERGED) == []


def test_a_hub_with_shell_0_2_0_and_file_0_1_0_gets_the_versions_shipped(tmp_path, monkeypatch):
    asyncio.run(before_brief_54(tmp_path, monkeypatch))


async def before_brief_54(tmp_path, monkeypatch):
    # What a hub made before this change holds: shell 0.2.0 and file 0.1.0. Their copies here are
    # marked by one cap each, shell.was and file.was, so the test can see the old code go.
    old = tmp_path / "old-defaults"
    shutil.copytree(DEFAULTS, old, ignore=shutil.ignore_patterns("__pycache__"))
    for name, version, keep in (("shell", "0.2.0", "shell.which"), ("file", "0.1.0", "file.stat")):
        manifest = json.loads((old / name / "plugin.json").read_text())
        (old / name / "plugin.json").write_text(json.dumps(dict(manifest, version=version)))
        with open(old / name / "worker.py", "a") as f:
            f.write(f'\nCAPS["{name}.was"] = Cap("The copy of {name} {version} this test starts from.", '
                    f'{{"type": "object"}}, CAPS["{keep}"].handler)\n')
    data = tmp_path / "hub"
    monkeypatch.setattr(rook_hub.plugins, "DEFAULTS", old)
    hub = Hub(data)
    host, port = await hub.serve("127.0.0.1", 0)
    alice = hub.auth.create_user("Alice", "a password")["guid"]
    assert {p["name"]: p["version"] for p in hub.plugins.list()} == {
        "agents": "0.6.1", "agents-claude-code": "0.2.2", "app-android": "0.1.0", "battery": "0.2.0",
        "clipboard-android": "0.1.0", "file": "0.1.0", "media-android": "0.1.0", "net": "0.1.0",
        "notification-android": "0.1.0", "phone": "0.1.1", "shell": "0.2.0",
        "sound-android": "0.1.0", "worker": "0.1.0"}
    worker = Worker(tmp_path / "box", f"http://{host}:{port}", name="box", macs=[], knock_every=0.02)
    task = asyncio.create_task(worker.run())
    try:
        await until(lambda: any(p["key"] == worker.key for p in hub.list_pending()))
        guid = hub.approve(worker.key, user=alice)["guid"]

        def announced():
            return {c["name"] for c in hub.get_worker(guid)["caps"] or []}

        await until(lambda: {"shell.was", "file.was", "shell.exec", "file.read"} <= announced())
        await hub.stop_serving()
        hub.close()

        # The next start, by this core: shell and file at the versions shipped now (SHIPPED), and the
        # worker, never restarted, is sent them when it links again.
        monkeypatch.setattr(rook_hub.plugins, "DEFAULTS", DEFAULTS)
        hub = Hub(data)
        assert {p["name"]: p["version"] for p in hub.plugins.list()} == {
            name: version for name, (version, _) in SHIPPED.items()}
        await hub.serve(host, port)
        await until(lambda: {"shell.exec", "file.read"} <= announced() and not {"shell.was", "file.was"} & announced(),
                    timeout=60)
        reply = await hub.call(guid, "shell.exec", {"argv": ["echo", "new"]}, "the new shell answers", user=alice)
        assert reply["type"] == "result" and reply["result"]["stdout"] == "new\n", reply
        reply = await hub.call(guid, "file.stat", {"path": str(tmp_path)}, "the new file answers", user=alice)
        assert reply["type"] == "result" and reply["result"]["type"] == "dir", reply
        assert [(p["name"], p["version"]) for p in hub.get_worker(guid)["plugins"]
                if p["name"] in ("shell", "file")] == [("file", SHIPPED["file"][0]), ("shell", SHIPPED["shell"][0])]
    finally:
        worker.stop()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 30)
        if hub._runner is not None:
            await hub.stop_serving()
        hub.close()
