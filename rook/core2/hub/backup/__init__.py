"""Off-site hub backup (castling-backup, switchover gate R5).

A backup is one file, ``rook-hub-<hub>-<UTC time>.rookbak``: a tar.gz of the
hub's config file, its data folder and its plugin library, sealed under a key
derived from the owner's recovery key. Only the recovery key restores it; the
hub needs no person to make one, because it keeps the recovery key in
``<data>/keys/recovery.key`` (the owner can see it again while the hub lives).

What goes in:

- ``hub.yaml``: the config (it holds the band key).
- ``data/``: every file in the data folder. Each SQLite database is copied
  with SQLite's online backup, so it is consistent while the hub runs (one
  database at a time: two databases are not one snapshot). ``vault.db`` is
  copied as it is: its values are sealed already. Left out: ``-wal``/``-shm``
  files (the copies are whole), ``assets/`` (a download cache), ``releases/``
  (core release tarballs: ``update.rollout`` makes them again) and the work
  folder ``.backup/``.
- ``library/``: the trusted plugin folders, following links (a plugin
  linked from a checkout is backed up as its files), wherever the library is.
- ``rook-backup.json`` (last): format, hub id, time, whether there is a
  library, and every file's size and SHA-256.

Sealing: XChaCha20-Poly1305 secretstream (libsodium, through PyNaCl) in 1 MiB
chunks, so a backup of any size streams in constant memory, and a cut-off or
re-ordered file fails to open. The key is BLAKE2b(key = the recovery key's
160 bits, person = ``rook-backup-1``); the stream header makes each file's
nonces fresh. File layout: ``ROOKBAK1``, the 24-byte stream header, then
chunks as a 4-byte big-endian length and the ciphertext; the last chunk
carries the FINAL tag.

Nothing unsealed leaves the data folder: database copies and verify-restores
are made in ``<data>/.backup/`` (0700, files 0600) and removed straight
after; the tar streams straight into the sealer.

Targets (setting ``target``): ``dir:/path`` (a folder: a USB disk, a mount),
``worker:<name>`` (the ``backup-store`` plugin on another worker, over the
band) or ``rclone:<remote>:<path>`` (anything rclone reaches: S3, Google
Drive, B2, SFTP; rclone's own config holds its credentials). ``keep`` newest
backups of this hub stay on the target; older ones are deleted after a good
upload.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

MAGIC = b"ROOKBAK1"
FORMAT = 1
CHUNK = 1 << 20                     # plaintext bytes per sealed chunk
MAX_CHUNK = CHUNK + 1024            # a longer length field means a damaged file
PERSON = b"rook-backup-1"
SUFFIX = ".rookbak"
NAME = re.compile(r"rook-hub-([0-9a-f]{8,64})-(\d{8}T\d{6}Z)\.rookbak")
WORK = ".backup"                     # <data>/.backup: private scratch, never in a backup
SKIP_TOP = {WORK, "assets", "releases"}   # releases: core release tarballs, rebuilt by update.rollout
SKIP_SUFFIX = ("-wal", "-shm", "-journal", ".tmp")
SQLITE = b"SQLite format 3\x00"
MANIFEST = "rook-backup.json"
KEEP = 7
PART = 4 << 20                       # bytes per worker-target call (base64 ~5.6 MB)
RCLONE_SECS = 3600
_ONE = threading.Lock()              # one backup at a time in this process


class BackupError(Exception):
    """A backup, verify or restore that can't go on; the message says why."""


# -- the key --------------------------------------------------------------------

def seal_key(recovery_key: str) -> bytes:
    """The backup key from a recovery key (``ABCD-EFGH-...``, 32 base32
    characters; spaces, dashes and case don't matter)."""
    text = re.sub(r"[\s-]", "", str(recovery_key or "")).upper()
    try:
        raw = base64.b32decode(text)
    except ValueError:
        raw = b""
    if len(raw) != 20:
        raise BackupError("that is not a recovery key (32 letters and digits, in groups of four)")
    return hashlib.blake2b(raw, digest_size=32, person=PERSON).digest()


def hub_recovery_key(data: Path) -> str:
    """The recovery key this hub keeps. A hub without one has never shown its
    owner one, so it can't make a backup they could restore: refuse."""
    path = Path(data) / "keys" / "recovery.key"
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise BackupError("this hub has no recovery key yet: run the recovery-key command, "
                          "save the key, then back up") from None


def hub_id_of(data: Path) -> str:
    from nacl.signing import SigningKey

    from rook.core2 import wire
    try:
        seed = bytes.fromhex((Path(data) / "keys" / "root.key").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        raise BackupError(f"no hub root key in {data}/keys") from None
    return wire.key_id(bytes(SigningKey(seed).verify_key))


# -- sealing --------------------------------------------------------------------

def _ss():
    from nacl import bindings
    return bindings


class SealWriter(io.RawIOBase):
    """A write-only file that seals what is written to ``out``."""

    def __init__(self, out, key: bytes):
        b = _ss()
        self.out, self.buf = out, bytearray()
        self.state = b.crypto_secretstream_xchacha20poly1305_state()
        out.write(MAGIC + b.crypto_secretstream_xchacha20poly1305_init_push(self.state, key))

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.buf += data
        while len(self.buf) >= CHUNK:
            self._push(bytes(self.buf[:CHUNK]), final=False)
            del self.buf[:CHUNK]
        return len(data)

    def _push(self, m: bytes, final: bool) -> None:
        b = _ss()
        tag = (b.crypto_secretstream_xchacha20poly1305_TAG_FINAL if final
               else b.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE)
        c = b.crypto_secretstream_xchacha20poly1305_push(self.state, m, MAGIC, tag)
        self.out.write(struct.pack(">I", len(c)) + c)

    def close(self) -> None:
        if not self.closed:
            self._push(bytes(self.buf), final=True)
            self.buf.clear()
        super().close()


class SealReader(io.RawIOBase):
    """A read-only file of what ``src`` sealed. Raises BackupError on a wrong
    key, a damaged file or one cut short."""

    def __init__(self, src, key: bytes):
        b = _ss()
        self.src, self.buf, self.done = src, b"", False
        head = src.read(len(MAGIC) + b.crypto_secretstream_xchacha20poly1305_HEADERBYTES)
        if head[:len(MAGIC)] != MAGIC:
            raise BackupError("not a Rook backup")
        self.state = b.crypto_secretstream_xchacha20poly1305_state()
        try:
            b.crypto_secretstream_xchacha20poly1305_init_pull(self.state, head[len(MAGIC):], key)
        except Exception:
            raise BackupError("not a Rook backup (header cut short)") from None
        self.first = True

    def readable(self) -> bool:
        return True

    def _pull(self) -> None:
        b = _ss()
        n = self.src.read(4)
        if len(n) < 4:
            raise BackupError("the backup is cut short")
        size = struct.unpack(">I", n)[0]
        c = self.src.read(size) if size <= MAX_CHUNK else b""
        if len(c) != size or not c:
            raise BackupError("the backup is damaged or cut short")
        try:
            m, tag = b.crypto_secretstream_xchacha20poly1305_pull(self.state, c, MAGIC)
        except Exception:
            raise BackupError("wrong recovery key, or the backup is damaged" if self.first
                              else "the backup is damaged") from None
        self.first = False
        self.buf += m
        if tag == b.crypto_secretstream_xchacha20poly1305_TAG_FINAL:
            if self.src.read(1):
                raise BackupError("the backup has data after its end")
            self.done = True

    def readinto(self, out) -> int:
        while not self.buf and not self.done:
            self._pull()
        n = min(len(out), len(self.buf))
        out[:n] = self.buf[:n]
        self.buf = self.buf[n:]
        return n


# -- making one -----------------------------------------------------------------

def _work(data: Path) -> Path:
    w = Path(data) / WORK
    w.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(w, 0o700)
    return w


def _files(root: Path, top: bool, skip: Path | None = None) -> Iterator[Path]:
    """Regular files under ``root``, sorted. The data folder (``top``) skips
    its cache and work folders and never follows links; the library follows
    them, so plugin folders linked from a checkout are backed up as files."""
    for dirpath, dirs, files in os.walk(root, followlinks=not top):
        rel = Path(dirpath).relative_to(root)
        if top and rel == Path("."):
            dirs[:] = [d for d in dirs if d not in SKIP_TOP]
        if skip is not None:
            dirs[:] = [d for d in dirs if (Path(dirpath) / d).resolve() != skip]
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git"))
        for f in sorted(files):
            p = Path(dirpath) / f
            if f.endswith(SKIP_SUFFIX) or (top and p.is_symlink()) or not p.is_file():
                continue
            yield p


def _is_sqlite(p: Path) -> bool:
    with open(p, "rb") as f:
        return f.read(16) == SQLITE


def _db_copy(src: Path, dst: Path) -> None:
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    a = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    b = sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()


class _Hashing:
    def __init__(self, f):
        self.f, self.h, self.n = f, hashlib.sha256(), 0

    def read(self, n=-1):
        d = self.f.read(n)
        self.h.update(d)
        self.n += len(d)
        return d


def _add(tar: tarfile.TarFile, name: str, f, size: int, mtime: float, files: dict) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = size, int(mtime), 0o600
    hf = _Hashing(f)
    tar.addfile(info, hf)
    if hf.n != size:
        raise BackupError(f"{name} changed size while it was copied; try again")
    files[name] = {"size": size, "sha256": hf.h.hexdigest()}


def write_backup(out, *, config: Path, data: Path, library: Path | None, key: bytes,
                 hub_id: str, now: float | None = None) -> dict:
    """Stream a sealed backup of this hub to ``out`` (a binary file). Returns
    its manifest."""
    data, now = Path(data), time.time() if now is None else now
    work = _work(data)
    files: dict[str, dict] = {}
    lib = Path(library).resolve() if library is not None and Path(library).is_dir() else None
    sealer = SealWriter(out, key)
    with tarfile.open(fileobj=sealer, mode="w|gz", format=tarfile.PAX_FORMAT) as tar:
        cfg = Path(config).read_bytes()
        _add(tar, "hub.yaml", io.BytesIO(cfg), len(cfg), Path(config).stat().st_mtime, files)
        roots = [("data", data, True)] + ([("library", lib, False)] if lib else [])
        for prefix, root, top in roots:
            for p in _files(root, top, skip=lib if top else None):
                name = f"{prefix}/{p.relative_to(root).as_posix()}"
                if _is_sqlite(p):
                    tmp = work / f"db-{os.getpid()}-{time.time_ns()}.copy"
                    try:
                        _db_copy(p, tmp)
                        with open(tmp, "rb") as f:
                            _add(tar, name, f, tmp.stat().st_size, p.stat().st_mtime, files)
                    finally:
                        with suppress(FileNotFoundError):
                            tmp.unlink()
                else:
                    with open(p, "rb") as f:
                        _add(tar, name, f, os.fstat(f.fileno()).st_size, p.stat().st_mtime, files)
        manifest = {"format": FORMAT, "hub": hub_id, "created": round(now, 3),
                    "library": "library" if lib else None, "files": files}
        body = json.dumps(manifest, indent=1, sort_keys=True).encode()
        _add(tar, MANIFEST, io.BytesIO(body), len(body), now, {})
    sealer.close()
    return manifest


def backup_name(hub_id: str, now: float) -> str:
    return f"rook-hub-{hub_id}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}{SUFFIX}"


def make(config: Path, data: Path, library: Path | None, now: float | None = None) -> tuple[Path, dict]:
    """A sealed backup of this hub in ``<data>/.backup/``. Returns (file, manifest)."""
    now = time.time() if now is None else now
    data = Path(data)
    key, hub = seal_key(hub_recovery_key(data)), hub_id_of(data)
    path = _work(data) / backup_name(hub, now)
    part = path.with_name(path.name + ".part")
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            manifest = write_backup(out, config=config, data=data, library=library, key=key,
                                    hub_id=hub, now=now)
            out.flush()
            os.fsync(out.fileno())
        os.replace(part, path)
    except BaseException:
        with suppress(FileNotFoundError):
            part.unlink()
        raise
    return path, manifest


# -- opening one ----------------------------------------------------------------

def _safe(name: str) -> bool:
    parts = name.split("/")
    return (bool(name) and not name.startswith("/") and "\\" not in name
            and all(p not in ("", ".", "..") for p in parts)
            and (name in ("hub.yaml", MANIFEST) or parts[0] in ("data", "library") and len(parts) > 1))


def _mkdirs(base: Path, path: Path) -> None:
    """``path`` and every folder between it and ``base``, each 0700."""
    d = base
    for part in path.relative_to(base).parts:
        d = d / part
        if not d.is_dir():
            d.mkdir(mode=0o700)


def unpack(src: Path, key: bytes, dest: Path) -> dict:
    """Open the sealed backup ``src`` into ``dest`` (which must be empty or
    missing): ``hub.yaml``, ``data/``, ``library/``. Checks every file
    against the manifest. Returns the manifest."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise BackupError(f"{dest} is not empty: restore into a new folder")
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    got: dict[str, dict] = {}
    manifest = None
    with open(src, "rb") as raw:
        reader = io.BufferedReader(SealReader(raw, key), CHUNK)
        try:
            with tarfile.open(fileobj=reader, mode="r|gz") as tar:
                for m in tar:
                    if not m.isfile() or not _safe(m.name) or m.name in got:
                        raise BackupError(f"the backup holds an unexpected entry: {m.name!r}")
                    f = tar.extractfile(m)
                    if m.name == MANIFEST:
                        manifest = json.loads(f.read(1 << 24))
                        continue
                    out = dest / m.name
                    _mkdirs(dest, out.parent)
                    h, n = hashlib.sha256(), 0
                    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as w:
                        while chunk := f.read(CHUNK):
                            h.update(chunk)
                            n += len(chunk)
                            w.write(chunk)
                    os.utime(out, (m.mtime, m.mtime))
                    got[m.name] = {"size": n, "sha256": h.hexdigest()}
        except (tarfile.TarError, EOFError, OSError, json.JSONDecodeError) as e:
            if isinstance(e, FileExistsError):
                raise BackupError(f"the backup holds {e.filename} twice") from None
            raise BackupError(f"the backup is damaged: {e}") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("the backup has no manifest it can read (damaged, or from a newer Rook)")
    if got != manifest.get("files"):
        missing = sorted(set(manifest.get("files") or {}) ^ set(got))
        bad = missing or sorted(k for k in got if got[k] != manifest["files"].get(k))
        raise BackupError(f"the backup doesn't match its manifest: {', '.join(bad[:5])}")
    return manifest


def _config_text(original: str, base: Path, data: Path, library: Path | None) -> str:
    """The backed-up config, pointing at the restored data and library
    (relative to ``base``, the new config's folder)."""
    raw = yaml.safe_load(original) or {}
    if not isinstance(raw, dict):
        raise BackupError("the backup's config is not a mapping")
    raw["data"] = os.path.relpath(data, base)
    if library is not None:
        raw["library"] = os.path.relpath(library, base)
    else:
        raw.pop("library", None)
    return ("# Rook hub, restored from a backup. It holds the band key: keep it private.\n"
            + yaml.safe_dump(raw, sort_keys=False))


def _write_new(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def check(dest: Path, manifest: dict) -> dict:
    """What a restore needs, on a folder :func:`unpack` made: the config
    loads, the root key is this backup's hub, and every database passes
    SQLite's quick check."""
    from ..runtime import load_config
    dest = Path(dest)
    if hub_id_of(dest / "data") != manifest["hub"]:
        raise BackupError("the backup's root key is not its hub's")
    lib = dest / manifest["library"] if manifest.get("library") else None
    probe = dest / ".hub.check.yaml"
    try:
        _write_new(probe, _config_text((dest / "hub.yaml").read_text(encoding="utf-8"), dest, dest / "data", lib))
        load_config(probe)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise BackupError(f"the backup's config doesn't load: {e}") from None
    finally:
        probe.unlink(missing_ok=True)
    dbs = 0
    for name in manifest["files"]:
        p = dest / name
        if _is_sqlite(p):
            db = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
            try:
                ok = db.execute("PRAGMA quick_check").fetchone()[0]
            except sqlite3.DatabaseError as e:
                ok = str(e)
            finally:
                db.close()
            if ok != "ok":
                raise BackupError(f"{name} fails SQLite's check: {ok}")
            dbs += 1
    return {"hub": manifest["hub"], "created": manifest["created"], "files": len(manifest["files"]),
            "databases": dbs, "bytes": sum(f["size"] for f in manifest["files"].values())}


def env_names(raw: Any) -> list[str]:
    """Environment variables a config reads (``psk_env``, ``key_env``, ...):
    their values were never in the file, so a backup doesn't hold them."""
    if isinstance(raw, dict):
        return sorted({v for k, v in raw.items() if k.endswith("_env") and isinstance(v, str)}
                      | {n for v in raw.values() for n in env_names(v)})
    if isinstance(raw, list):
        return sorted({n for v in raw for n in env_names(v)})
    return []


def restore(src: Path, recovery_key: str, config: Path) -> dict:
    """Restore a hub on a fresh machine: open ``src`` beside ``config``
    (``data/``, ``library/``), check it, then write ``config`` pointing at
    them. Refuses to overwrite anything."""
    config = Path(config).absolute()
    folder = config.parent
    for p in (config, folder / "data", folder / "library"):
        if p.exists():
            raise BackupError(f"{p} exists: restore makes a new hub folder, it never overwrites one")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = folder / f".restore-{os.getpid()}"
    try:
        manifest = unpack(src, seal_key(recovery_key), stage)
        summary = check(stage, manifest)
        original = (stage / "hub.yaml").read_text(encoding="utf-8")
        os.replace(stage / "data", folder / "data")
        if (stage / "library").exists():
            os.replace(stage / "library", folder / "library")
        lib = folder / manifest["library"] if manifest.get("library") else None
        _write_new(config, _config_text(original, folder, folder / "data", lib))
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {**summary, "env": env_names(yaml.safe_load(original))}


def verify(src: Path, data: Path) -> dict:
    """Restore ``src`` into a private scratch folder under ``data`` with this
    hub's recovery key, check it, and remove it again."""
    data = Path(data)
    key = seal_key(hub_recovery_key(data))
    scratch = _work(data) / f"verify-{os.getpid()}-{time.time_ns()}"
    try:
        manifest = unpack(src, key, scratch)
        return check(scratch, manifest)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# -- targets --------------------------------------------------------------------

class DirTarget:
    """A folder (a USB disk, a network mount)."""

    def __init__(self, path: str):
        self.path = Path(os.path.expanduser(path))
        if not self.path.is_absolute():
            raise BackupError(f"dir target needs an absolute path, not {path!r}")
        self.label = f"dir:{self.path}"

    def put(self, src: Path, name: str) -> None:
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        part = self.path / (name + ".part")
        try:
            shutil.copyfile(src, part)
            os.chmod(part, 0o600)
            with open(part, "rb") as f:
                os.fsync(f.fileno())
            os.replace(part, self.path / name)
        except BaseException:
            with suppress(FileNotFoundError):
                part.unlink()
            raise

    def list(self) -> list[dict]:
        if not self.path.is_dir():
            return []
        return [{"name": p.name, "size": p.stat().st_size} for p in self.path.iterdir()
                if NAME.fullmatch(p.name) and p.is_file()]

    def get(self, name: str, dst: Path) -> None:
        shutil.copyfile(self.path / name, dst)

    def delete(self, name: str) -> None:
        (self.path / name).unlink(missing_ok=True)


class WorkerTarget:
    """Another worker on the band, through its ``backup-store`` plugin.
    ``call(cap, args)`` runs a cap on that worker (the hub's dispatch)."""

    def __init__(self, worker: str, call: Callable[[str, dict], Any] | None):
        if call is None:
            raise BackupError("a worker target needs the running hub: use the backup.run cap")
        self.worker, self.call = worker, call
        self.label = f"worker:{worker}"

    def put(self, src: Path, name: str) -> None:
        h = hashlib.sha256()
        with open(src, "rb") as f:
            offset = 0
            while True:
                chunk = f.read(PART)
                h.update(chunk)
                last = len(chunk) < PART
                self.call("backup-store.put", {"name": name, "offset": offset,
                                               "data": base64.b64encode(chunk).decode("ascii"),
                                               **({"sha256": h.hexdigest()} if last else {})})
                offset += len(chunk)
                if last:
                    return

    def list(self) -> list[dict]:
        return [b for b in self.call("backup-store.list", {})["backups"] if NAME.fullmatch(b["name"])]

    def get(self, name: str, dst: Path) -> None:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            offset = 0
            while True:
                got = self.call("backup-store.get", {"name": name, "offset": offset, "size": PART})
                chunk = base64.b64decode(got["data"])
                out.write(chunk)
                offset += len(chunk)
                if got.get("eof") or not chunk:
                    return

    def delete(self, name: str) -> None:
        self.call("backup-store.delete", {"name": name})


class RcloneTarget:
    """Any rclone remote (``rclone:<remote>:<path>``): S3, Google Drive, B2,
    SFTP and the rest. rclone's own config (``rclone config``) holds the
    remote's credentials; nothing unsealed goes to it."""

    def __init__(self, remote: str, binary: str = "rclone"):
        if ":" not in remote or remote.startswith("-"):
            raise BackupError(f"rclone target is remote:path, not {remote!r}")
        self.remote, self.bin = remote.rstrip("/"), binary
        self.label = f"rclone:{remote}"

    def _run(self, *argv: str, timeout: float = RCLONE_SECS) -> str:
        try:
            p = subprocess.run([self.bin, *argv], capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise BackupError("rclone is not installed on the hub") from None
        except subprocess.TimeoutExpired:
            raise BackupError(f"rclone {argv[0]} took longer than {timeout:g}s") from None
        if p.returncode != 0:
            raise BackupError(f"rclone {argv[0]} failed: {(p.stderr or p.stdout).strip()[-300:]}")
        return p.stdout

    def _at(self, name: str) -> str:
        return f"{self.remote}/{name}"

    def put(self, src: Path, name: str) -> None:
        self._run("copyto", str(src), self._at(name))

    def list(self) -> list[dict]:
        out = self._run("lsjson", "--files-only", self.remote, timeout=300)
        return [{"name": e["Name"], "size": e.get("Size", 0)} for e in json.loads(out or "[]")
                if NAME.fullmatch(e.get("Name", ""))]

    def get(self, name: str, dst: Path) -> None:
        self._run("copyto", self._at(name), str(dst))
        os.chmod(dst, 0o600)

    def delete(self, name: str) -> None:
        self._run("deletefile", self._at(name), timeout=300)


def target(spec: str, call: Callable[[str, dict], Any] | None = None):
    """A target from its setting: ``dir:/path``, ``worker:<name>`` or
    ``rclone:<remote>:<path>``."""
    kind, _, rest = str(spec or "").partition(":")
    if not rest:
        raise BackupError("no backup target: set backup's target (dir:/path, worker:<name> or rclone:<remote>:<path>)")
    if kind == "dir":
        return DirTarget(rest)
    if kind == "worker":
        return WorkerTarget(rest, call)
    if kind == "rclone":
        return RcloneTarget(rest)
    raise BackupError(f"unknown backup target kind {kind!r}: dir, worker or rclone")


def backups(t, hub_id: str | None = None) -> list[dict]:
    """The target's backups (of ``hub_id``, if given), newest first."""
    out = []
    for b in t.list():
        m = NAME.fullmatch(b["name"])
        if m and (hub_id is None or m.group(1) == hub_id):
            out.append({**b, "hub": m.group(1), "at": m.group(2)})
    return sorted(out, key=lambda b: b["at"], reverse=True)


def prune(t, hub_id: str, keep: int) -> list[str]:
    """Delete this hub's backups past the ``keep`` newest. Returns their names."""
    old = [b["name"] for b in backups(t, hub_id)[max(1, int(keep)):]]
    for name in old:
        t.delete(name)
    return old


def run(*, config: Path, data: Path, library: Path | None, spec: str, keep: int = KEEP,
        call: Callable[[str, dict], Any] | None = None, check_after: bool = True,
        now: float | None = None) -> dict:
    """Make a backup, push it to the target, verify the pushed copy by
    restoring it (fetched back from the target) into scratch, then prune."""
    t = target(spec, call)
    if isinstance(t, DirTarget):
        for inside in (data, library):
            if inside is not None and t.path.resolve().is_relative_to(Path(inside).resolve()):
                raise BackupError(f"{t.label} is inside the hub's {'data' if inside == data else 'library'} "
                                  "folder: each backup would hold the ones before it")
    if not _ONE.acquire(blocking=False):
        raise BackupError("a backup is running already")
    try:
        path, manifest = make(config, data, library, now)
        size = path.stat().st_size
        try:
            t.put(path, path.name)
        finally:
            path.unlink(missing_ok=True)
        out = {"name": path.name, "target": t.label, "size": size, "files": len(manifest["files"])}
        if check_after:
            out["verified"] = fetch_verify(t, path.name, data)
        out["pruned"] = prune(t, manifest["hub"], keep)       # only after the new one checked out
        return out
    finally:
        _ONE.release()


def fetch_verify(t, name: str, data: Path) -> dict:
    """Fetch ``name`` back from the target and verify it by restoring it."""
    if not NAME.fullmatch(name):
        raise BackupError(f"not a backup name: {name!r}")
    local = _work(data) / f"fetch-{os.getpid()}-{time.time_ns()}-{name}"
    try:
        t.get(name, local)
        return verify(local, data)
    finally:
        local.unlink(missing_ok=True)


def fetch(spec: str, name: str | None, dst: Path, hub_id: str | None = None) -> str:
    """For a restore: copy a backup (the newest, unless ``name``) from a dir
    or rclone target to ``dst``. Returns its name."""
    t = target(spec)
    if name is None:
        found = backups(t, hub_id)
        if not found:
            raise BackupError(f"no backups on {t.label}")
        name = found[0]["name"]
    t.get(name, dst)
    return name
