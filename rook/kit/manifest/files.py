"""Canonical JSON, the plugin folder's file-hash list, and its trust class."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

SIG_FILE = "plugin.sig"

_CONFIG = {".yaml", ".yml", ".md", ".json", ".txt"}
_PAGE = {".html", ".htm", ".js", ".mjs", ".css", ".svg", ".png", ".jpg", ".jpeg",
         ".gif", ".webp", ".ico", ".woff", ".woff2"}


def canonical_json(data) -> bytes:
    """Sorted keys, no whitespace, ASCII-escaped: the form manifests are
    hashed and signed in (same rules as rook.core.authz.canonical)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _walk(folder: Path):
    for p in sorted(folder.rglob("*")):
        if p.is_symlink():
            raise ValueError(f"{p.relative_to(folder).as_posix()}: symlinks are not allowed in a plugin")
        if p.is_file():
            rel = p.relative_to(folder).as_posix()
            if rel != SIG_FILE:
                yield rel, p


def file_hashes(folder) -> dict[str, str]:
    """Every file in the plugin folder except plugin.sig: posix relative path
    -> sha256 hex, sorted by path. This list is what the hub signs."""
    folder = Path(folder)
    return {rel: hashlib.sha256(p.read_bytes()).hexdigest() for rel, p in _walk(folder)}


def trust_class(folder) -> str:
    """Derived, never declared: ``code`` if anything could run as Python (or
    is a file type we don't recognise), ``page`` if it ships sandboxed web
    files, else ``config`` (YAML and markdown)."""
    found = "config"
    for rel, p in _walk(Path(folder)):
        ext = p.suffix.lower()
        if ext in _PAGE:
            found = "page"
        elif ext not in _CONFIG:
            return "code"
    return found
