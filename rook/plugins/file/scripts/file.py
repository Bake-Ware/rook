"""file: read, write, list, search and stat paths on this worker.

Pure stdlib, in the worker process, as the worker's account: nothing here
starts a process, so nothing can be elevated (use shell.exec with elevation
for files the worker can't reach). Failures raise ctx.Error naming the path.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import stat as st_
import time
from fnmatch import fnmatchcase
from pathlib import Path

SEARCH_SECONDS = 50     # a search stops here and says truncated (the cap's timeout is 60s)


def read(ctx, path, encoding="utf-8", max_bytes=8 * 1024 * 1024):
    p = Path(path)
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            raw = f.read(max_bytes + 1)
    except OSError as e:
        raise _error(ctx, e, path) from None
    out = {"size": size, "encoding": encoding}
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        out["truncated"] = True
    if encoding == "base64":
        out["content"] = base64.b64encode(raw).decode("ascii")
    else:
        try:
            out["content"] = raw.decode(encoding, errors="replace")
        except LookupError:
            raise ctx.Error(f"unknown encoding: {encoding}") from None
    return out


def write(ctx, path, content, encoding="utf-8", append=False, create_parents=False):
    p = Path(path)
    if encoding == "base64":
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise ctx.Error("content is not base64") from None
    else:
        try:
            raw = content.encode(encoding)
        except LookupError:
            raise ctx.Error(f"unknown encoding: {encoding}") from None
        except UnicodeEncodeError as e:
            raise ctx.Error(f"content can't be written as {encoding}: {e.reason}") from None
    try:
        if create_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab" if append else "wb") as f:
            n = f.write(raw)
    except OSError as e:
        raise _error(ctx, e, path) from None
    return {"path": str(p), "written": n}


def list_(ctx, path, recursive=False, hidden=True, limit=5000):
    p = Path(path)
    if not p.is_dir():
        raise ctx.Error(f"{'not a directory' if p.exists() else 'not found'}: {path}")
    entries = []
    try:
        for child in (p.rglob("*") if recursive else p.iterdir()):
            rel = child.relative_to(p)
            if not hidden and any(part.startswith(".") for part in rel.parts):
                continue
            if len(entries) >= limit:
                return {"entries": entries, "truncated": True}
            try:
                meta = _meta(child.lstat())
            except OSError:
                continue
            entries.append({"name": child.name, **meta, **({"path": str(rel)} if recursive else {})})
    except OSError as e:
        raise _error(ctx, e, path) from None
    return {"entries": entries}


def search(ctx, pattern, path=".", recursive=True, glob="*", ignore_case=False, limit=100):
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ctx.Error(f"bad pattern: {e}") from None
    root = Path(path)
    if not root.exists():
        raise ctx.Error(f"not found: {path}")
    if root.is_file():
        files = iter([root])
    else:
        files = (f for f in (root.rglob(glob) if recursive else root.glob(glob)) if f.is_file())
    deadline = time.monotonic() + SEARCH_SECONDS
    matches = []
    for fp in files:
        if time.monotonic() > deadline:
            return {"matches": matches, "truncated": True, "timed_out": True}
        try:
            with open(fp, "rb") as f:
                if b"\x00" in f.read(4096):
                    continue                    # binary
            with open(fp, encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if rx.search(line):
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}
                        matches.append({"path": str(fp), "line": n, "text": line.rstrip("\r\n")[:1000]})
        except OSError:
            continue
    return {"matches": matches}


def stat(ctx, path):
    p = Path(path)
    try:
        return {"exists": True, **_meta(p.lstat())}
    except FileNotFoundError:
        return {"exists": False}
    except OSError as e:
        raise _error(ctx, e, path) from None


def _meta(s: os.stat_result) -> dict:
    if st_.S_ISDIR(s.st_mode):
        kind = "dir"
    elif st_.S_ISLNK(s.st_mode):
        kind = "symlink"
    elif st_.S_ISREG(s.st_mode):
        kind = "file"
    else:
        kind = "other"
    return {"type": kind, "size": s.st_size, "modified": s.st_mtime, "mode": oct(st_.S_IMODE(s.st_mode))}


def _error(ctx, e: OSError, path: str):
    what = {FileNotFoundError: "not found", IsADirectoryError: "is a directory",
            NotADirectoryError: "not a directory", PermissionError: "permission denied",
            FileExistsError: "exists"}.get(type(e))
    return ctx.Error(f"{what}: {path}" if what else f"{path}: {e.strerror or e}")
