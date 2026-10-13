"""Build/version identity for the worker bundle.

These values are the committed *defaults* — what you get running from a source
checkout. At bundle time, ``rook/remote/build_band_worker.py`` overwrites this
file with the real stamp (git commit count + short hash + build time).

``BUILD`` is the monotonic comparator the OTA self-update uses: a worker updates
only when a signed manifest advertises a ``build`` strictly greater than its own.
An unstamped/dev worker has ``BUILD = 0``, which is never greater than any
published build, so it never auto-updates itself out from under a developer.

``VERSION`` for a source run is ``0.dev+<short commit>`` when this file sits in
a git checkout (so two dev hubs on different commits are told apart), else
``0.dev``. It stays ``0.``-prefixed, so it still parses as build 0.
"""

from __future__ import annotations

import re
from pathlib import Path


def _source_commit() -> str:
    """HEAD of the git checkout this file lives in, read from ``.git`` once at
    import (no subprocess: this runs in every worker and the hub), or ""."""
    try:
        root = Path(__file__).resolve().parents[2]
        dotgit = root / ".git"
        if dotgit.is_file():      # a worktree: "gitdir: <path>"
            text = dotgit.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return ""
            gitdir = Path(text[len("gitdir:"):].strip())
            gitdir = gitdir if gitdir.is_absolute() else root / gitdir
        elif dotgit.is_dir():
            gitdir = dotgit
        else:
            return ""
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        sha = head
        if head.startswith("ref:"):
            ref = head[len("ref:"):].strip()
            common = gitdir
            if (gitdir / "commondir").is_file():
                common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
            sha = ""
            for d in (gitdir, common):
                if (d / ref).is_file():
                    sha = (d / ref).read_text(encoding="utf-8").strip()
                    break
            if not sha and (common / "packed-refs").is_file():
                for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        sha = line.split(" ", 1)[0]
                        break
        return sha.lower() if re.fullmatch(r"[0-9a-fA-F]{40,64}", sha) else ""
    except (OSError, ValueError):
        return ""


_SOURCE_COMMIT = _source_commit()

BUILD = 0            # monotonic integer (git rev-list --count HEAD); 0 = unstamped/dev
VERSION = f"0.dev+{_SOURCE_COMMIT[:7]}" if _SOURCE_COMMIT else "0.dev"
COMMIT = ""          # git short hash (stamped bundles only)
BUILT_AT = ""        # ISO-8601 UTC build timestamp


def as_dict() -> dict:
    return {"version": VERSION, "build": BUILD, "commit": COMMIT, "built_at": BUILT_AT}
