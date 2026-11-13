"""Backup, restore and move (F3, X4, X2, R5, W7, K3; Bake 13 Nov), with the real commands as
processes. A hub with a user, a session, an agent token, a secret, a plugin with hub caps, a
record with a blob, journal entries, a recorded stream and an approved worker is backed up while
it serves and heartbeats and stream output keep arriving; the backup is restored under another
path with the key file moved too; and the start command serves the restored data as the same hub."""

import asyncio
import ctypes
import ctypes.util
import fcntl
import json
import os
import struct
import pathlib
import shutil
import sys

from nacl.signing import VerifyKey

from helpers import eventually
from memory_link import connector
from rook_hub import Hub, backup
from rook_protocol import decode, encode
from rook_worker import Cap, Worker
from test_door import mcp_client, tool
from test_first_run import ROOT, Started
from test_plugins import write_plugin
from test_site import COOKIE, ask, browser, login

NAME = "Wilhelmina Quartermaine"
PASSWORD = "a long password nobody guesses"
SECRET = "deploy-key-7f3a9c2e5b1d4f60"
WHY = "Keep the launch checklist where the whole team can find it"
TEXT = "Step one: check the checklist. Step two: launch."
MARKER = "KEEPSAKE-SOURCE-MARKER-41"
KEEP = f'''"""{MARKER}: keeps text as a blob, with a record that links to it."""


def setup(plugin):
    plugin.record_type("keepsake")

    @plugin.cap("keep.put", "Keep some text as a blob, with a record linking to it.", {{"type": "object"}})
    def put(call, args):
        blob = call.store_blob(args["text"].encode("utf-8"), "text/plain", title="kept text")
        record = call.create("keepsake", args["title"], body={{"blob": blob["guid"]}})
        call.link(record["guid"], blob["guid"])
        return {{"record": record["moniker"]}}

    @plugin.cap("keep.get", "Read a kept record and its text.", {{"type": "object"}})
    def get(call, args):
        record = call.get(args["record"])
        return {{"title": record["title"], "text": call.read_blob(record["body"]["blob"]).decode("utf-8")}}

    @plugin.cap("keep.echo", "Return the arguments as the hub hands them to the plugin.", {{"type": "object"}})
    def echo(call, args):
        return args
'''

live = []


async def ticker(args, stream):
    live.append(stream)
    return {"title": "build log", "description": "lines from a long build"}


class Watch:
    """Every name created in, or moved into, a directory while it is watched (Linux inotify): a file
    that exists for a moment is caught as surely as one that stays."""

    def __init__(self, path):
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK)
        assert self.fd >= 0 and self.libc.inotify_add_watch(self.fd, os.fsencode(path), 0x100 | 0x80) >= 0  # CREATE, MOVED_TO

    def names(self):
        found = []
        while True:
            try:
                data = os.read(self.fd, 1 << 16)
            except BlockingIOError:
                os.close(self.fd)
                return found
            i = 0
            while i < len(data):
                wd, mask, cookie, length = struct.unpack_from("iIII", data, i)
                found.append(data[i + 16:i + 16 + length].rstrip(b"\0").decode())
                i += 16 + length


async def run(*args):
    """`python -m rook_hub <args>` as a person runs it from the clone (README.md)."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in ("protocol/python", "hub")))
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "rook_hub", *args, cwd=ROOT, env=env,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(process.communicate(), 120)
    return process.returncode, out.decode(), err.decode()


def test_backup_restore_move(tmp_path):
    live.clear()
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    data, key = tmp_path / "here" / "data", tmp_path / "here" / "keys" / "vault.key"
    hub = Hub(data, vault_key_file=key)
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    user = hub.auth.create_user(NAME, PASSWORD)
    agent = hub.auth.mint("agent", user=user["guid"])
    hub.vault.create({"scope": "hub", "name": "deploy-key", "value": SECRET}, {"user": user["guid"], "token": None})
    worker = Worker(tmp_path / "w", base, connector(hub), name="builder", caps={
        "build.log": Cap("Open the build log.", {"type": "object"}, ticker, opens_stream=True)}, macs=[], knock_every=0.02)
    running = asyncio.create_task(worker.run())
    await eventually(lambda: hub.list_pending())
    approved = hub.approve(worker.key, user=user["guid"])
    hub.set_interval(approved["moniker"], 1)                 # heartbeats every second while the backup runs
    await eventually(lambda: hub.get_worker(approved["moniker"])["online"])
    plugin = write_plugin(tmp_path / "keep", {"name": "keep", "version": "1.0.0", "hub": {"module": "hub.py"}}, KEEP)
    pumping = None
    try:
        async with mcp_client(f"{base}/mcp", agent) as door, browser() as client:
            async def call(worker_name, cap, why, **args):
                failed, reply = await tool(door, "call", worker=worker_name, cap=cap, args=args, why=why)
                assert not failed, reply
                return reply

            await call("hub", "plugin.install", "install keep", path=plugin)
            kept = (await call("hub", "keep.put", WHY, title="Launch checklist", text=TEXT))["record"]
            stream = (await call(approved["moniker"], "build.log", "watch the build"))["stream"]
            for n in range(3):
                await live[0].send(f"line {n}\n")
            await eventually(lambda: hub.streams.get(stream) and len(
                hub._db.execute("SELECT 1 FROM stream_data d JOIN streams s ON s.guid = d.stream WHERE s.moniker = ?",
                                (stream,)).fetchall()) == 3)
            assert (await login(client, base, NAME, PASSWORD))[0] == 200
            session = next(c.value for c in client.cookie_jar if c.key == COOKIE)

            # 1. A backup while the hub serves and writes: heartbeats and stream output keep arriving.
            async def pump():
                n = 0
                while True:
                    await live[0].send(f"tick {n}\n")
                    n += 1
                    await asyncio.sleep(0.005)
            pumping = asyncio.create_task(pump())
            beats = hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0]
            saved = tmp_path / "backups" / "hub.rookbackup"
            saved.parent.mkdir()
            watch = Watch(saved.parent)
            code, out, err = await run("backup", "--data", str(data), "--vault-key", str(key), "--to", str(saved))
            assert code == 0, err
            # The destination only ever saw the sealed backup: no plain working copy, not even for a moment (X4).
            assert watch.names() == ["hub.rookbackup"]
            assert not (data / backup.COPY).exists()            # the working copy, in the data directory, is gone
            assert "written to" in out and "Keep the vault key file apart" in out
            await asyncio.sleep(1.2)
            assert hub._db.execute("SELECT count(*) FROM heartbeats").fetchone()[0] > beats   # it went on serving
            assert (data / "hub.lock").exists() and hub._serving_dir is not None
            pumping.cancel()
            assert sorted(p.name for p in saved.parent.iterdir()) == ["hub.rookbackup"]   # no copy left beside it

            # A plain copy an interrupted backup left in the data directory is cleared by the next backup;
            # a backup that finds another one running is refused and writes nothing.
            (data / backup.COPY).write_bytes(b"SQLite format 3\0" + NAME.encode())
            again = tmp_path / "backups" / "again.rookbackup"
            code, out, err = await run("backup", "--data", str(data), "--vault-key", str(key), "--to", str(again))
            assert code == 0 and not (data / backup.COPY).exists(), err
            with open(data / backup.COPY, "wb") as other:
                fcntl.flock(other, fcntl.LOCK_EX)
                busy = tmp_path / "backups" / "busy.rookbackup"
                code, out, err = await run("backup", "--data", str(data), "--vault-key", str(key), "--to", str(busy))
                assert code != 0 and "Another backup" in err and not busy.exists(), err
            (data / backup.COPY).unlink()
            again.unlink()
    finally:
        if pumping is not None:
            pumping.cancel()
        worker.stop()
        await asyncio.gather(running, return_exceptions=True)
        await hub.stop_serving()
        hub.close()

    # 2. The file shows nothing: no name, secret, why, plugin source, or key.
    blob = saved.read_bytes()
    vault_key = bytes.fromhex(key.read_text().strip())
    for plain in (NAME.encode(), SECRET.encode(), WHY.encode(), TEXT.encode(), MARKER.encode(), b"keep.put",
                  b"build log", b"line 1", b"SQLite format 3", vault_key, key.read_text().strip().encode(),
                  (data / "token.secret").read_text().strip().encode(), (data / "hub.key").read_text().strip().encode()):
        assert plain not in blob, plain
    assert blob.startswith(backup.MAGIC)

    # 3. Refusals, each with a plain message and nothing written.
    other_key = tmp_path / "other.key"
    other_key.write_text(os.urandom(32).hex() + "\n")
    cut = tmp_path / "cut.rookbackup"
    cut.write_bytes(blob[:-1000])
    at_boundary = tmp_path / "boundary.rookbackup"
    at_boundary.write_bytes(blob[:len(backup.MAGIC) + backup.HEADER + 2 * backup.SEALED])
    changed = tmp_path / "changed.rookbackup"
    changed.write_bytes(blob[:len(blob) // 2] + bytes([blob[len(blob) // 2] ^ 1]) + blob[len(blob) // 2 + 1:])
    full = tmp_path / "full"
    (full / "data").mkdir(parents=True)
    (full / "data" / "notes.txt").write_text("mine")
    for source, target, use_key, said in (
            (saved, tmp_path / "t1" / "data", other_key, "does not open with this vault key"),
            (cut, tmp_path / "t2" / "data", key, "cut short"),
            (at_boundary, tmp_path / "t3" / "data", key, "cut short"),
            (changed, tmp_path / "t4" / "data", key, "changed or damaged"),
            (saved, full / "data", key, "already holds something")):
        code, out, err = await run("restore", "--from", str(source), "--data", str(target), "--vault-key", str(use_key))
        assert code != 0 and said in err and "Nothing was written." in err, (said, err)
        if target.parent == full:
            assert [p.name for p in (full / "data").iterdir()] == ["notes.txt"] and len(list(full.iterdir())) == 1
        else:
            assert not target.parent.exists(), list(target.parent.iterdir())
    code, out, err = await run("backup", "--data", str(data), "--vault-key", str(other_key), "--to",
                               str(tmp_path / "wrong.rookbackup"))
    assert code != 0 and "does not hold this hub's vault key" in err and not (tmp_path / "wrong.rookbackup").exists()

    # 4. Move: the key file and the backup go elsewhere; restore under another path; start it there.
    moved_key = tmp_path / "there" / "secrets" / "vault.key"
    moved_key.parent.mkdir(parents=True)
    shutil.copy(key, moved_key)
    moved = tmp_path / "there" / "srv" / "rook" / "data"
    code, out, err = await run("restore", "--from", str(saved), "--data", str(moved), "--vault-key", str(moved_key))
    assert code == 0 and "Restored" in out, err
    shutil.rmtree(tmp_path / "here")                         # the old machine is gone
    async with Started(moved, moved_key) as there:
        assert "  vault:     open" in there.lines
        new = there.base
        async with browser() as client, mcp_client(f"{new}/mcp", agent) as door:
            # Users log in; a session made before still works.
            assert (await login(client, new, NAME, PASSWORD))[0] == 200
            async with browser() as before:
                before.cookie_jar.update_cookies({COOKIE: session})
                status, me = await ask(before, "GET", f"{new}/api/session")
                assert status == 200 and me["user"]["name"] == NAME, me

            async def call(worker_name, cap, why, **args):
                failed, reply = await tool(door, "call", worker=worker_name, cap=cap, args=args, why=why)
                assert not failed, reply
                return reply

            # The agent token made before works; secrets open; the plugin loads and its caps answer;
            # the record and its blob read back.
            assert await call("hub", "keep.echo", "check a secret", value="secret:deploy-key") == {"value": SECRET}
            assert await call("hub", "keep.get", "read it back", record=kept) == {"title": "Launch checklist",
                                                                                   "text": TEXT}
            listed = (await call("hub", "plugin.list", "see the plugins"))["plugins"]
            assert [p["name"] for p in listed] == ["keep"] and "keep.put" in listed[0]["caps"]
            # The journal and the recorded stream read back.
            failed, found = await tool(door, "journal", text="launch checklist")
            assert any(e["why"] == WHY for e in found["entries"])
            failed, read = await tool(door, "stream", action="read", stream=stream, **{"from": 0})
            assert [e["out"] for e in read["entries"][:3]] == ["line 0\n", "line 1\n", "line 2\n"]
            assert len(read["entries"]) > 3            # what arrived during the backup, up to its moment
            # The approved worker's identity is still accepted: its knock is answered as approved, signed by
            # the same hub key (R5, W7).
            async with client.post(f"{new}/knock", data=encode({
                    "type": "knock", "protocol": 1, "name": "builder", "key": worker.key, "macs": [], "caps": []}),
                    headers={"Content-Type": "application/json"}) as response:
                answer = decode(await response.read())
            assert answer["status"] == "approved" and (answer["guid"], answer["moniker"]) == (
                approved["guid"], approved["moniker"])
            assert answer["hub_key"] == worker.identity["hub_key"] and answer["signature"] == worker.identity["signature"]
            VerifyKey(bytes.fromhex(answer["hub_key"])).verify(b"rook-worker-key:" + bytes.fromhex(worker.key),
                                                               bytes.fromhex(answer["signature"]))
