"""Core v2 journal hash chains (spec 11 of docs/spec/core-v2.md).

Each entry names its chain and carries ``prev``, the hash of the entry
before it in that chain (GENESIS for the first). ``hash`` is SHA-256 over
the canonical entry without ``hash``, so it covers ``prev`` too.

Chains: one per project (``project:<name>``) plus ``general`` for sessions
with no project. Retention (11.4) prunes a chain from its start in whole
segments, never with gaps: a closed project's whole chain, or the general
chain one UTC day at a time. A segment is kept for the retention tier of the
riskiest entry in it.
"""
from __future__ import annotations

import hashlib
from typing import Iterable

from . import rights
from .wire import canonical

GENESIS = "0" * 64
GENERAL = "general"
DAY = 86400

# Days a segment is kept after it ends, by the riskiest entry in it.
RETENTION_DAYS = {"read": 30, "write": 30, "exec": 60, "admin": 90}


def entry_hash(entry: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in entry.items() if k != "hash"})).hexdigest()


def append(chain_head: str | None, entry: dict) -> dict:
    """Return ``entry`` with ``prev`` and ``hash`` set after ``chain_head``."""
    out = {**entry, "prev": chain_head or GENESIS}
    out["hash"] = entry_hash(out)
    return out


def first_break(entries: Iterable[dict], head: str | None = None) -> int | None:
    """Index of the first entry that doesn't follow from the one before it
    (``head`` is the hash before ``entries[0]``), or None if the chain holds."""
    prev = head or GENESIS
    for i, e in enumerate(entries):
        if e.get("prev") != prev or e.get("hash") != entry_hash(e):
            return i
        prev = e["hash"]
    return None


def chain_name(project: str | None) -> str:
    return f"project:{project}" if project else GENERAL


def stamp(entry: dict, session: dict | None) -> dict:
    """``entry`` with the session id, its project and the chain that project
    names. The hub does this; whatever chain the caller wrote is replaced."""
    project = (session or {}).get("project") or None
    out = {**entry, "project": project, "chain": chain_name(project)}
    if session:
        out["session"] = session.get("id")
    return out


def tier(risk) -> str:
    """The retention tier for a risk: read when there is none (session
    starts, notes), admin when it isn't a known risk (as in rights)."""
    return "read" if risk is None else rights.RISKS[rights.risk_rank(risk)]


def expired(end: float, risk, now: float, days: dict | None = None) -> bool:
    """Whether a segment that ended at ``end`` with ``risk`` as its riskiest
    entry is past retention at ``now``."""
    return now >= end + {**RETENTION_DAYS, **(days or {})}[tier(risk)] * DAY
