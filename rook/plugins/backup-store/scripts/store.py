"""backup-store: the hub's sealed backups in one folder on this worker.

Stdlib only. Names are checked by the manifest's pattern (no paths), so
nothing outside the folder is reachable. A backup is written as
``<name>.part`` and renamed once the whole file's SHA-256 matches; a finished
backup is never overwritten. ``delete`` refuses a hub's newest backup.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from pathlib import Path

NAME = re.compile(r"rook-hub-([0-9a-f]{8,64})-(\d{8}T\d{6}Z)\.rookbak")
MAX_DATA = 6 * 1024 * 1024        # base64 of 4 MiB, with room


def _folder(ctx) -> Path:
    p = Path(os.path.expanduser(str(ctx.settings.get("folder") or "~/.rook-backups")))
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def _name(ctx, name: str) -> str:
    if not NAME.fullmatch(name or ""):
        raise ctx.Error(f"not a backup name: {name!r}")
    return name


def put(ctx, name, offset, data, sha256=None):
    folder, name = _folder(ctx), _name(ctx, name)
    if (folder / name).exists():
        raise ctx.Error(f"{name} is here already")
    if len(data) > MAX_DATA:
        raise ctx.Error("data is too big: send at most 4 MiB at a time")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ctx.Error("data is not base64") from None
    part = folder / (name + ".part")
    if offset == 0:                # a new upload: what an earlier one of this hub left is dropped
        hub = NAME.fullmatch(name).group(1)
        for old in folder.glob(f"rook-hub-{hub}-*.rookbak.part"):
            if old != part:
                old.unlink(missing_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if offset not in (0, have):
        raise ctx.Error(f"offset {offset}: {have} bytes are here; send from {have} (or 0 to start again)")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if offset == 0 else os.O_APPEND), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
        if sha256 is not None:
            f.flush()
            os.fsync(f.fileno())
    size = offset + len(raw)
    if sha256 is None:
        return {"name": name, "received": size}
    h = hashlib.sha256()
    with open(part, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    if h.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise ctx.Error(f"{name}: sha256 doesn't match; the upload was dropped, send it again")
    os.replace(part, folder / name)
    return {"name": name, "size": size, "kept": True}


def _backups(folder: Path) -> list[dict]:
    return sorted(({"name": p.name, "size": p.stat().st_size} for p in folder.iterdir()
                   if NAME.fullmatch(p.name) and p.is_file()), key=lambda b: b["name"], reverse=True)


def list_(ctx):
    folder = _folder(ctx)
    return {"folder": str(folder), "backups": _backups(folder)}


def get(ctx, name, offset=0, size=4 << 20):
    path = _folder(ctx) / _name(ctx, name)
    try:
        with open(path, "rb") as f:
            total = os.fstat(f.fileno()).st_size
            f.seek(offset)
            raw = f.read(size)
    except FileNotFoundError:
        raise ctx.Error(f"no backup {name} here") from None
    return {"name": name, "offset": offset, "data": base64.b64encode(raw).decode("ascii"),
            "eof": offset + len(raw) >= total}


def delete(ctx, name):
    folder, name = _folder(ctx), _name(ctx, name)
    hub = NAME.fullmatch(name).group(1)
    mine = [b["name"] for b in _backups(folder) if NAME.fullmatch(b["name"]).group(1) == hub]
    if name not in mine:
        raise ctx.Error(f"no backup {name} here")
    if name == mine[0]:
        raise ctx.Error(f"{name} is this hub's newest backup: it stays")
    (folder / name).unlink()
    return {"name": name, "deleted": True}
