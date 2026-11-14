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
from rook_hub import Hub, history
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
