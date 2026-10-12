"""Where the local coding agents (Claude Code, Codex) keep their state, and
how to find their CLIs.

One place for what claude-history, codex-history, sessions, terminals and the
``rook sessions`` CLI each used to work out for themselves. Stdlib only.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable

_IS_WIN = sys.platform == "win32"


def home() -> Path:
    """The user's home as the agents see it (``%USERPROFILE%`` on Windows)."""
    if _IS_WIN:
        return Path(os.environ.get("USERPROFILE", str(Path.home())))
    return Path.home()


def claude_home() -> Path:
    """``~/.claude``: Claude Code's settings, history index and transcripts."""
    return home() / ".claude"


def claude_projects() -> Path:
    """``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``."""
    return claude_home() / "projects"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def codex_sessions() -> Path:
    """``$CODEX_HOME/sessions`` (rollouts; ``archived_sessions`` sits beside it)."""
    return codex_home() / "sessions"


def expand(path: str | None, default: Callable[[], Path]) -> Path:
    """A caller-supplied root with ``~`` and ``$VARS`` expanded, else ``default()``."""
    if not path:
        return default()
    return Path(os.path.expanduser(os.path.expandvars(path)))


def short_id(session_id: str) -> str:
    """The first group of a session UUID, as the agents print it."""
    return session_id.split("-", 1)[0] if session_id else ""


# Where npm puts global CLIs on Windows when they are not on the worker's PATH.
def _windows_npm_dirs() -> list[str]:
    return [r"C:\nvm4w\nodejs", str(Path(os.environ.get("APPDATA", "")) / "npm"),
            r"C:\Program Files\nodejs"]


def agent_bin(name: str) -> str | None:
    """Locate an agent CLI (``claude``, ``codex``). On Windows npm installs
    them as ``.cmd`` shims, which ``shutil.which`` misses unless asked by that
    name; elsewhere never the Rook session shim, always the real binary."""
    if _IS_WIN:
        found = shutil.which(name) or shutil.which(name + ".cmd")
        if found:
            return found
        for d in _windows_npm_dirs():
            c = os.path.join(d, name + ".cmd")
            if os.path.exists(c):
                return c
        return None
    from .shim import which_real
    return which_real(name)


def claude_bin() -> str | None:
    return agent_bin("claude")


def codex_bin() -> str | None:
    return agent_bin("codex")
