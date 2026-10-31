"""Off-site hub backup (rook.core2.hub.backup, gate R5): sealing, snapshots
of a live hub, restore round trips, the targets (dir, a worker's
backup-store plugin, rclone), retention, the hub caps and the CLI.

Everything runs under a hard timeout; subprocesses get their own."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
import threading
import urllib.request
from pathlib import Path

import pytest
import yaml

pytest.importorskip("nacl")

from rook.core2.enroll.hub import recovery_key  # noqa: E402
from rook.core2.hub import backup as bk  # noqa: E402
from rook.core2.hub.identity import Caller as HubCaller  # noqa: E402
from rook.core2.hub.runtime import Hub, Loopback, _key, load_config  # noqa: E402
from rook.core2.hub.vault import Vault  # noqa: E402
from rook.core2.worker.host import CallError, Host  # noqa: E402
from rook.kit.ctx import Caller  # noqa: E402
from rook.kit.manifest import check as check_manifest  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check the hub backup."
PSK = "9c1d2e3f40516273849506a7b8c9d0e1f2a3b4c5d6e7f809"
SECRET = "ghp_n0tInTheClearAnywhere"


def go(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def make_hub(root: Path, library: bool = True) -> Path:
    """A hub folder as init leaves it, with some life in it: a WAL journal,
    a vault file, a plugin's own database, plugin data, a cache and the
    leftovers a backup must skip. Returns the config path."""
    root.mkdir(parents=True)
    cfg = {"data": "./data", "band": BAND, "users": {"bake": {"preset": "owner"}},
           "doors": {"http": {"bind": "127.0.0.1:0"}},
           "transport": {"bands": [{"psk": PSK, "relay": "127.0.0.1:1"}]}}
    data = root / "data"
    (data / "keys").mkdir(parents=True, mode=0o700)
    _key(data / "keys" / "root.key")
    recovery_key(data)
    db = sqlite3.connect(data / "journal.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE calls (n INTEGER, why TEXT)")
    db.executemany("INSERT INTO calls VALUES (?, ?)", [(i, "x" * 200) for i in range(5000)])
    db.commit()
    db.close()
    Vault(data).unlock().set("gh-token", SECRET, by="person:bake")
    (data / "plugins" / "agents").mkdir(parents=True)
    sqlite3.connect(data / "plugins" / "agents" / "agents.db").execute("CREATE TABLE a (x)").connection.close()
    (data / "plugins" / "agents" / "notes.json").write_text('{"n": 1}')
    (data / "tokens.json").write_text("{}")
    (data / "assets").mkdir()
    (data / "assets" / "big.bin").write_bytes(b"cache" * 100)
    (data / "workers.json.tmp").write_text("half written")
    if library:
        lib = root / "library"
        lib.mkdir()
        (lib / "file").symlink_to(REPO / "rook" / "plugins" / "file", target_is_directory=True)
        cfg["library"] = "./library"
    (root / "hub.yaml").write_text("# a comment\n" + yaml.safe_dump(cfg))
    return root / "hub.yaml"


def tree(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*"))
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))}


def rows(db: Path, sql: str):
    c = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


# -- sealing ----------------------------------------------------------------------

def seal(blob: bytes, key: bytes) -> bytes:
    out = io.BytesIO()
    w = bk.SealWriter(out, key)
    for i in range(0, len(blob), 777_777):
        w.write(blob[i:i + 777_777])
    w.close()
    return out.getvalue()


def unseal(sealed: bytes, key: bytes) -> bytes:
    return bk.SealReader(io.BytesIO(sealed), key).read()


@pytest.mark.parametrize("size", [0, 1, bk.CHUNK - 1, bk.CHUNK, bk.CHUNK + 1, 3 * bk.CHUNK + 5])
def test_seal_round_trips_across_chunk_edges(size):
    key = bk.seal_key(recovery_key_text())
    blob = os.urandom(size)
    sealed = seal(blob, key)
    assert sealed.startswith(bk.MAGIC) and unseal(sealed, key) == blob
    assert blob[:64] not in sealed or size == 0


def recovery_key_text() -> str:
    raw = base64.b32encode(os.urandom(20)).decode()
    return "-".join(raw[i:i + 4] for i in range(0, 32, 4))


def test_a_wrong_key_a_cut_or_a_change_never_opens():
    rk = recovery_key_text()
    key = bk.seal_key(rk)
    blob = os.urandom(2 * bk.CHUNK + 10)
    sealed = seal(blob, key)
    # however the key was written down, it is the same key
    assert bk.seal_key(rk.lower().replace("-", " ")) == key
    with pytest.raises(bk.BackupError, match="wrong recovery key"):
        unseal(sealed, bk.seal_key(recovery_key_text()))
    for cut in (len(sealed) - 1, len(sealed) - 100, len(bk.MAGIC) + 24 + 4 + bk.CHUNK + 17):
        with pytest.raises(bk.BackupError, match="cut short|damaged"):
            unseal(sealed[:cut], key)
    flipped = bytearray(sealed)
    flipped[len(sealed) // 2] ^= 1
    with pytest.raises(bk.BackupError, match="damaged"):
        unseal(bytes(flipped), key)
    with pytest.raises(bk.BackupError, match="after its end"):
        unseal(sealed + b"x", key)
    with pytest.raises(bk.BackupError, match="not a Rook backup"):
        unseal(b"PK\x03\x04" + sealed[4:], key)
    for bad in ("", "hunter2", "AAAA-BBBB", rk + "AAAA"):
        with pytest.raises(bk.BackupError, match="not a recovery key"):
            bk.seal_key(bad)


# -- a whole hub ------------------------------------------------------------------

def test_full_round_trip_restores_a_working_hub(tmp_path):
    cfg = make_hub(tmp_path / "old")
    old = cfg.parent / "data"
    usb = tmp_path / "usb"
    out = bk.run(config=cfg, data=old, library=cfg.parent / "library", spec=f"dir:{usb}")
    [name] = os.listdir(usb)
    assert bk.NAME.fullmatch(name) and out["name"] == name and out["pruned"] == []
    assert out["verified"]["databases"] == 3 and out["verified"]["hub"] == bk.hub_id_of(old)
    assert stat.S_IMODE((usb / name).stat().st_mode) == 0o600 and stat.S_IMODE(usb.stat().st_mode) == 0o700
    # nothing unsealed is left behind in the work folder
    assert list((old / bk.WORK).iterdir()) == []

    new = tmp_path / "new" / "hub.yaml"
    summary = bk.restore(usb / name, recovery_key(old), new)
    assert summary["hub"] == bk.hub_id_of(new.parent / "data") == bk.hub_id_of(old)
    restored, original = tree(new.parent / "data"), tree(old)
    # every file came back; the cache, temp files and work folder didn't
    assert set(restored) == set(original) - {"assets/big.bin", "workers.json.tmp"}
    for name_ in ("keys/root.key", "keys/recovery.key", "plugins/agents/notes.json", "tokens.json"):
        assert restored[name_] == original[name_]
    # databases are whole copies, the WAL folded in
    assert rows(new.parent / "data" / "journal.db", "SELECT count(*) FROM calls") == [(5000,)]
    # the vault came back as it was, and opens with the restored root key
    for table in ("secrets", "wraps"):
        assert rows(new.parent / "data" / "vault.db", f"SELECT * FROM {table}") == \
            rows(old / "vault.db", f"SELECT * FROM {table}")
    assert Vault(new.parent / "data").unlock().get("gh-token") == SECRET
    # the library came back as files, not links into a checkout
    lib = new.parent / "library" / "file"
    assert not lib.is_symlink() and (lib / "plugin.yaml").read_bytes() == \
        (REPO / "rook/plugins/file/plugin.yaml").read_bytes()
    assert not any(p.name == "__pycache__" for p in lib.rglob("*"))
    # the new config loads and points at the restored folders; it keeps the band key
    c = load_config(new)
    assert c.data == (new.parent / "data").resolve() and c.library == (new.parent / "library").resolve()
    assert c.transport["bands"][0]["psk"] == PSK
    for p in [new, *new.parent.rglob("*")]:
        mode = stat.S_IMODE(p.stat().st_mode) if not p.is_symlink() else 0
        assert mode in (0o600, 0o700), (p, oct(mode))
    # and the restored hub starts, with the old hub's id
    hub = Hub(c, transport=Loopback())

    async def start_stop():
        await hub.start()
        try:
            assert hub.hub_id == summary["hub"]
        finally:
            await hub.stop()
    go(start_stop())


def test_the_backup_holds_no_secret_in_the_clear(tmp_path):
    cfg = make_hub(tmp_path / "hub", library=False)
    data = cfg.parent / "data"
    path, manifest = bk.make(cfg, data, None)
    sealed = path.read_bytes()
    for secret in ((data / "keys" / "root.key").read_text().strip(), PSK, recovery_key(data), SECRET):
        assert secret.encode() not in sealed
    assert b"journal.db" not in sealed and b"calls" not in sealed      # names are sealed too
    assert manifest["library"] is None and "hub.yaml" in manifest["files"]
    assert {p.name for p in (data / bk.WORK).iterdir()} == {path.name}


def test_a_snapshot_is_consistent_while_the_hub_writes(tmp_path):
    cfg = make_hub(tmp_path / "hub", library=False)
    data = cfg.parent / "data"
    stop = threading.Event()

    def writer():
        db = sqlite3.connect(data / "journal.db", timeout=10)
        n = 0
        while not stop.is_set():
            with db:
                db.execute("INSERT INTO calls VALUES (?, ?)", (10_000 + n, "y"))
                db.execute("INSERT INTO calls VALUES (?, ?)", (-10_000 - n, "y"))   # pairs, in one transaction
            n += 1
        db.close()
    t = threading.Thread(target=writer)
    t.start()
    try:
        path, _ = bk.make(cfg, data, None)
    finally:
        stop.set()
        t.join(10)
    dest = tmp_path / "check"
    m = bk.unpack(path, bk.seal_key(recovery_key(data)), dest)
    assert bk.check(dest, m)["databases"] == 3
    n, total = rows(dest / "data" / "journal.db", "SELECT count(*), sum(n) FROM calls WHERE abs(n) >= 10000")[0]
    assert n % 2 == 0 and (total or 0) == 0      # pairs sum to 0: never half a transaction


def test_restore_refuses_to_overwrite_and_unsafe_or_mismatched_backups(tmp_path):
    cfg = make_hub(tmp_path / "hub", library=False)
    data = cfg.parent / "data"
    path, _ = bk.make(cfg, data, None)
    rk = recovery_key(data)
    with pytest.raises(bk.BackupError, match="never overwrites"):
        bk.restore(path, rk, cfg)                       # over the live hub
    busy = tmp_path / "busy"
    (busy / "data").mkdir(parents=True)
    with pytest.raises(bk.BackupError, match="exists"):
        bk.restore(path, rk, busy / "hub.yaml")
    with pytest.raises(bk.BackupError, match="wrong recovery key"):
        bk.restore(path, recovery_key_text(), tmp_path / "x" / "hub.yaml")
    assert not (tmp_path / "x" / "data").exists() and not (tmp_path / "x" / "hub.yaml").exists()

    key = bk.seal_key(rk)

    def crafted(entries, manifest=None) -> Path:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for name, body, kind in entries:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.SYMTYPE:
                    info.linkname = "/etc/passwd"
                    tar.addfile(info)
                else:
                    info.size = len(body)
                    tar.addfile(info, io.BytesIO(body))
            if manifest is not None:
                body = json.dumps(manifest).encode()
                info = tarfile.TarInfo(bk.MANIFEST)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        p = tmp_path / f"crafted-{len(os.listdir(tmp_path))}.rookbak"
        p.write_bytes(seal(raw.getvalue(), key))
        return p
    for entries in ([("../evil", b"x", tarfile.REGTYPE)], [("/etc/evil", b"x", tarfile.REGTYPE)],
                    [("data/../../evil", b"x", tarfile.REGTYPE)], [("data/link", b"", tarfile.SYMTYPE)],
                    [("other/x", b"x", tarfile.REGTYPE)], [("data", b"", tarfile.DIRTYPE)]):
        dest = tmp_path / f"d{len(os.listdir(tmp_path))}"
        with pytest.raises(bk.BackupError, match="unexpected entry"):
            bk.unpack(crafted(entries), key, dest)
    assert not (tmp_path / "evil").exists()
    lie = {"format": 1, "hub": "0" * 16, "created": 0, "library": None,
           "files": {"data/a": {"size": 1, "sha256": "0" * 64}}}
    with pytest.raises(bk.BackupError, match="doesn't match its manifest"):
        bk.unpack(crafted([("data/a", b"x", tarfile.REGTYPE)], lie), key, tmp_path / "lie")
    with pytest.raises(bk.BackupError, match="no manifest"):
        bk.unpack(crafted([("data/a", b"x", tarfile.REGTYPE)]), key, tmp_path / "nomanifest")


def test_a_hub_without_a_recovery_key_does_not_back_up(tmp_path):
    cfg = make_hub(tmp_path / "hub", library=False)
    (cfg.parent / "data" / "keys" / "recovery.key").unlink()
    with pytest.raises(bk.BackupError, match="no recovery key yet"):
        bk.make(cfg, cfg.parent / "data", None)
    assert not (cfg.parent / "data" / "keys" / "recovery.key").exists()     # never made behind the owner's back


# -- targets and retention ---------------------------------------------------------

def names(hub: str, n: int) -> list[str]:
    return [f"rook-hub-{hub}-2026103{i % 10}T0{i % 10}0000Z.rookbak" for i in range(n)]


def test_retention_keeps_the_newest_of_this_hub_only(tmp_path):
    t = bk.DirTarget(str(tmp_path / "usb"))
    src = tmp_path / "blob"
    src.write_bytes(b"sealed")
    mine, theirs = names("a" * 16, 5), names("b" * 16, 3)
    for n in mine + theirs:
        t.put(src, n)
    (tmp_path / "usb" / "notes.txt").write_text("not a backup")
    assert [b["name"] for b in bk.backups(t, "a" * 16)] == sorted(mine, reverse=True)
    assert bk.prune(t, "a" * 16, 2) == sorted(mine, reverse=True)[2:]
    assert sorted(os.listdir(tmp_path / "usb")) == sorted(sorted(mine)[-2:] + theirs + ["notes.txt"])
    assert bk.prune(t, "a" * 16, 0) == sorted(mine)[-2:-1]        # keep never goes below one
    with pytest.raises(bk.BackupError, match="absolute"):
        bk.DirTarget("relative/path")
    for spec, match in (("", "no backup target"), ("ftp:x", "unknown"), ("rclone:nocolon", "remote:path"),
                        ("worker:janitor", "running hub")):
        with pytest.raises(bk.BackupError, match=match):
            bk.target(spec)


def store_call(tmp_path: Path):
    """``call(cap, args)`` into the real backup-store plugin, in a worker host."""
    folder = tmp_path / "store"

    def call(cap, args):
        async def body():
            host = Host(os_name="linux", data_root=tmp_path / "wdata")
            await host.load(REPO / "rook" / "plugins" / "backup-store")
            await host.set_settings("backup-store", local={"folder": str(folder)})
            try:
                return await host.call(cap, args, Caller(person="bake", band="b1", why=WHY))
            finally:
                await host.close()
        return go(body(), 30)
    return call, folder


def test_a_worker_target_stores_through_backup_store(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "PART", 64 * 1024)        # several parts per backup
    cfg = make_hub(tmp_path / "hub", library=False)
    data = cfg.parent / "data"
    call, folder = store_call(tmp_path)
    t = bk.WorkerTarget("janitor", call)
    out = bk.run(config=cfg, data=data, library=None, spec="worker:janitor", call=call, keep=1)
    assert out["target"] == "worker:janitor" and out["verified"]["databases"] == 3
    [kept] = os.listdir(folder)
    assert kept == out["name"] and stat.S_IMODE((folder / kept).stat().st_mode) == 0o600
    # the newest backup of a hub never goes, whoever asks
    with pytest.raises(CallError, match="newest backup"):
        call("backup-store.delete", {"name": kept})
    # a bad upload is dropped, never kept under the name
    other = names(bk.hub_id_of(data), 1)[0]
    call("backup-store.put", {"name": other, "offset": 0, "data": base64.b64encode(b"abc").decode()})
    with pytest.raises(CallError, match="offset 9"):
        call("backup-store.put", {"name": other, "offset": 9, "data": ""})
    with pytest.raises(CallError, match="sha256 doesn't match"):
        call("backup-store.put", {"name": other, "offset": 3, "data": "", "sha256": "0" * 64})
    assert os.listdir(folder) == [kept]
    with pytest.raises(CallError, match="is here already"):
        call("backup-store.put", {"name": kept, "offset": 0, "data": ""})
    for bad in ("../x.rookbak", "rook-hub-zz-1.rookbak"):
        with pytest.raises(CallError):
            call("backup-store.get", {"name": bad})
    # restore on a fresh machine: copy the file off that worker
    summary = bk.restore(folder / kept, recovery_key(data), tmp_path / "new" / "hub.yaml")
    assert summary["hub"] == bk.hub_id_of(data)
    assert [b["name"] for b in bk.backups(t)] == [kept]


def test_an_rclone_target(tmp_path):
    """rclone, faked: a program on PATH that maps fake:<path> to a folder."""
    bins, remote = tmp_path / "bin", tmp_path / "remote"
    bins.mkdir()
    remote.mkdir()
    fake = bins / "rclone"
    fake.write_text(f"""#!{sys.executable}
import json, os, shutil, sys
root = {str(remote)!r}
def local(p):
    assert p.startswith("fake:"), p
    return os.path.join(root, p[5:].lstrip("/"))
cmd, *rest = sys.argv[1:]
if cmd == "copyto":
    a, b = rest
    a = local(a) if a.startswith("fake:") else a
    b = local(b) if b.startswith("fake:") else b
    os.makedirs(os.path.dirname(b), exist_ok=True)
    shutil.copyfile(a, b)
elif cmd == "lsjson":
    d = local(rest[-1])
    print(json.dumps([{{"Name": n, "Size": os.path.getsize(os.path.join(d, n))}} for n in os.listdir(d)]
                     if os.path.isdir(d) else []))
elif cmd == "deletefile":
    os.unlink(local(rest[0]))
else:
    sys.exit("unknown " + cmd)
""")
    fake.chmod(0o755)
    env_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bins}{os.pathsep}{env_path}"
    try:
        cfg = make_hub(tmp_path / "hub", library=False)
        data = cfg.parent / "data"
        first = bk.run(config=cfg, data=data, library=None, spec="rclone:fake:rook", keep=1, now=1_800_000_000)
        second = bk.run(config=cfg, data=data, library=None, spec="rclone:fake:rook", keep=1, now=1_800_000_100)
        assert second["pruned"] == [first["name"]] and os.listdir(remote / "rook") == [second["name"]]
        got = tmp_path / "got.rookbak"
        assert bk.fetch("rclone:fake:rook", None, got) == second["name"]
        assert bk.verify(got, data)["hub"] == bk.hub_id_of(data)
        with pytest.raises(bk.BackupError, match="rclone copyto failed"):
            bk.RcloneTarget("fake:rook").get("rook-hub-" + "0" * 16 + "-20261031T000000Z.rookbak", got)
    finally:
        os.environ["PATH"] = env_path
    with pytest.raises(bk.BackupError, match="not installed"):
        bk.RcloneTarget("x:y", binary=str(tmp_path / "no-such-rclone")).list()


# -- the hub caps, the job, the CLI -------------------------------------------------

def test_the_backup_plugin_and_its_job_template_validate():
    for folder in (REPO / "rook" / "core2" / "hub" / "backup", REPO / "rook" / "plugins" / "backup-store"):
        _, issues = check_manifest(folder)
        assert not [i for i in issues if i.level == "error"], issues
    job = yaml.safe_load((REPO / "rook/core2/hub/backup/plugin.yaml").read_text())["jobs"]["nightly"]
    assert job["call"] == "backup.run" and "cron" in job["trigger"] and not job.get("enabled")


def test_hub_caps_are_for_owners_and_run_through_dispatch(tmp_path):
    cfg_path = make_hub(tmp_path / "hub", library=False)
    usb = tmp_path / "usb"
    raw = yaml.safe_load(cfg_path.read_text())
    raw["users"]["ann"] = {"preset": "adult"}      # every grant, but not an owner
    raw["settings"] = {"backup": {"target": f"dir:{usb}", "keep": 1}}
    raw.pop("transport")
    cfg_path.write_text(yaml.safe_dump(raw))

    async def body():
        hub = Hub(load_config(cfg_path), transport=Loopback())
        await hub.start()
        try:
            owner, kid = hub.add_token(HubCaller("person:bake")), hub.add_token(HubCaller("person:ann"))

            def post(token, cap, args=None):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{hub.doors[0].port}/call", method="POST",
                    data=json.dumps({"worker": "rook", "cap": cap, "args": args or {}, "why": WHY}).encode(),
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())
            first = await asyncio.to_thread(post, owner, "backup.run")
            assert first["ok"], first
            second = await asyncio.to_thread(post, owner, "backup.run")
            assert second["ok"] and second["result"]["pruned"] in ([], [first["result"]["name"]])
            listed = await asyncio.to_thread(post, owner, "backup.list")
            assert listed["ok"] and [b["name"] for b in listed["result"]["backups"]] == [second["result"]["name"]]
            checked = await asyncio.to_thread(post, owner, "backup.verify")
            assert checked["ok"] and checked["result"]["hub"] == hub.hub_id
            refused = await asyncio.to_thread(post, kid, "backup.list")
            assert not refused["ok"] and "owner" in refused["error"]
            entries = [e for e in hub.journal.entries("general") if e.get("cap") == "backup.run"]
            assert len(entries) == 2 and all(e["why"] == WHY for e in entries)
            got = await asyncio.to_thread(post, owner, "hub.manifests", {"section": "jobs"})
            manifests = {m["plugin"]: m for m in got["result"]["manifests"]}
            assert "nightly" in manifests["backup"]["jobs"]
        finally:
            await hub.stop()
    go(body(), 90)


def test_cli_backup_and_restore_on_a_fresh_machine(tmp_path):
    cfg = make_hub(tmp_path / "hub")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), os.environ.get("PYTHONPATH", "")])}

    def cli(*args, stdin=None):
        return subprocess.run([sys.executable, "-m", "rook.core2.hub", *args], capture_output=True, text=True,
                              timeout=120, input=stdin, env=env, cwd=tmp_path)
    usb = tmp_path / "usb"
    r = cli("--config", str(cfg), "backup", "--target", f"dir:{usb}")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["verified"]["hub"] == bk.hub_id_of(cfg.parent / "data")
    r = cli("--config", str(cfg), "backup", "verify", "--target", f"dir:{usb}")
    assert r.returncode == 0 and json.loads(r.stdout)["hub"], r.stderr
    r = cli("--config", str(cfg), "backup", "--target", "worker:janitor")
    assert r.returncode == 1 and "running hub" in r.stderr
    rk = recovery_key(cfg.parent / "data")
    r = cli("--config", str(tmp_path / "new" / "hub.yaml"), "restore", "--target", f"dir:{usb}", stdin="nope\n")
    assert r.returncode == 1 and "not a recovery key" in r.stderr and not (tmp_path / "new" / "hub.yaml").exists()
    assert [p.name for p in (tmp_path / "new").iterdir()] == []        # the fetched copy is gone
    r = cli("--config", str(tmp_path / "new" / "hub.yaml"), "restore", "--target", f"dir:{usb}", stdin=rk + "\n")
    assert r.returncode == 0, r.stderr
    assert "Restored hub" in r.stdout and rk not in r.stdout + r.stderr
    assert load_config(tmp_path / "new" / "hub.yaml").library == (tmp_path / "new" / "library").resolve()
    r = cli("--config", str(tmp_path / "new" / "hub.yaml"), "recovery-key")
    assert r.stdout.strip() == rk
