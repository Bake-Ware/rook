"""Role home directories on an agent host (docs/design/huddles.md 4.4).

Every role has a home directory, like a user's home::

    <roles dir>/<id>/            mode 0700
      role.json                  what the files below were rendered from
      memory/                    the role's memory (rook.worker.rolemem)
      workspace/                 its drafts, notes and working files (the only writable root)
      .mcp.json                  Claude Code: MCP servers
      .claude/settings.json      Claude Code: permissions, auto-memory off
      .codex/config.toml         Codex: MCP servers and tool allowlists
      .config/opencode/opencode.json   OpenCode: MCP servers, tools, permissions, agent
      .rook/                     host-local scratch, never moved

The home is the harness's working directory for every turn of the role, and
each harness loads its tools from the config files here (never from the
request). The files are rendered from what the hub sends with each turn (the
role's hub tools, built-ins and memory mode) and rewritten only when they
differ. Moving a role to another host is moving this folder
(:func:`export` / :func:`import_`).

Two MCP servers are named in every config: ``rook``, the hub's per-turn tool
server (bearer token from ``ROOK_TURN_TOKEN`` in the environment, never on
disk), and ``memory``, the stdio server over ``memory/``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

ROLE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
HUB = "rook"
MEMORY = "memory"
TOKEN_ENV = "ROOK_TURN_TOKEN"
SCRATCH = ".rook"
WORKSPACE = "workspace"
#: Claude Code's file tools for a role's workspace (``workspace`` on a turn):
#: Read, Glob and Grep anywhere in its home, Write and Edit only in workspace/.
WORKSPACE_CLAUDE = ("Read", "Write", "Edit", "Glob", "Grep")
MEMORY_MODES = ("write", "read", "off")
MEMORY_TOOLS = {"write": ("memory_recall", "memory_remember", "memory_forget"),
                "read": ("memory_recall",), "off": ()}
#: Built-in tool groups a role may be given, per harness. Codex has no file
#: tools without its shell, which stays off, so it gets none.
BUILTIN = {
    "read": {"claude": ("Read",), "opencode": ("read", "list"), "codex": ()},
    "search": {"claude": ("Grep", "Glob"), "opencode": ("grep", "glob"), "codex": ()},
}
#: Claude Code tools a role never gets, denied in its settings as well.
CLAUDE_DENY = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
               "Task", "KillShell", "BashOutput")
#: Codex built-in tool features turned off in a role's config (unknown names
#: are ignored by Codex). ``code_mode_host`` stays on: Codex calls MCP tools
#: through it.
CODEX_OFF = ("shell_tool", "unified_exec", "multi_agent", "image_generation",
             "browser_use", "browser_use_external", "computer_use", "plugins", "apps",
             "view_image", "hooks", "skill_search", "tool_suggest", "sleep_tool", "goals",
             "memories", "in_app_browser", "in_app_local_automation", "workspace_dependencies")
OPENCODE_AGENT = "rookrole"
#: Where OpenCode's config lives in a home (loaded with ``OPENCODE_CONFIG``: as
#: project config, OpenCode 1.18 fails every turn once a remote MCP server answers).
OPENCODE_CONFIG = ".config/opencode/opencode.json"
ARCHIVE_MAX = 8 * 1024 * 1024


class RoleHomeError(ValueError):
    pass


def check_id(role: str) -> str:
    role = str(role or "")
    if not ROLE_ID.match(role):
        raise RoleHomeError("role: 1-32 of a-z 0-9 _ -, starting with a letter")
    return role


def home_of(base: str | os.PathLike, role: str) -> Path:
    return Path(base) / check_id(role)


def ensure(base: str | os.PathLike, role: str) -> Path:
    """The role's home, created with its layout if missing."""
    home = home_of(base, role)
    for d in (home, home / "memory", home / WORKSPACE, home / SCRATCH):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    return home


def _toml(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=True)


def memory_server(home: Path, mode: str) -> tuple[str, list[str], dict]:
    """``(command, args, env)`` that starts the home's memory server with this
    host's Python and this copy of Rook (``-P``: never import from the home)."""
    root = str(Path(__file__).resolve().parents[2])
    args = ["-P"] if sys.version_info >= (3, 11) else []
    args += ["-m", "rook.worker.rolemem", str(home)]
    if mode == "read":
        args.append("--read-only")
    return sys.executable, args, {"PYTHONPATH": root}


def claude_tools(builtin: list[str], workspace: bool = False) -> list[str]:
    """Claude Code's ``--tools`` for a role turn."""
    out = [t for b in sorted(set(builtin or [])) for t in BUILTIN[b]["claude"]]
    if workspace:
        out += [t for t in WORKSPACE_CLAUDE if t not in out]
    return out


def files(home: Path, *, role: str, name: str = "", rev: int = 0, mcp_url: str,
          tools: list[str], builtin: list[str], memory: str = "write",
          workspace: bool = False) -> dict[str, str]:
    """Every config file of the home, by relative path, as text. With
    ``workspace`` (Claude Code only), its file tools work in the home, and
    ``workspace/`` is the only folder it may write."""
    if memory not in MEMORY_MODES:
        raise RoleHomeError(f"memory: one of {', '.join(MEMORY_MODES)}")
    tools = sorted({str(t) for t in tools or []})
    bad = [t for t in tools if not TOOL_NAME.match(t)]
    if bad:
        raise RoleHomeError(f"tool names: a-z 0-9 _ only ({bad[0]!r})")
    unknown = [b for b in builtin or [] if b not in BUILTIN]
    if unknown:
        raise RoleHomeError(f"builtin: any of {', '.join(BUILTIN)} ({unknown[0]!r})")
    builtin = sorted(set(builtin or []))
    mem_tools = list(MEMORY_TOOLS[memory])
    cmd, cmd_args, cmd_env = memory_server(home, memory)

    def native(harness: str) -> list[str]:
        return [t for b in builtin for t in BUILTIN[b][harness]]

    # -- Claude Code ---------------------------------------------------------
    servers: dict[str, Any] = {HUB: {"type": "http", "url": mcp_url,
                                     "headers": {"Authorization": "Bearer ${%s}" % TOKEN_ENV}}}
    if mem_tools:
        servers[MEMORY] = {"type": "stdio", "command": cmd, "args": cmd_args, "env": cmd_env}
    allow = ([f"mcp__{HUB}__{t}" for t in tools] + [f"mcp__{MEMORY}__{t}" for t in mem_tools]
             + native("claude"))
    deny = list(CLAUDE_DENY)
    if workspace:
        # "//" is an absolute path in Claude Code's rules; dontAsk refuses
        # anything not allowed, so writes anywhere else are refused.
        for root in dict.fromkeys((str(Path(home).absolute()), str(Path(home).resolve()))):
            ws = f"{root}/{WORKSPACE}"
            allow += [f"Read(/{root}/**)", f"Glob(/{root}/**)", f"Grep(/{root}/**)",
                      f"Write(/{ws}/**)", f"Edit(/{ws}/**)"]
        deny = [d for d in deny if d not in ("Edit", "Write", "MultiEdit")]
    claude_settings = {
        "permissions": {"allow": allow, "deny": deny, "defaultMode": "dontAsk"},
        "enableAllProjectMcpServers": False,
        "enabledMcpjsonServers": sorted(servers),
        "autoMemoryEnabled": False,
        "includeCoAuthoredBy": False,
        "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1"},
    }

    # -- Codex ---------------------------------------------------------------
    lines = ["# Generated by Rook from role " + role + "; change the role, not this file.",
             'approval_policy = "never"', 'sandbox_mode = "read-only"',
             'web_search = "disabled"', "",
             "[features]"]
    lines += [f"{f} = false" for f in CODEX_OFF]
    lines += ["", f"[mcp_servers.{HUB}]", f"url = {_toml(mcp_url)}",
              f"bearer_token_env_var = {_toml(TOKEN_ENV)}",
              f"enabled_tools = {_toml(tools)}",
              'default_tools_approval_mode = "approve"']
    if mem_tools:
        lines += ["", f"[mcp_servers.{MEMORY}]", f"command = {_toml(cmd)}",
                  f"args = {_toml(cmd_args)}", f"enabled_tools = {_toml(mem_tools)}",
                  'default_tools_approval_mode = "approve"',
                  f"[mcp_servers.{MEMORY}.env]", f"PYTHONPATH = {_toml(cmd_env['PYTHONPATH'])}"]

    # -- OpenCode ------------------------------------------------------------
    oc_tools: dict[str, bool] = {"*": False}
    for t in tools:
        oc_tools[f"{HUB}_{t}"] = True
    for t in mem_tools:
        oc_tools[f"{MEMORY}_{t}"] = True
    for t in native("opencode"):
        oc_tools[t] = True
    oc_mcp: dict[str, Any] = {HUB: {"type": "remote", "url": mcp_url, "enabled": True,
                                    "headers": {"Authorization": "Bearer {env:%s}" % TOKEN_ENV}}}
    if mem_tools:
        oc_mcp[MEMORY] = {"type": "local", "command": [cmd, *cmd_args], "enabled": True,
                          "environment": cmd_env}
    opencode = {
        "$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
        "mcp": oc_mcp, "tools": oc_tools,
        "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
        "agent": {OPENCODE_AGENT: {"mode": "primary", "tools": oc_tools,
                                   # relative to this file, in .config/opencode/
                                   "prompt": "{file:../../%s/system.md}" % SCRATCH}},
    }

    spec = {"id": role, "name": name or role, "rev": int(rev or 0), "tools": tools,
            "builtin": builtin, "memory": memory, "workspace": bool(workspace)}
    return {
        "role.json": json.dumps(spec, indent=2, sort_keys=True) + "\n",
        ".mcp.json": json.dumps({"mcpServers": servers}, indent=2) + "\n",
        ".claude/settings.json": json.dumps(claude_settings, indent=2) + "\n",
        ".codex/config.toml": "\n".join(lines) + "\n",
        OPENCODE_CONFIG: json.dumps(opencode, indent=2) + "\n",
    }


def render(home: Path, **kw) -> list[str]:
    """Write the home's config files; returns the ones that changed."""
    changed = []
    for rel, text in files(home, **kw).items():
        path = home / rel
        try:
            if path.read_text(encoding="utf-8") == text:
                continue
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        changed.append(rel)
    return changed


def info(home: Path) -> dict:
    spec = {}
    try:
        spec = json.loads((home / "role.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    mem = home / "memory"
    count = len(list(mem.glob("m_*.md"))) if mem.is_dir() else 0
    size = sum(p.stat().st_size for p in home.rglob("*") if p.is_file())
    return {"role": home.name, "path": str(home), "rev": spec.get("rev"),
            "tools": spec.get("tools"), "memory_mode": spec.get("memory"), "memories": count,
            "bytes": size}


def homes(base: str | os.PathLike) -> list[dict]:
    base = Path(base)
    if not base.is_dir():
        return []
    return [info(p) for p in sorted(base.iterdir()) if p.is_dir() and ROLE_ID.match(p.name)]


# -- moving ---------------------------------------------------------------------------

def _skip(rel: Path) -> bool:
    return bool(rel.parts) and rel.parts[0] == SCRATCH


def export(base: str | os.PathLike, role: str) -> dict:
    """The home as a base64 tar.gz (without ``.rook/``) and its sha256."""
    home = home_of(base, role)
    if not home.is_dir():
        raise RoleHomeError(f"no home for role {role!r} on this host")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(home.rglob("*")):
            rel = p.relative_to(home)
            if _skip(rel) or p.is_symlink() or not (p.is_file() or p.is_dir()):
                continue
            tar.add(p, arcname=str(rel), recursive=False)
    data = buf.getvalue()
    if len(data) > ARCHIVE_MAX:
        raise RoleHomeError("the role's home is too large to move over the band")
    return {"role": role, "archive": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def set_aside(base: str | os.PathLike, role: str, why: str = "moved") -> str | None:
    """Rename a home out of the way (never deleted); returns its new name."""
    home = home_of(base, role)
    if not home.exists():
        return None
    dest = home.with_name(f"{role}.{why}-{time.strftime('%Y%m%d-%H%M%S')}")
    n = 1
    while dest.exists():
        dest = home.with_name(f"{role}.{why}-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
        n += 1
    home.rename(dest)
    return dest.name


def import_(base: str | os.PathLike, role: str, archive: str, sha256: str,
            replace: bool = False) -> dict:
    """Unpack an exported home here. An existing home is set aside first only
    with ``replace``; otherwise it is refused."""
    home = home_of(base, role)
    try:
        data = base64.b64decode(archive, validate=True)
    except (ValueError, TypeError):
        raise RoleHomeError("archive: base64 of a tar.gz") from None
    if len(data) > ARCHIVE_MAX:
        raise RoleHomeError("archive too large")
    if hashlib.sha256(data).hexdigest() != str(sha256 or ""):
        raise RoleHomeError("archive does not match its sha256")
    aside = None
    if home.exists():
        if not replace:
            raise RoleHomeError(f"role {role!r} already has a home here (replace to set it aside)")
        aside = set_aside(base, role, "replaced")
    tmp = home.with_name(f".{role}.import")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            rel = Path(m.name)
            if rel.is_absolute() or ".." in rel.parts or not (m.isfile() or m.isdir()) \
                    or _skip(rel):
                continue
            dest = tmp / rel
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is not None:
                dest.write_bytes(src.read())
    tmp.rename(home)
    ensure(base, role)
    out = info(home)
    if aside:
        out["set_aside"] = aside
    return out
