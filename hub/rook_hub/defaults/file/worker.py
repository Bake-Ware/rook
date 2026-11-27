"""file: read, write, list, search and stat files on this worker, as the worker's own user,
shipped as a plugin installed by default (PL7). docs/inventory.md CAP-04. The names, arguments
and results are 0.5's (origin/castling rook/plugins/file/plugin.yaml and scripts/file.py), by
Bake's answer of 17 November.

Nothing here bounds a call (Bake, 14 and 17 November). `max_bytes` (file.read) and `limit`
(file.list, file.search) are 0.5's arguments a caller may pass, with no default: with nothing
passed, a cap gives everything. There is no deadline, no size cap and no cut of a line. What can
be big is made as it is sent (brief 54): file.read's content a piece at a time, file.list's entries
a folder at a time, file.search's matches as the search finds them; a result over about a megabyte
goes in pieces (docs/protocol.md, "A big result"), and one under it is the same reply as ever.
Paths are as given: relative to the worker's folder, no `~` expansion, as in 0.5.

file.search runs the pattern in a child Python, as 0.5 does: Python's `re` holds the interpreter
for a whole match, so a pattern that takes long would otherwise stop the worker, heartbeats and all.
The child runs until it is done."""

import asyncio
import base64
import binascii
import codecs
import json
import os
import pathlib
import re
import stat as st
import sys

from rook_worker import LEFT_OUT, PIECE, Cap, Items, Text

try:
    from java import jclass     # Chaquopy's: only in the Rook Android app (brief AP8, _unmounted)
except ImportError:
    jclass = None

SHARED = ("/sdcard", "/storage", "/mnt/sdcard", "/mnt/user")     # Android's shared storage


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


def _unmounted(path):
    """On the Rook Android app before the phone's first unlock since it restarted, Android has not yet opened its
    shared storage (/sdcard), so nothing under it is found (brief AP8): what to say then, else None."""
    if jclass is None or not os.path.abspath(path).startswith(SHARED):
        return None
    said = jclass("systems.bake.rook.core.worker.Bridge").locked("keeps the phone's shared storage (/sdcard) closed")
    return None if said is None else str(said)


def _failed(e, path):
    """An OSError on `path` as a refusal that says what to try (C3)."""
    closed = _unmounted(path) if isinstance(e, FileNotFoundError) else None
    if closed:
        return ValueError(f"not found: {path}; {closed}")
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

async def read(args):
    _check(args, "file.read", ("path", "encoding", "max_bytes"), ("path",))
    path, encoding, most = _text(args, "path"), _text(args, "encoding", "utf-8"), _count(args, "max_bytes", 0)
    try:
        # Anything that opens is read, a FIFO or a device too, for as long as it takes (Bake, 17 November:
        # a call that can wait for ever is allowed to). 0.5 refused all but regular files; not here.
        f = await asyncio.to_thread(open, path, "rb")
        info = os.fstat(f.fileno())
    except OSError as e:
        raise _failed(e, path) from None
    if encoding != "base64":
        try:
            b"".decode(encoding)        # a codec that is not a text codec is refused as an unknown one
            decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        except LookupError:
            f.close()
            raise ValueError(f"unknown encoding: {encoding}; give a Python codec such as utf-8 or latin-1, "
                             "or base64 for bytes") from None
    cut = asyncio.get_running_loop().create_future()      # whether max_bytes cut it: known at the end
    content = _content(f, path, most, cut, None if encoding == "base64" else decoder)
    return {"content": Text(content), "encoding": encoding, "size": info.st_size, "truncated": cut}


async def _content(f, path, most, cut, decoder):
    """The file's content as text, or base64 when `decoder` is None, a piece at a time."""
    got, carry, truncated = 0, b"", False
    try:
        while True:
            chunk = await asyncio.to_thread(f.read, PIECE if most is None else min(PIECE, most + 1 - got))
            if not chunk:
                break
            got += len(chunk)
            if most is not None and got > most:
                chunk, truncated = chunk[:len(chunk) - (got - most)], True
            if decoder is not None:
                yield decoder.decode(chunk)
            else:
                data = carry + chunk
                whole = len(data) // 3 * 3        # base64 of whole 3-byte groups, the rest carried
                yield base64.b64encode(data[:whole]).decode("ascii")
                carry = data[whole:]
            if truncated:
                break
        yield decoder.decode(b"", final=True) if decoder is not None else base64.b64encode(carry).decode("ascii")
    except OSError as e:
        raise _failed(e, path) from None
    finally:
        f.close()
        if not cut.done():
            cut.set_result(True if truncated else LEFT_OUT)


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
        closed = _unmounted(path)
        raise ValueError(f"not found: the folder of {path}; {closed or 'make it with create_parents true'}") from None
    except OSError as e:
        raise _failed(e, path) from None
    return {"path": str(pathlib.Path(path)), "written": written}


async def list_(args):
    _check(args, "file.list", ("path", "recursive", "hidden", "limit"), ("path",))
    path, recursive = _text(args, "path"), _flag(args, "recursive", False)
    hidden, limit = _flag(args, "hidden", True), _count(args, "limit", 1)
    if not await asyncio.to_thread(os.path.isdir, path):
        closed = None if os.path.exists(path) else await asyncio.to_thread(_unmounted, path)
        raise ValueError(f"{'not a folder' if os.path.exists(path) else 'not found'}: {path}; "
                         + (closed or "give a folder's path; describe a file with file.stat"))
    first = await _folder(path, path)       # the folder itself is read now, so a refusal comes as ever
    cut = asyncio.get_running_loop().create_future()      # whether limit cut it: known at the end
    return {"entries": Items(_entries(path, first, recursive, hidden, limit, cut)), "truncated": cut}


async def _entries(path, first, recursive, hidden, limit, cut):
    """Every entry under `path`, in the order 0.5 gives them: each entry, then (recursive) what is under it."""
    count, truncated = 0, False

    async def walk(listed):
        nonlocal count, truncated
        for full, meta, deeper in listed:
            relative = os.path.relpath(full, path)
            if hidden or not any(part.startswith(".") for part in relative.split(os.sep)):
                if limit is not None and count >= limit:
                    truncated = True
                    return
                if meta is not None:      # None: gone, or not readable, since the folder was read
                    count += 1
                    yield {"name": os.path.basename(full), **meta, **({"path": relative} if recursive else {})}
            if recursive and deeper:
                async for entry in walk(await _folder(full, path)):
                    yield entry
                if truncated:
                    return

    try:
        async for entry in walk(first):
            yield entry
    finally:
        if not cut.done():
            cut.set_result(True if truncated else LEFT_OUT)


async def _folder(folder, path):
    """One folder's entries, read in a thread: (full path, its _meta or None, whether to go into it)."""
    def read():
        listed = []
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            try:
                meta = _meta(os.lstat(full))
            except OSError:
                meta = None
            listed.append((full, meta, os.path.isdir(full) and not os.path.islink(full)))
        return listed
    try:
        return await asyncio.to_thread(read)
    except OSError as e:
        raise _failed(e, path) from None


def stat(args):
    _check(args, "file.stat", ("path",), ("path",))
    path = _text(args, "path")
    try:
        return {"exists": True, **_meta(os.lstat(path))}
    except FileNotFoundError:
        closed = _unmounted(path)
        return {"exists": False, **({"note": closed} if closed else {})}
    except OSError as e:
        raise _failed(e, path) from None


# The search itself, run as `python -I -c SCAN` with its job as JSON on stdin. It writes each match
# as a line of JSON on stdout as it finds it, then a last line {"truncated": ...}. A file with a NUL
# byte is binary and skipped, as in 0.5.
SCAN = r'''
import json, re, sys
from pathlib import Path

def scan(pattern, path, recursive, glob, ignore_case, limit):
    rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    root = Path(path)
    files = [root] if root.is_file() else (f for f in (root.rglob(glob) if recursive else root.glob(glob))
                                           if f.is_file())
    count = 0
    for found in files:
        try:
            data = found.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                if limit is not None and count >= limit:
                    return True
                print(json.dumps({"path": str(found), "line": number, "text": line}))
                count += 1
    return False

print(json.dumps({"truncated": scan(**json.load(sys.stdin))}))
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
        closed = await asyncio.to_thread(_unmounted, job["path"])
        raise ValueError(f"not found: {job['path']}; {closed or 'give a folder or a file to search'}")
    child = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", SCAN, stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    child.stdin.write(json.dumps(job).encode("utf-8"))
    child.stdin.close()
    said = asyncio.ensure_future(child.stderr.read())     # what it says if it fails, read alongside
    cut = asyncio.get_running_loop().create_future()      # whether limit cut it: known at the end
    return {"matches": Items(_matches(child, said, cut)), "truncated": cut}


async def _matches(child, said, cut):
    """Each match as the child finds it. Lines are split here, not by readline, which has a longest line."""
    last, held = None, b""
    try:
        while chunk := await child.stdout.read(PIECE):
            *lines, held = (held + chunk).split(b"\n")
            for line in lines:
                item = json.loads(line)
                if "path" not in item:
                    last = item
                else:
                    yield item
        await child.wait()
        if last is None:
            raise ValueError(f"search failed: {(await said).decode('utf-8', 'replace').strip()}")
    finally:
        if child.returncode is None:         # the call ended early: the search goes with it
            child.kill()
            await child.wait()
        if not cut.done():
            cut.set_result(True if last and last["truncated"] else LEFT_OUT)


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
        read),
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
        list_),
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
