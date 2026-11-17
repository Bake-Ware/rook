"""file: read, write, list, search and stat files on this worker, as the worker's own user,
shipped as a plugin installed by default (PL7). docs/inventory.md CAP-04. The names, arguments
and results are 0.5's (origin/castling rook/plugins/file/plugin.yaml and scripts/file.py), by
Bake's answer of 17 November.

Nothing here bounds a call (Bake, 14 and 17 November). `max_bytes` (file.read) and `limit`
(file.list, file.search) are 0.5's arguments a caller may pass, with no default: with nothing
passed, a cap gives everything. There is no deadline, no size cap and no cut of a line. A result
over the largest message is answered `cap_failed` by the worker, as for every cap (docs/protocol.md,
"Sizes"). Paths are as given: relative to the worker's folder, no `~` expansion, as in 0.5.

file.search runs the pattern in a child Python, as 0.5 does: Python's `re` holds the interpreter
for a whole match, so a pattern that takes long would otherwise stop the worker, heartbeats and all.
The child runs until it is done."""

import asyncio
import base64
import binascii
import json
import os
import pathlib
import re
import stat as st
import sys

from rook_worker import Cap


# ------------------------------------------------------------------- arguments

def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed)}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _text(args, name, default=None):
    value = args.get(name, default)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _flag(args, name, default):
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _count(args, name, least):
    """A whole number of at least `least`, or None when not given: then nothing is cut."""
    value = args.get(name)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < least):
        raise ValueError(f"{name} must be a whole number, {least} or more; leave it out for everything")
    return value


def _failed(e, path):
    """An OSError on `path` as a refusal that says what to try (C3)."""
    what = {FileNotFoundError: "not found", IsADirectoryError: "is a folder", NotADirectoryError: "not a folder",
            PermissionError: "permission denied for this worker's user", FileExistsError: "exists"}.get(type(e))
    hint = {FileNotFoundError: "; check the path with file.stat, or list its folder with file.list",
            IsADirectoryError: "; list a folder with file.list",
            NotADirectoryError: "; give a folder's path"}.get(type(e), "")
    return ValueError((f"{what}: {path}" if what else f"{path}: {e.strerror or e}") + hint)


def _meta(s):
    """type, size, modified and mode, of a path's own lstat: a symlink is itself."""
    if st.S_ISDIR(s.st_mode):
        kind = "dir"
    elif st.S_ISLNK(s.st_mode):
        kind = "symlink"
    elif st.S_ISREG(s.st_mode):
        kind = "file"
    else:
        kind = "other"
    return {"type": kind, "size": s.st_size, "modified": s.st_mtime, "mode": oct(st.S_IMODE(s.st_mode))}


# -------------------------------------------------------------------- the caps

def read(args):
    _check(args, "file.read", ("path", "encoding", "max_bytes"), ("path",))
    path, encoding, most = _text(args, "path"), _text(args, "encoding", "utf-8"), _count(args, "max_bytes", 0)
    try:
        # Opened without waiting, then only a regular file is read, as 0.5 does: a FIFO, a device
        # or a folder is refused, saying so.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(fd)
            if not st.S_ISREG(info.st_mode):
                raise ValueError(f"not a regular file: {path}; file.read reads files, describe it with file.stat")
            if getattr(os, "O_NONBLOCK", 0):
                os.set_blocking(fd, True)
            raw = f.read() if most is None else f.read(most + 1)
    except OSError as e:
        raise _failed(e, path) from None
    out = {"size": info.st_size, "encoding": encoding}
    if most is not None and len(raw) > most:
        raw = raw[:most]
        out["truncated"] = True
    if encoding == "base64":
        out["content"] = base64.b64encode(raw).decode("ascii")
    else:
        try:
            out["content"] = raw.decode(encoding, errors="replace")
        except LookupError:
            raise ValueError(f"unknown encoding: {encoding}; give a Python codec such as utf-8 or latin-1, "
                             "or base64 for bytes") from None
    return out


def write(args):
    _check(args, "file.write", ("path", "content", "encoding", "append", "create_parents"), ("path", "content"))
    path, content, encoding = _text(args, "path"), _text(args, "content"), _text(args, "encoding", "utf-8")
    append, parents = _flag(args, "append", False), _flag(args, "create_parents", False)
    if encoding == "base64":
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("content is not base64; give base64 text, or another encoding for text") from None
    else:
        try:
            raw = content.encode(encoding)
        except LookupError:
            raise ValueError(f"unknown encoding: {encoding}; give a Python codec such as utf-8 or latin-1, "
                             "or base64 for bytes") from None
        except UnicodeEncodeError as e:
            raise ValueError(f"content can't be written as {encoding}: {e.reason}; use utf-8") from None
    try:
        if parents:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "ab" if append else "wb") as f:
            written = f.write(raw)
    except FileNotFoundError:
        raise ValueError(f"not found: the folder of {path}; make it with create_parents true") from None
    except OSError as e:
        raise _failed(e, path) from None
    return {"path": str(pathlib.Path(path)), "written": written}


def list_(args):
    _check(args, "file.list", ("path", "recursive", "hidden", "limit"), ("path",))
    path, recursive = _text(args, "path"), _flag(args, "recursive", False)
    hidden, limit = _flag(args, "hidden", True), _count(args, "limit", 1)
    if not os.path.isdir(path):
        raise ValueError(f"{'not a folder' if os.path.exists(path) else 'not found'}: {path}; "
                         "give a folder's path; describe a file with file.stat")
    entries = []
    try:
        for full in _under(path, recursive):
            relative = os.path.relpath(full, path)
            if not hidden and any(part.startswith(".") for part in relative.split(os.sep)):
                continue
            if limit is not None and len(entries) >= limit:
                return {"entries": entries, "truncated": True}
            try:
                meta = _meta(os.lstat(full))
            except OSError:
                continue                  # gone, or not readable, since the folder was read
            entries.append({"name": os.path.basename(full), **meta, **({"path": relative} if recursive else {})})
    except OSError as e:
        raise _failed(e, path) from None
    return {"entries": entries}


def _under(path, recursive):
    """Every path in a folder; with `recursive`, in every folder under it, not following links."""
    for name in os.listdir(path):
        full = os.path.join(path, name)
        yield full
        if recursive and os.path.isdir(full) and not os.path.islink(full):
            yield from _under(full, True)


def stat(args):
    _check(args, "file.stat", ("path",), ("path",))
    path = _text(args, "path")
    try:
        return {"exists": True, **_meta(os.lstat(path))}
    except FileNotFoundError:
        return {"exists": False}
    except OSError as e:
        raise _failed(e, path) from None


# The search itself, run as `python -I -c SCAN` with its job as JSON on stdin and the result as
# JSON on stdout. A file with a NUL byte is binary and skipped, as in 0.5.
SCAN = r'''
import json, re, sys
from pathlib import Path

def scan(pattern, path, recursive, glob, ignore_case, limit):
    rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    root = Path(path)
    files = [root] if root.is_file() else (f for f in (root.rglob(glob) if recursive else root.glob(glob))
                                           if f.is_file())
    matches = []
    for found in files:
        try:
            data = found.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                if limit is not None and len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
                matches.append({"path": str(found), "line": number, "text": line})
    return {"matches": matches}

print(json.dumps(scan(**json.load(sys.stdin))))
'''


async def search(args):
    _check(args, "file.search", ("pattern", "path", "recursive", "glob", "ignore_case", "limit"), ("pattern",))
    job = {"pattern": _text(args, "pattern"), "path": _text(args, "path", "."),
           "recursive": _flag(args, "recursive", True), "glob": _text(args, "glob", "*"),
           "ignore_case": _flag(args, "ignore_case", False), "limit": _count(args, "limit", 1)}
    try:
        re.compile(job["pattern"])
    except re.error as e:
        raise ValueError(f"bad pattern: {e}; give a Python regular expression") from None
    if not os.path.exists(job["path"]):
        raise ValueError(f"not found: {job['path']}; give a folder or a file to search")
    child = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", SCAN, stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await child.communicate(json.dumps(job).encode("utf-8"))
    finally:
        if child.returncode is None:         # the call was cancelled: the search goes with it
            child.kill()
            await child.wait()
    try:
        return json.loads(out)
    except ValueError:
        raise ValueError(f"search failed: {err.decode('utf-8', 'replace').strip()}") from None


def _thread(function):
    """Each file cap blocks on the disk, so a call runs it in a thread."""
    async def handler(args):
        return await asyncio.to_thread(function, args)
    return handler


_PATH = {"type": "string", "description": "A path on this worker, relative to the worker's folder unless absolute."}
_ENCODING = {"type": "string", "description": "A Python codec such as utf-8 (the default) or latin-1, or base64 for bytes."}

CAPS = {
    "file.read": Cap(
        "A file's content: text in encoding, or base64 (encoding base64) for bytes; gives content, size, encoding, and truncated when max_bytes cut it.",
        {"type": "object", "required": ["path"], "additionalProperties": False, "properties": {
            "path": _PATH, "encoding": _ENCODING,
            "max_bytes": {"type": "integer", "minimum": 0,
                          "description": "Read at most this many bytes; the whole file unless given."}}},
        _thread(read)),
    "file.write": Cap(
        "Write content to a file, replacing it (or append true); encoding base64 writes bytes; create_parents makes missing folders. Gives path, written.",
        {"type": "object", "required": ["path", "content"], "additionalProperties": False, "properties": {
            "path": _PATH, "content": {"type": "string", "description": "The text, or base64 with encoding base64."},
            "encoding": _ENCODING,
            "append": {"type": "boolean", "description": "Add to the end instead of replacing; false unless given."},
            "create_parents": {"type": "boolean", "description": "Make missing folders first; false unless given."}}},
        _thread(write)),
    "file.list": Cap(
        "A folder's entries: name, type (file, dir, symlink, other), size, modified, mode; recursive adds each entry's relative path.",
        {"type": "object", "required": ["path"], "additionalProperties": False, "properties": {
            "path": _PATH,
            "recursive": {"type": "boolean", "description": "Every folder under it too; false unless given."},
            "hidden": {"type": "boolean", "description": "Include dot files; true unless given."},
            "limit": {"type": "integer", "minimum": 1,
                      "description": "At most this many entries, then truncated; all of them unless given."}}},
        _thread(list_)),
    "file.search": Cap(
        "Lines matching a regex in the files under path (glob narrows them; binary files are skipped): matches of path, line, text.",
        {"type": "object", "required": ["pattern"], "additionalProperties": False, "properties": {
            "pattern": {"type": "string", "description": "A Python regular expression."},
            "path": {**_PATH, "description": "A folder or a file; the worker's folder unless given."},
            "recursive": {"type": "boolean", "description": "Every folder under path; true unless given."},
            "glob": {"type": "string", "description": "Only files whose names match, such as *.py; * unless given."},
            "ignore_case": {"type": "boolean", "description": "Match regardless of case; false unless given."},
            "limit": {"type": "integer", "minimum": 1,
                      "description": "At most this many matches, then truncated; all of them unless given."}}},
        search),
    "file.stat": Cap(
        "Whether a path exists, and if so its type, size, modified time and mode (a symlink is described as itself).",
        {"type": "object", "required": ["path"], "additionalProperties": False, "properties": {"path": _PATH}},
        _thread(stat)),
}
