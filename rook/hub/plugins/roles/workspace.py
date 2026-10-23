"""A role's workspace: ``<home>/workspace/`` on its agent host
(docs/design/huddles.md 4.4, 4.5).

Its drafts, notes, scratch files and working documents. The role reads,
writes, lists, searches and deletes there freely, with no card; nothing
outside it is writable this way. Tasks are for work items, never a notepad.

Two ways in, one folder:

* **Native** (Claude Code on an agent host that announces
  ``hb.agenthost.workspace``): the home's settings allow Read, Write, Edit,
  Glob and Grep, with ``workspace/`` the only writable root
  (:mod:`rook.worker.rolehome`).
* **Hub tools** (every other harness and host): ``workspace_list``,
  ``workspace_read``, ``workspace_write``, ``workspace_edit``,
  ``workspace_search`` and ``workspace_delete``, served by the hub's per-turn
  tool server. The hub checks the path (relative, inside ``workspace/``, no
  ``..``, no hidden top-level config) and then runs the host's own
  ``file.*`` caps (``shell.exec`` ``rm`` for a delete) as hub system code,
  journaled as the role.

People see the folder read-only on the role's page (``role.workspace``).
"""
from __future__ import annotations

import posixpath
import re
from typing import Any

DIR = "workspace"
#: Largest file the tools read or write (characters).
MAX_FILE = 200_000
#: Most entries a listing returns.
MAX_LIST = 500
NAME = re.compile(r"^[A-Za-z0-9._@+=,~ -]{1,120}$")


class WorkspaceError(ValueError):
    pass


def clean(path: Any, allow_root: bool = False) -> str:
    """A path inside the workspace, normalised (``notes/a.md``), or
    WorkspaceError. ``allow_root``: "" (the folder itself) is fine."""
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.startswith(DIR + "/"):
        p = p[len(DIR) + 1:]                     # "workspace/x" means x
    if p in ("", ".", DIR):
        if allow_root:
            return ""
        raise WorkspaceError("give a file path inside your workspace, like notes/plan.md")
    if p.startswith("/") or p.startswith("~"):
        raise WorkspaceError("paths are relative to your workspace (notes/plan.md), never "
                             "absolute: nothing outside it is yours to write")
    parts = p.split("/")
    if any(x in ("", ".", "..") for x in parts):
        raise WorkspaceError("a workspace path has no empty, '.' or '..' parts")
    bad = next((x for x in parts if not NAME.match(x)), None)
    if bad is not None:
        raise WorkspaceError(f"{bad!r}: names use letters, digits, spaces and . _ - @ + = , ~")
    if len(parts) > 12:
        raise WorkspaceError("at most 12 folders deep")
    return posixpath.join(*parts)


def root_of(home: str) -> str:
    return posixpath.join(home.rstrip("/"), DIR)


def absolute(home: str, rel: str) -> str:
    return posixpath.join(root_of(home), rel) if rel else root_of(home)


def _fn(name: str, description: str, props: dict | None = None,
        required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {},
                       **({"required": required} if required else {})}}}


_PATH = {"path": {"type": "string", "description": "relative to your workspace, e.g. notes/plan.md"}}
TOOLS = {
    "workspace_list": _fn("workspace_list", "List the files in your workspace (your own folder for "
                                            "drafts, notes and working documents; no approval "
                                            "needed). path: a folder in it (blank: all of it).",
                          {"path": {"type": "string"}}),
    "workspace_read": _fn("workspace_read", "Read a file from your workspace.", _PATH, ["path"]),
    "workspace_write": _fn("workspace_write", "Create or replace a file in your workspace (folders "
                                              "are made as needed). append: add to the end.",
                           {**_PATH, "content": {"type": "string"},
                            "append": {"type": "boolean"}}, ["path", "content"]),
    "workspace_edit": _fn("workspace_edit", "Replace one exact piece of text in a workspace file "
                                            "(old must appear exactly once).",
                          {**_PATH, "old": {"type": "string"}, "new": {"type": "string"}},
                          ["path", "old", "new"]),
    "workspace_search": _fn("workspace_search", "Search your workspace's files for a regular "
                                                "expression.",
                            {"pattern": {"type": "string"}, "path": {"type": "string"}},
                            ["pattern"]),
    "workspace_delete": _fn("workspace_delete", "Delete a file from your workspace.", _PATH,
                            ["path"]),
}


class Workspace:
    """The file operations, through caps on the role's agent host.

    ``call(cap, args) -> result dict`` runs a cap on that host as hub system
    code (raising on failure); ``home`` is the role's home path there."""

    def __init__(self, call, home: str) -> None:
        self.call = call
        self.home = home.rstrip("/")
        self.root = root_of(self.home)

    async def _guard(self, rel: str, final: bool = True) -> dict:
        """Refuse a path that runs through a symlink (one a person or another
        process put there could point outside the workspace). Returns the
        target's lstat (``{exists, type}``)."""
        parts = rel.split("/") if rel else []
        here = self.root
        st = await self.call("file.exists", {"path": here})
        if st.get("exists") and st.get("type") != "dir":
            raise WorkspaceError("the workspace folder is not a plain folder; ask a person")
        for i, part in enumerate(parts):
            here = posixpath.join(here, part)
            st = await self.call("file.exists", {"path": here})
            if not st.get("exists"):
                return {"exists": False}
            last = i == len(parts) - 1
            if st.get("type") == "symlink":
                raise WorkspaceError(f"{posixpath.join(*parts[:i + 1])} is a link; links are "
                                     "not followed in a workspace")
            if not last and st.get("type") != "dir":
                raise WorkspaceError(f"{posixpath.join(*parts[:i + 1])} is not a folder")
        return st if parts else {"exists": st.get("exists"), "type": "dir"}

    async def list(self, path: str = "") -> dict:
        rel = clean(path, allow_root=True)
        target = absolute(self.home, rel)
        if not (await self._guard(rel)).get("exists"):
            return {"ok": True, "path": rel, "files": [], "note": "empty: nothing written yet"}
        res = await self.call("file.list", {"path": target, "recursive": True,
                                            "include_hidden": False, "max_entries": MAX_LIST})
        files = [{"path": posixpath.join(rel, e.get("path") or e["name"]) if rel
                  else (e.get("path") or e["name"]),
                  "size": e.get("size"), "modified": e.get("modified")}
                 for e in res.get("entries") or [] if e.get("type") == "file"
                 and not str(e.get("path") or e["name"]).startswith(".")]
        files.sort(key=lambda f: f["path"])
        out = {"ok": True, "path": rel, "files": files}
        if res.get("truncated"):
            out["truncated"] = True
        return out

    async def read(self, path: str) -> dict:
        rel = clean(path)
        st = await self._guard(rel)
        if not st.get("exists"):
            raise WorkspaceError(f"no file {rel} in your workspace (workspace_list shows them)")
        if st.get("type") != "file":
            raise WorkspaceError(f"{rel} is a {st.get('type')}, not a file")
        res = await self.call("file.read", {"path": absolute(self.home, rel),
                                            "max_bytes": MAX_FILE})
        out = {"ok": True, "path": rel, "content": res.get("content", ""),
               "size": res.get("size_bytes")}
        if res.get("truncated"):
            out["truncated"] = f"[truncated: {MAX_FILE} of {res.get('size_bytes')} bytes]"
        return out

    async def write(self, path: str, content: str, append: bool = False) -> dict:
        rel = clean(path)
        content = str(content if content is not None else "")
        if len(content) > MAX_FILE:
            raise WorkspaceError(f"at most {MAX_FILE} characters per write; split the file")
        st = await self._guard(rel)
        if st.get("exists") and st.get("type") != "file":
            raise WorkspaceError(f"{rel} is a {st.get('type')}, not a file")
        res = await self.call("file.write", {"path": absolute(self.home, rel), "content": content,
                                             "append": bool(append), "create_parents": True})
        return {"ok": True, "path": rel, "written_bytes": res.get("written_bytes"),
                "appended": bool(append)}

    async def edit(self, path: str, old: str, new: str) -> dict:
        rel = clean(path)
        cur = await self.read(rel)
        if cur.get("truncated"):
            raise WorkspaceError("that file is too large to edit here; rewrite it in parts")
        text = cur["content"]
        n = text.count(str(old or ""))
        if not old or n != 1:
            raise WorkspaceError(f"old must appear exactly once in {rel} (found {n} times)")
        return await self.write(rel, text.replace(old, str(new or ""), 1))

    async def search(self, pattern: str, path: str = "") -> dict:
        rel = clean(path, allow_root=True)
        target = absolute(self.home, rel)
        if not (await self._guard(rel)).get("exists"):
            return {"ok": True, "matches": [], "count": 0}
        res = await self.call("file.search", {"pattern": str(pattern or ""), "path": target,
                                              "max_results": 100})
        cut = self.root + "/"
        matches = [{"path": str(m.get("path", "")).removeprefix(cut),
                    "line": m.get("line_number"), "text": str(m.get("content", ""))[:300]}
                   for m in res.get("matches") or []]
        return {"ok": True, "matches": matches, "count": len(matches),
                **({"truncated": True} if res.get("truncated") else {})}

    async def delete(self, path: str) -> dict:
        rel = clean(path)
        target = absolute(self.home, rel)
        st = await self._guard(rel)
        if not st.get("exists"):
            raise WorkspaceError(f"no file {rel} in your workspace")
        if st.get("type") != "file":
            raise WorkspaceError(f"{rel} is a {st.get('type')}; only files are deleted")
        await self.call("shell.exec", {"argv": ["rm", "-f", "--", target], "timeout": 15})
        return {"ok": True, "path": rel, "deleted": True}

    async def run(self, name: str, args: dict) -> dict:
        """One workspace tool call from a role turn."""
        a = args if isinstance(args, dict) else {}
        if name == "workspace_list":
            return await self.list(a.get("path") or "")
        if name == "workspace_read":
            return await self.read(a.get("path"))
        if name == "workspace_write":
            return await self.write(a.get("path"), a.get("content"), bool(a.get("append")))
        if name == "workspace_edit":
            return await self.edit(a.get("path"), a.get("old"), a.get("new"))
        if name == "workspace_search":
            return await self.search(a.get("pattern"), a.get("path") or "")
        if name == "workspace_delete":
            return await self.delete(a.get("path"))
        raise WorkspaceError(f"unknown workspace tool {name!r}")
