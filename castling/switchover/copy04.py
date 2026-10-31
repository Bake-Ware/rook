"""Take a read-only copy of a 0.4 hub's data folder, for the one move.

Runs on the 0.4 hub's machine with the stock python3 (no Rook, no sqlite3
command needed: bakenetcanada has none), as the user that owns the data:

    python3 copy04.py /home/ubuntu/rook04-copy            # from /var/lib/rook-band-mcp
    python3 copy04.py --from <data folder> <empty folder>

It never writes to the hub's folder: each database is read through a
``mode=ro`` connection (``immutable`` when the hub is stopped, so not even a
``-shm`` appears) and copied with SQLite's online backup (one consistent
file, safe while the hub runs); every other file is copied as is. The copies are then
switched out of WAL mode so opening one can't leave ``-wal`` files beside
it. Skipped: ``-wal``/``-shm``/``.lock`` files (the backup already holds
their contents) and the release binaries under ``updates/`` (worker bundles
and APKs, published as GitHub releases; their ``.json``/``.jsonl`` are kept).
``COPY-INFO.json`` records when, from where, and each file's sha256.

The copy holds the vault key and every secret: keep it 0700, move it to the
0.5 hub privately, and delete it after the move.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import time

LIVE = "/var/lib/rook-band-mcp"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_uri(path: str) -> str:
    """How to open a hub database without writing beside it. A WAL database
    nothing has open (no ``-wal``/``-shm``: the hub is stopped) is read as
    immutable, since even a read-only open would create those files; every
    other one through a read-only connection, which takes the hub's locks."""
    with open(path, "rb") as f:
        wal = f.read(20)[18:20] == b"\x02\x02"
    idle = not os.path.exists(path + "-wal") and not os.path.exists(path + "-shm")
    return f"file:{path}?mode=ro" + ("&immutable=1" if wal and idle else "")


def copy(src: str, dst: str) -> dict:
    src, dst = os.path.abspath(src), os.path.abspath(dst)
    if dst == src or dst.startswith(src + os.sep):
        raise SystemExit(f"{dst} is inside {src}: copy somewhere else")
    if os.path.exists(dst) and os.listdir(dst):
        raise SystemExit(f"{dst} is not empty")
    os.umask(0o077)
    os.makedirs(dst, mode=0o700, exist_ok=True)
    files, skipped = {}, []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        rel_dir = os.path.relpath(root, src)
        for name in sorted(names):
            path = os.path.join(root, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if name.endswith(("-wal", "-shm", ".lock")) or os.path.islink(path):
                continue
            if rel.startswith("updates" + os.sep) and not name.endswith((".json", ".jsonl")):
                skipped.append({"path": rel, "size": os.path.getsize(path)})
                continue
            out = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(out), mode=0o700, exist_ok=True)
            with open(path, "rb") as f:
                is_db = f.read(16).startswith(b"SQLite format 3")
            if is_db:
                live = sqlite3.connect(_source_uri(path), uri=True)
                out_db = sqlite3.connect(out)
                try:
                    live.backup(out_db)
                    out_db.execute("PRAGMA journal_mode=DELETE")
                finally:
                    out_db.close()
                    live.close()
            else:
                shutil.copy2(path, out)
            files[rel] = {"size": os.path.getsize(out), "sha256": _sha256(out)}
        if not names and not dirs:
            os.makedirs(os.path.join(dst, rel_dir), mode=0o700, exist_ok=True)
    info = {"taken": time.time(), "from": src, "host": socket.gethostname(),
            "release": os.environ.get("ROOK_RELEASE") or None, "files": files, "not_copied": skipped}
    with open(os.path.join(dst, "COPY-INFO.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1, sort_keys=True)
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only copy of a 0.4 hub's data folder.")
    ap.add_argument("dest", help="an empty (or new) folder for the copy")
    ap.add_argument("--from", dest="src", default=LIVE, help=f"the 0.4 data folder (default {LIVE})")
    a = ap.parse_args(argv)
    info = copy(a.src, a.dest)
    print(f"{len(info['files'])} files copied to {a.dest}; {len(info['not_copied'])} release files not copied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
