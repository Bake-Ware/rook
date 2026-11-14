"""Backup and restore of a hub's data directory (F3, X4; Bake 13 Nov).

A backup is one file holding everything a hub needs to come back as itself: a
consistent copy of the database, the hub's own key (`hub.key`, R5), the token
secret (`token.secret`), the installed plugins' folders and the blob bytes. It
is taken while the hub serves the directory and writes to it: the database is
copied through SQLite's backup API on a read-only connection, which sees one
consistent moment and needs neither the hub nor its lock. That plain working
copy is kept in the data directory itself (`.backup.db`), beside the database
it copies, never where the backup goes: the destination only ever sees sealed
bytes (X4). A backup holds an exclusive lock on the copy while it works, so two
backups never share it; a copy left by an interrupted backup is overwritten and
then removed by the next one.

The file is encrypted and authenticated with a key derived from the vault key
(X4: "Backups are encrypted"; Bake 13 Nov). The vault key itself is never in it.
Its layout:

    MAGIC                    b"rook-backup 1\\n": what it is, and the format version
    header                   24 bytes: the libsodium secretstream header (random)
    chunks                   the data, as a tar stream cut in CHUNK-byte pieces, each
                             sealed by secretstream (XChaCha20-Poly1305), the last
                             one marked final

Secretstream binds every chunk to its place, so a changed, dropped, reordered or
cut-off chunk is refused. The file is written and read a chunk at a time, so
neither a backup nor a restore holds the data in memory.

A restore unpacks into a new directory beside the target and moves it into
place only once the whole file has been read and proven. Anything wrong
leaves nothing behind.
"""

import fcntl
import hashlib
import os
import pathlib
import shutil
import sqlite3
import tarfile
import tempfile

from nacl import bindings as sodium
from nacl.exceptions import CryptoError

from . import owner_only, vault
from .errors import HubError

MAGIC = b"rook-backup 1\n"
CHUNK = 1 << 16                         # plaintext bytes per sealed chunk
SEALED = CHUNK + sodium.crypto_secretstream_xchacha20poly1305_ABYTES
HEADER = sodium.crypto_secretstream_xchacha20poly1305_HEADERBYTES
FINAL = sodium.crypto_secretstream_xchacha20poly1305_TAG_FINAL
FILES = ("hub.key", "token.secret")     # beside the database; hub.lock and the WAL are not data
COPY = ".backup.db"                     # the plain working copy of the database: in the data directory only


def _key(vault_key):
    return hashlib.blake2b(vault_key, digest_size=32, person=b"rook-backup").digest()


def read_key(path):
    """The vault key in the key file at `path`, never made here."""
    try:
        key = bytes.fromhex(pathlib.Path(path).read_text().strip())
        if len(key) == 32:
            return key
    except (OSError, ValueError):
        pass
    raise HubError("vault_key_file_bad", f"{path} does not hold a vault key.",
                   "Give the path of this hub's vault key file, the one its start command uses.")


class _Sealing:
    """A write-only file: what is written goes out sealed, a chunk at a time."""

    def __init__(self, out, key):
        self.out, self.buffer = out, bytearray()
        self.state = sodium.crypto_secretstream_xchacha20poly1305_state()
        out.write(MAGIC + sodium.crypto_secretstream_xchacha20poly1305_init_push(self.state, key))

    def write(self, data):
        self.buffer += data
        while len(self.buffer) >= CHUNK:
            self._push(bytes(self.buffer[:CHUNK]), 0)
            del self.buffer[:CHUNK]
        return len(data)

    def finish(self):
        self._push(bytes(self.buffer), FINAL)

    def _push(self, data, tag):
        self.out.write(sodium.crypto_secretstream_xchacha20poly1305_push(self.state, data, MAGIC, tag))


class _Opening:
    """A read-only file: the backup's data, each chunk proven before any of it is handed on."""

    def __init__(self, source, key, path):
        self.source, self.path, self.buffer, self.done, self.chunks = source, path, b"", False, 0
        if source.read(len(MAGIC)) != MAGIC:
            raise HubError("not_a_backup", f"{path} is not a Rook hub backup in a format this hub reads.",
                           "Give the file the backup command wrote.")
        header = source.read(HEADER)
        self.state = sodium.crypto_secretstream_xchacha20poly1305_state()
        if len(header) != HEADER:
            raise self._cut()
        sodium.crypto_secretstream_xchacha20poly1305_init_pull(self.state, header, key)

    def _cut(self):
        return HubError("backup_cut_short", f"{self.path} is not a whole backup: it is cut short.",
                        "Copy the whole file again from where the backup was written, then restore from it.")

    def read(self, size=-1):
        while not self.done and (size < 0 or len(self.buffer) < size):
            self._pull()
        if size < 0:
            size = len(self.buffer)
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _pull(self):
        sealed = self.source.read(SEALED)
        if not sealed:
            raise self._cut()
        try:
            data, tag = sodium.crypto_secretstream_xchacha20poly1305_pull(self.state, sealed, MAGIC)
        except CryptoError:
            if self.chunks == 0:            # nothing has opened yet: the key, most likely
                raise HubError("backup_wrong_key", f"{self.path} does not open with this vault key: it was made "
                                                   "with another hub's key, or its start was changed.",
                               "Give the vault key file of the hub the backup was taken from.") from None
            if len(sealed) < SEALED:        # the file's last bytes do not end a backup
                raise HubError("backup_cut_short", f"{self.path} is not a whole backup: it is cut short, or its "
                                                   "end was changed.",
                               "Copy the whole file again from where the backup was written, then restore from it.") from None
            raise HubError("backup_changed", f"{self.path} has been changed or damaged after it was written.",
                           "Restore from another copy of the backup.") from None
        self.chunks += 1
        self.buffer += data
        if tag == FINAL:
            if self.source.read(1):
                raise HubError("backup_changed", f"{self.path} has data after its end: it was changed.",
                               "Restore from another copy of the backup.")
            self.done = True


# ------------------------------------------------------------------- backup

def backup(data, vault_key_file, to):
    """Write a backup of the hub data directory `data` to the new file `to`. Returns its size."""
    data, to = pathlib.Path(data), pathlib.Path(to)
    key = read_key(vault_key_file)
    if not (data / "hub.db").is_file():
        raise HubError("not_hub_data", f"{data} is not a hub's data directory: it has no hub.db.",
                       "Give the --data directory the hub's start command uses.")
    if to.exists():
        raise HubError("backup_exists", f"{to} already exists.", "Give a path where no file is yet.")
    to.parent.mkdir(parents=True, exist_ok=True)
    copy = data / COPY
    held = open(os.open(copy, os.O_WRONLY | os.O_CREAT, 0o600), "wb")
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        held.close()
        raise HubError("backup_running", f"Another backup of {data} is running.",
                       "Wait for it to finish, then take this one.") from None
    try:
        held.truncate(0)            # a copy an interrupted backup left goes now
        source = sqlite3.connect(data.resolve().as_uri() + "/hub.db?mode=ro", uri=True)
        target = sqlite3.connect(copy)
        target.execute("PRAGMA journal_mode=OFF")     # no journal file beside the copy
        try:
            source.backup(target)            # one consistent moment, while the hub goes on writing
            _check_key(target, key, vault_key_file)
            folders = [row[0] for row in target.execute("SELECT folder FROM plugins")]
        finally:
            source.close()
            target.close()
        with open(os.open(to, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:    # owner-only, whatever the umask
            try:
                sealing = _Sealing(out, _key(key))
                with tarfile.open(fileobj=sealing, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                    tar.add(copy, arcname="hub.db")
                    for name in FILES:
                        tar.add(data / name, arcname=name)
                    for folder in folders:
                        if not (data / "plugins" / folder).is_dir():
                            raise HubError("backup_moved_under", f"Plugin folder {folder} went away during the backup "
                                                                 "(a plugin was installed again meanwhile).",
                                           "Take the backup again.")
                        tar.add(data / "plugins" / folder, arcname=f"plugins/{folder}", filter=_kept)
                    if (data / "blobs").is_dir():         # content-addressed: only ever added to (K3, K4)
                        tar.add(data / "blobs", arcname="blobs", filter=_kept)
                sealing.finish()
            except BaseException:
                out.close()
                to.unlink()
                raise
    finally:
        copy.unlink(missing_ok=True)
        held.close()
    return to.stat().st_size


def _kept(info):
    """Leave out what is not data: a blob still being written, and Python's compiled cache."""
    name = pathlib.PurePosixPath(info.name)
    return None if name.suffix == ".partial" or "__pycache__" in name.parts else info


def _check_key(db, key, path):
    """Refuse a key file that does not open this hub's vault: its backups would open with no key."""
    if vault.key_opens(db, key):
        return
    raise HubError("vault_key_wrong", f"{path} does not hold this hub's vault key.",
                   "Give the vault key file this hub's start command uses.")


# ------------------------------------------------------------------ restore

def restore(source, data, vault_key_file):
    """Write the hub data directory `data` from the backup file `source`. `data` must not exist,
    or be empty. Nothing is written unless the whole backup opens and is proven."""
    source, data = pathlib.Path(source), pathlib.Path(data)
    key = read_key(vault_key_file)
    if data.exists() and (not data.is_dir() or any(data.iterdir())):
        raise HubError("data_not_empty", f"{data} already holds something.",
                       "Restore into a new directory, or an empty one; a hub's data is never restored over.")
    if not source.is_file():
        raise HubError("no_backup", f"{source} is not a file.", "Give the path of a backup file.")
    made = [parent for parent in [data.parent, *data.parent.parents] if not parent.exists()]
    data.parent.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix=f".{data.name}.restoring.", dir=data.parent))
    try:
        with open(source, "rb") as f:
            opening = _Opening(f, _key(key), source)
            with tarfile.open(fileobj=opening, mode="r|") as tar:
                for member in tar:
                    _unpack(tar, member, work, source)
            opening.read()          # to the final chunk: a backup cut short is refused here
            if not opening.done:
                raise opening._cut()
        if not (work / "hub.db").is_file() or not all((work / name).is_file() for name in FILES):
            raise HubError("not_a_backup", f"{source} does not hold a hub's data.", "Give the file the backup command wrote.")
        owner_only(work)            # the folders mkdir made above a file (plugins/) got the umask's mode, not 0700
        if data.exists():
            data.rmdir()            # empty, checked above
        os.replace(work, data)
    except tarfile.TarError:
        shutil.rmtree(work, ignore_errors=True)
        for parent in made:
            parent.rmdir()
        raise HubError("not_a_backup", f"{source} opens, but does not hold a hub's data.",
                       "Give the file the backup command wrote.") from None
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        for parent in made:              # the folders made for it, innermost first: nothing is left behind
            parent.rmdir()
        raise


def _unpack(tar, member, work, source):
    """One file or folder from the backup, inside `work` only, owner-only."""
    path = pathlib.PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
        raise HubError("not_a_backup", f"{source} holds {member.name!r}, which no backup holds.",
                       "Give the file the backup command wrote.")
    target = work.joinpath(*path.parts)
    if member.isdir():
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        return
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tar.extractfile(member) as part, open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        shutil.copyfileobj(part, out, CHUNK)
