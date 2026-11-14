"""The hub's data is its owner's only (X4, X2, L1; Bake 13 Nov). The real start command runs under
a umask of 000, which would leave every file it makes readable, and writable, by every local user:
the data directory still comes out 0700 and every file in it 0600, through setup, a secret, a
plugin install, a blob, heartbeats and a restart. A directory an older hub left open is tightened
at start, which says so; a backup restored under the same umask is owner-only too."""

import asyncio
import os
import pathlib
import stat
import sys

from helpers import eventually
from memory_link import connector
from rook_hub import Hub, backup, history
from rook_worker import Worker
from test_backup import KEEP
from test_first_run import ROOT, Started
from test_plugins import write_plugin
from test_site import ask, browser, login

NAME, PASSWORD = "Ottoline", "a password typed twice"
OPEN = 0                            # the umask: nothing is taken off what a program asks for


def loose(top):
    """Every path under `top`, itself included, that group or others have any permission on."""
    paths = [str(top)]
    for folder, folders, files in os.walk(top):
        paths += [os.path.join(folder, name) for name in folders + files]
    return [(os.path.relpath(path, top), oct(mode)) for path in paths
            if (mode := stat.S_IMODE(os.lstat(path).st_mode)) & 0o077]


def owner_only(top):
    assert stat.S_IMODE(top.stat().st_mode) == 0o700
    assert loose(top) == [], loose(top)


def tightened(lines):
    return [line for line in lines if line.startswith("  tightened:")]


async def run(*args):
    """`python -m rook_hub <args>` from the clone, under the open umask."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in ("protocol/python", "hub")))
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "rook_hub", *map(str, args), cwd=ROOT, env=env,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                   umask=OPEN)
    out, err = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode == 0, err.decode()
    return out.decode()


def test_hub_data_is_owner_only(tmp_path):
    asyncio.run(owner_only_throughout(tmp_path))


async def owner_only_throughout(tmp_path):
    data, key = tmp_path / "data", tmp_path / "keys" / "vault.key"
    plugin = pathlib.Path(write_plugin(tmp_path / "keep", {"name": "keep", "version": "1.0.0",
                                                           "hub": {"module": "hub.py"}}, KEEP))
    for path in (plugin, *plugin.iterdir()):               # a plugin as a person might have it: open to all
        path.chmod(0o755 if path.is_dir() else 0o644)

    # 1. A fresh hub: the directory, hub.db, the WAL and shared-memory files SQLite makes beside it,
    #    hub.lock, hub.key and token.secret.
    async with Started(data, key, umask=OPEN) as hub, browser() as client:
        assert not tightened(hub.lines)
        assert {"hub.db", "hub.db-wal", "hub.db-shm", "hub.lock", "hub.key", "token.secret"} <= {
            p.name for p in data.iterdir()}
        owner_only(data)
        # Setup.
        assert (await ask(client, "POST", hub.base + "/api/setup", {"name": NAME, "password": PASSWORD}))[0] == 200
        owner_only(data)
        # A secret (X2).
        status, reply = await ask(client, "POST", hub.base + "/api/secrets",
                                  {"scope": "hub", "name": "deploy-key", "value": "s3cret-value-123"})
        assert status == 200, reply
        owner_only(data)
        # A plugin install: its copied folder, and the __pycache__ Python writes as it imports the hub module.
        status, reply = await ask(client, "POST", hub.base + "/api/plugins", {"path": str(plugin)})
        assert status == 200, reply
        [folder] = (data / "plugins").iterdir()
        assert (folder / "__pycache__").is_dir() and (folder / "hub.py").exists()
        owner_only(data)
        # A blob (K3).
        status, reply = await ask(client, "POST", hub.base + "/api/call", {
            "worker": "hub", "cap": "keep.put", "args": {"title": "Notes", "text": "kept text"}, "why": "keep a note"})
        assert status == 200, reply
        record = reply["result"]["record"]
        assert len(list((data / "blobs").iterdir())) == 1
        owner_only(data)

    # 2. Heartbeats. A worker reaches a hub over the in-memory link only (there is no worker transport
    #    between processes yet), so this hub runs in the test's process, under the same open umask.
    before = os.umask(OPEN)
    try:
        hub = Hub(data, vault_key_file=key)
        assert hub.tightened == 0
        host, port = await hub.serve("127.0.0.1", 0)       # the worker knocks over HTTP
        worker = Worker(tmp_path / "w", f"http://{host}:{port}", connector(hub), name="beater", caps={}, macs=[],
                        knock_every=0.02)
        running = asyncio.create_task(worker.run())
        try:
            await eventually(lambda: hub.list_pending())
            [user] = hub._db.execute("SELECT guid FROM users").fetchall()
            approved = hub.approve(worker.key, user=user["guid"])
            await eventually(lambda: hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0] >= 1)
            # The health charts read on a read-only connection of their own (history.py): it still opens.
            assert (await history.series(hub, approved["moniker"], 3600))
            owner_only(data)
        finally:
            worker.stop()
            await asyncio.gather(running, return_exceptions=True)
            await hub.stop_serving()
            hub.close()
    finally:
        os.umask(before)

    # 3. A restart of the real start command: SQLite makes the WAL and shared-memory files again, 0600;
    #    nothing needed tightening, so nothing is said.
    async with Started(data, key, umask=OPEN) as hub, browser() as client:
        assert not tightened(hub.lines)
        assert (data / "hub.db-wal").exists() and (data / "hub.db-shm").exists()
        owner_only(data)
        assert (await login(client, hub.base, NAME, PASSWORD))[0] == 200
        status, reply = await ask(client, "GET", f"{hub.base}/api/heartbeats/{approved['moniker']}?span=3600")
        assert status == 200, reply
        status, reply = await ask(client, "POST", hub.base + "/api/call", {
            "worker": "hub", "cap": "keep.get", "args": {"record": record}, "why": "read the note back"})
        assert status == 200 and reply["result"] == {"title": "Notes", "text": "kept text"}, reply

        # 4. A backup taken while it serves, and a restore, both under the open umask: what the restore
        #    writes is owner-only, and a hub started on it finds nothing to tighten.
        saved = tmp_path / "hub.rookbackup"
        await run("backup", "--data", data, "--vault-key", key, "--to", saved)
        owner_only(data)
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600      # the backup file too, under umask 000
    restored = tmp_path / "moved" / "data"
    await run("restore", "--from", saved, "--data", restored, "--vault-key", key)
    owner_only(restored)
    async with Started(restored, key, umask=OPEN) as hub, browser() as client:
        assert not tightened(hub.lines) and "  vault:     open" in hub.lines
        owner_only(restored)
        assert (await login(client, hub.base, NAME, PASSWORD))[0] == 200


def test_an_open_data_directory_is_tightened_at_start(tmp_path):
    asyncio.run(tightened_at_start(tmp_path))


async def tightened_at_start(tmp_path):
    data, key = tmp_path / "data", tmp_path / "vault.key"
    async with Started(data, key) as hub, browser() as client:
        assert (await ask(client, "POST", hub.base + "/api/setup", {"name": NAME, "password": PASSWORD}))[0] == 200
    # As an older hub left it: the directory 0755, hub.db and every other file 0644, and the WAL and
    # shared-memory files a hub that was killed leaves behind.
    (data / "hub.db-wal").write_bytes(b"")
    (data / "hub.db-shm").write_bytes(b"")
    (data / "notes").mkdir()
    data.chmod(0o755)
    for path in data.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    opened = len(loose(data))
    assert opened == len(list(data.rglob("*"))) + 1

    async with Started(data, key, umask=OPEN) as hub, browser() as client:
        assert tightened(hub.lines) == [f"  tightened: {opened} files and folders in the data directory were open to "
                                        "other users; now only this hub's owner can read them"]
        owner_only(data)
        assert (await login(client, hub.base, NAME, PASSWORD))[0] == 200
    # Once: the next start finds nothing to tighten, and says nothing.
    async with Started(data, key, umask=OPEN) as hub:
        assert not tightened(hub.lines)
        owner_only(data)


# What another user owns, in the data directory, a restored one or a plugin's folder, cannot be
# changed by the hub's user. Without root, os.chmod is made to refuse tightening one path (a mode with
# nothing for group and others) as the system refuses it for a file another user owns; copying a
# plugin, which sets the source's modes on the hub's own new files, still works.
REFUSE = """
import os, sys
real = os.chmod
def chmod(path, mode, *args, **kwargs):
    if str(path).endswith({suffix!r}) and not mode & 0o077:
        raise PermissionError(1, "Operation not permitted", str(path))
    return real(path, mode, *args, **kwargs)
os.chmod = chmod
from rook_hub.__main__ import main
main(sys.argv[1:])
"""


def refusing(monkeypatch, suffix):
    real = os.chmod

    def chmod(path, mode, *args, **kwargs):
        if str(path).endswith(suffix) and not mode & 0o077:
            raise PermissionError(1, "Operation not permitted", str(path))
        return real(path, mode, *args, **kwargs)
    monkeypatch.setattr(os, "chmod", chmod)


def test_what_another_user_owns_is_left_as_it_is(tmp_path, monkeypatch):
    asyncio.run(another_users(tmp_path, monkeypatch))


async def another_users(tmp_path, monkeypatch):
    data, key = tmp_path / "data", tmp_path / "vault.key"
    Hub(data, vault_key_file=key).close()
    (data / "theirs.txt").write_text("not the hub's")
    (data / "theirs.txt").chmod(0o644)
    (data / "notes").mkdir(mode=0o755)
    (data / "notes").chmod(0o755)

    # 1. The start command: it tightens what it may, says in one line what it could not, and serves.
    started = Started(data, key)
    started.args = [sys.executable, "-c", REFUSE.format(suffix="theirs.txt"), *started.args[3:]]
    async with started as hub, browser() as client:
        assert tightened(hub.lines) == ["  tightened: 1 files and folders in the data directory were open to "
                                        "other users; now only this hub's owner can read them"]
        assert f"  could not tighten 1 files and folders in the data directory that another user owns, " \
               f"first: {data / 'theirs.txt'}" in hub.lines
        assert not any("Traceback" in line for line in hub.lines)
        assert (await ask(client, "POST", hub.base + "/api/setup", {"name": NAME, "password": PASSWORD}))[0] == 200
    assert stat.S_IMODE((data / "notes").stat().st_mode) == 0o700
    assert stat.S_IMODE((data / "theirs.txt").stat().st_mode) == 0o644
    (data / "theirs.txt").unlink()

    # 2. A plugin whose hub module this user may not change installs and loads as usual.
    plugin = pathlib.Path(write_plugin(tmp_path / "keep", {"name": "keep", "version": "1.0.0",
                                                           "hub": {"module": "hub.py"}}, KEEP))
    (plugin / "hub.py").chmod(0o644)
    hub = Hub(data, vault_key_file=key)
    try:
        [user] = hub._db.execute("SELECT guid FROM users").fetchall()
        refusing(monkeypatch, "hub.py")
        assert hub.plugins.install(plugin, {"user": user["guid"], "token": None})["name"] == "keep"
        assert "keep.put" in hub.plugins.caps
        monkeypatch.undo()
        [folder] = (data / "plugins").iterdir()
        assert stat.S_IMODE((folder / "hub.py").stat().st_mode) == 0o644      # left as it was
    finally:
        hub.close()

    # 3. A restore whose folder this user may not change is restored as usual.
    saved = tmp_path / "hub.rookbackup"
    backup.backup(data, key, saved)
    refusing(monkeypatch, "/plugins")
    restored = tmp_path / "moved" / "data"
    before = os.umask(0o022)               # so the restore's pass meets plugins/ open (0755), and is refused
    try:
        backup.restore(saved, restored, key)
    finally:
        os.umask(before)
    monkeypatch.undo()
    assert (restored / "hub.db").is_file() and stat.S_IMODE((restored / "plugins").stat().st_mode) & 0o077
    assert stat.S_IMODE(restored.stat().st_mode) == 0o700 and stat.S_IMODE((restored / "hub.db").stat().st_mode) == 0o600
