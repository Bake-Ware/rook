"""Core v2 journal hash chains (spec 11 of docs/spec/core-v2.md).

Each entry names its chain and carries ``prev``, the hash of the entry
before it in that chain (GENESIS for the first). ``hash`` is SHA-256 over
the canonical entry without ``hash``, so it covers ``prev`` too.
"""
from __future__ import annotations

import hashlib

from .wire import canonical

GENESIS = "0" * 64


def entry_hash(entry: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in entry.items() if k != "hash"})).hexdigest()


def append(chain_head: str | None, entry: dict) -> dict:
    """Return ``entry`` with ``prev`` and ``hash`` set after ``chain_head``."""
    out = {**entry, "prev": chain_head or GENESIS}
    out["hash"] = entry_hash(out)
    return out


def first_break(entries: list, head: str | None = None) -> int | None:
    """Index of the first entry that doesn't follow from the one before it
    (``head`` is the hash before ``entries[0]``), or None if the chain holds."""
    prev = head or GENESIS
    for i, e in enumerate(entries):
        if e.get("prev") != prev or e.get("hash") != entry_hash(e):
            return i
        prev = e["hash"]
    return None
