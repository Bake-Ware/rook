"""file.* — local filesystem read/write/list/search.

Pure stdlib, no platform branching. Capabilities are intentionally permissive
about path — the worker is expected to be sandboxed at deployment time, not by
this plugin. Failures raise; the reply's ``error`` names the path.
"""

from __future__ import annotations

import base64
import os
import re
import stat
from pathlib import Path

from ...core.pluginkit import fail, require_int, require_str
from ..plugin import Plugin, capability


def _stat_to_meta(p: Path, st: os.stat_result) -> dict:
    if stat.S_ISDIR(st.st_mode):
        kind = "dir"
    elif stat.S_ISLNK(st.st_mode):
        kind = "symlink"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        kind = "other"
    return {
        "name": p.name,
        "type": kind,
        "size": st.st_size,
        "modified": st.st_mtime,
    }


class FilePlugin(Plugin):
    NAMESPACE = "file"

    @capability("read")
    def _read(self, path: str, encoding: str = "utf-8",
              max_bytes: int = 8 * 1024 * 1024) -> dict:
        """Read a file. ``encoding`` of ``"base64"`` returns raw bytes b64-encoded.

        Reads up to ``max_bytes`` (default 8 MiB) to avoid runaway responses.
        """
        path = require_str(path, "path", strip=False)
        max_bytes = require_int(max_bytes, "max_bytes", min=0)
        try:
            p = Path(path)
            size = p.stat().st_size
            with open(p, "rb") as f:
                raw = f.read(max_bytes + 1)
            truncated = len(raw) > max_bytes
            if truncated:
                raw = raw[:max_bytes]
            if encoding == "base64":
                content: str = base64.b64encode(raw).decode("ascii")
            else:
                content = raw.decode(encoding, errors="replace")
            out = {
                "ok": True,
                "content": content,
                "size_bytes": size,
                "encoding": encoding,
            }
            if truncated:
                out["truncated"] = True
                out["returned_bytes"] = max_bytes
            return out
        except FileNotFoundError:
            fail(f"file not found: {path}")
        except PermissionError as e:
            fail(f"permission denied: {e}")
        except IsADirectoryError:
            fail(f"is a directory: {path}")

    @capability("write")
    def _write(self, path: str, content: str, encoding: str = "utf-8",
               append: bool = False, create_parents: bool = False) -> dict:
        """Write ``content`` to ``path``. ``encoding`` ``"base64"`` decodes first."""
        path = require_str(path, "path", strip=False)
        content = require_str(content, "content", strip=False, empty=True)
        try:
            p = Path(path)
            if create_parents:
                p.parent.mkdir(parents=True, exist_ok=True)
            if encoding == "base64":
                raw = base64.b64decode(content)
            else:
                raw = content.encode(encoding)
            mode = "ab" if append else "wb"
            with open(p, mode) as f:
                n = f.write(raw)
            return {"ok": True, "written_bytes": n, "path": str(p)}
        except FileNotFoundError as e:
            fail(f"parent directory missing: {e}")
        except PermissionError as e:
            fail(f"permission denied: {e}")

    @capability("list")
    def _list(self, path: str, recursive: bool = False,
              include_hidden: bool = True, max_entries: int = 5000) -> dict:
        """List a directory. ``recursive`` walks subdirs; ``max_entries`` caps results."""
        path = require_str(path, "path", strip=False)
        max_entries = require_int(max_entries, "max_entries", min=1)
        try:
            p = Path(path)
            if not p.exists():
                fail(f"not found: {path}")
            if not p.is_dir():
                fail(f"not a directory: {path}")
            entries: list[dict] = []
            iterator = p.rglob("*") if recursive else p.iterdir()
            for child in iterator:
                if len(entries) >= max_entries:
                    return {"ok": True, "entries": entries,
                            "truncated": True, "limit": max_entries}
                name = child.name
                if not include_hidden and name.startswith("."):
                    continue
                try:
                    st = child.lstat()
                except OSError:
                    continue
                meta = _stat_to_meta(child, st)
                if recursive:
                    meta["path"] = str(child.relative_to(p))
                entries.append(meta)
            return {"ok": True, "entries": entries}
        except PermissionError as e:
            fail(f"permission denied: {e}")

    @capability("search")
    def _search(self, pattern: str, path: str = ".", recursive: bool = True,
                max_results: int = 100, ignore_case: bool = False,
                glob: str = "*") -> dict:
        """Grep ``pattern`` (regex) across files under ``path``. Skips binary files."""
        pattern = require_str(pattern, "pattern", strip=False)
        max_results = require_int(max_results, "max_results", min=1)
        try:
            flags = re.IGNORECASE if ignore_case else 0
            rx = re.compile(pattern, flags)
        except re.error as e:
            fail(f"invalid regex: {e}")
        root = Path(path)
        if not root.exists():
            fail(f"not found: {path}")

        results: list[dict] = []
        files: list[Path]
        if root.is_file():
            files = [root]
        elif recursive:
            files = [p for p in root.rglob(glob) if p.is_file()]
        else:
            files = [p for p in root.glob(glob) if p.is_file()]

        truncated = False
        for fp in files:
            if len(results) >= max_results:
                truncated = True
                break
            try:
                with open(fp, "rb") as f:
                    head = f.read(4096)
                if b"\x00" in head:
                    continue  # treat as binary
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if rx.search(line):
                            results.append({
                                "path": str(fp),
                                "line_number": lineno,
                                "content": line.rstrip("\n"),
                            })
                            if len(results) >= max_results:
                                truncated = True
                                break
            except (PermissionError, OSError):
                continue
        out: dict = {"ok": True, "matches": results, "count": len(results)}
        if truncated:
            out["truncated"] = True
        return out

    @capability("exists")
    def _exists(self, path: str) -> dict:
        """Check if a path exists; reports the kind if so."""
        p = Path(path)
        if not p.exists() and not p.is_symlink():
            return {"ok": True, "exists": False}
        try:
            st = p.lstat()
        except OSError as e:
            fail(str(e))
        return {"ok": True, "exists": True, **_stat_to_meta(p, st)}


PLUGIN = FilePlugin
