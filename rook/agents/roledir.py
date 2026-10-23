"""Role directories on the session host (docs/design/persistent-agents.md 3.1).

The hub renders a role's files (``CLAUDE.md``, ``.claude/settings.json``,
``.mcp.json``, ``voice/...``) and sends them in ``configure``; this module
writes them, atomically and only when their text changed, never outside the
role directory and never into the host-local ``.rook/``.

Two things are merged into the role's main ``.mcp.json`` here, on the host,
because their definitions never leave it: MCP servers granted from the host's
own Claude Code user config (``host_servers``, by name) and the role's memory
server (``memory`` write/read, :func:`rook.worker.rolehome.memory_server`).

``.rook/sessions.json`` keeps the session id of each lane (and resume and
reset counts), so a restart resumes the same conversation.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any

from ..worker import rolehome

ROLE_ID = rolehome.ROLE_ID
SCRATCH = rolehome.SCRATCH
LAYOUT = ("memory", "workspace", SCRATCH, "voice")
#: Directories the hub may not write into: host-local state and side threads.
RESERVED = (SCRATCH, "forks")
MAX_FILE = 1024 * 1024
MAX_FILES = 200
MCP_FILE = ".mcp.json"
#: Where the hub's own ``.mcp.json`` is kept, to merge host servers into again.
HUB_MCP = f"{SCRATCH}/hub.mcp.json"
SESSIONS = f"{SCRATCH}/sessions.json"
MEMORY_MODES = rolehome.MEMORY_MODES
SERVER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class RoleDirError(ValueError):
    pass


def check_role(role: Any) -> str:
    role = str(role or "")
    if not ROLE_ID.match(role):
        raise RoleDirError("role: 1-32 of a-z 0-9 _ -, starting with a letter")
    return role


def ensure(base: str | os.PathLike, role: str) -> Path:
    """The role directory with its layout, mode 0700."""
    d = Path(base) / check_role(role)
    d.mkdir(parents=True, exist_ok=True)
    for sub in LAYOUT:
        (d / sub).mkdir(exist_ok=True)
    os.chmod(d, 0o700)
    return d


def check_rel(rel: Any) -> str:
    """A relative path the hub may write: no absolute paths, no ``..``, nothing
    in ``.rook/`` or ``forks/``."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        raise RoleDirError(f"bad file path {rel!r}")
    p = PurePosixPath(rel)
    if p.is_absolute() or rel.startswith("~") or re.match(r"^[A-Za-z]:", rel):
        raise RoleDirError(f"absolute file path {rel!r}")
    parts = [x for x in p.parts if x not in ("", ".")]
    if not parts or ".." in parts:
        raise RoleDirError(f"file path {rel!r} leaves the role directory")
    if parts[0] in RESERVED:
        raise RoleDirError(f"file path {rel!r} is in a host-local directory")
    return "/".join(parts)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def validate_files(files: Any) -> dict[str, str]:
    """``{relpath: text}`` checked as a whole before anything is written."""
    if files is None:
        return {}
    if not isinstance(files, dict):
        raise RoleDirError("dir_files: {relpath: text}")
    if len(files) > MAX_FILES:
        raise RoleDirError("dir_files: too many files")
    out = {}
    for rel, text in files.items():
        clean = check_rel(rel)
        if not isinstance(text, str):
            raise RoleDirError(f"{rel}: text expected")
        if len(text.encode("utf-8")) > MAX_FILE:
            raise RoleDirError(f"{rel}: larger than {MAX_FILE} bytes")
        out[clean] = text
    return out


def write_files(role_dir: Path, files: dict[str, str]) -> list[str]:
    """Write validated files whose text changed; returns the changed paths."""
    for rel in files:
        # Symlinks on the way (or the file itself) must not lead outside.
        path = role_dir / rel
        probe = path
        while not probe.exists() and not probe.is_symlink() and probe != role_dir:
            probe = probe.parent
        if path.is_symlink() or not _inside(role_dir, probe):
            raise RoleDirError(f"file path {rel!r} leaves the role directory")
    changed = []
    for rel, text in files.items():
        path = role_dir / rel
        if read_text(path) == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, text)
        changed.append(rel)
    return changed


# -- MCP servers from the host's own config -----------------------------------------

def user_servers(user_config: str | os.PathLike) -> dict[str, dict]:
    """``mcpServers`` of the host's Claude Code user config (definitions stay here)."""
    try:
        data = json.loads(Path(user_config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {k: v for k, v in servers.items() if SERVER_NAME.match(str(k)) and isinstance(v, dict)}


def memory_server(role_dir: Path, mode: str) -> dict:
    cmd, args, env = rolehome.memory_server(role_dir, mode)
    return {"type": "stdio", "command": cmd, "args": args, "env": env}


def merged_mcp(role_dir: Path, hub_text: str | None, host_servers: list[str],
               available: dict[str, dict], memory: str) -> tuple[str | None, list[str]]:
    """The main directory's ``.mcp.json``: the hub's file plus the granted host
    servers and the memory server (the hub's own definitions win). Returns
    ``(text or None when there is nothing to write, missing server names)``."""
    missing = [n for n in host_servers if n not in available]
    if hub_text is None and not host_servers and memory == "off":
        return None, missing
    try:
        doc = json.loads(hub_text) if hub_text else {}
    except ValueError:
        raise RoleDirError(".mcp.json from the hub is not JSON") from None
    if not isinstance(doc, dict):
        raise RoleDirError(".mcp.json from the hub is not a JSON object")
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers = dict(servers)
    for name in host_servers:
        if name in available and name not in servers:
            servers[name] = available[name]
    if memory != "off" and rolehome.MEMORY not in servers:
        servers[rolehome.MEMORY] = memory_server(role_dir, memory)
    out = dict(doc)
    out["mcpServers"] = servers
    return json.dumps(out, indent=2) + "\n", missing


# -- session records ---------------------------------------------------------------

class Sessions:
    """``.rook/sessions.json``: per lane {id, started, resumes, resets, last_turn_uuid}."""

    def __init__(self, role_dir: Path) -> None:
        self.path = role_dir / SESSIONS

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, lane: str) -> dict:
        rec = self.load().get(lane)
        return dict(rec) if isinstance(rec, dict) else {}

    def put(self, lane: str, rec: dict) -> None:
        data = self.load()
        data[lane] = rec
        write_text(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def update(self, lane: str, **fields) -> dict:
        rec = self.get(lane)
        rec.update(fields)
        self.put(lane, rec)
        return rec


def transcript_exists(claude_config_dir: str | os.PathLike, session_id: str) -> bool:
    """Whether Claude Code has a transcript for ``session_id`` (any project dir)."""
    if not session_id or not SESSION_ID.match(session_id):
        return False
    pattern = os.path.join(glob.escape(str(claude_config_dir)), "projects", "*",
                           f"{session_id}.jsonl")
    return any(os.path.isfile(p) for p in glob.glob(pattern))


def now() -> float:
    return round(time.time(), 3)
