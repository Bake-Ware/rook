"""Speakable record names: ``brave-otter-lamp``.

Console rooms, handoff threads, tasks, projects, jobs, job runs, huddles,
huddle cards and chat rooms each get a three-word name a person can say
out loud. The name is an alias. The record's hex or prefixed id stays the
canonical key (storage, foreign keys, links, the journal); the name sits
beside it in a ``record_names`` table inside the store's own database.
Names are unique per kind per band and never change once assigned.

The table is additive: dropping it loses no record data, and a release that
predates it ignores it. Every store creates it on open and backfills names
for records that have none (:func:`backfill`), so upgrading is idempotent.

Resolution (:func:`resolve`) accepts the id, or the name in any case with
spaces, hyphens or underscores between the words (``Brave Otter lamp``,
``brave_otter_lamp``). Near misses (one misheard word, a small typo) come
back as suggestions; a read may use a single clear near miss, a write never
does (``write=True`` needs the exact name or the id).

The word lists (``name_words/``) are curated for speech: short common words,
no homophones within the list, nothing offensive, violent, sexual or brand
named, and nothing that sounds like a command. ``tests/test_names.py`` checks
those rules.

This module is stdlib-only and loads its word lists lazily, so importing it
costs nothing where names are never generated (the worker bundle).
"""
from __future__ import annotations

import difflib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from typing import Callable, Iterable

__all__ = [
    "KINDS", "adjectives", "nouns", "words", "generate", "normalize",
    "is_name", "looks_like_name", "ensure_table", "assign", "name_of",
    "names_of", "find", "suggest", "backfill", "drop", "resolve", "Resolved",
    "NameError_", "with_name", "label",
]

#: The record kinds that carry a name. A huddle is a chat room, so it shares
#: the room's name (kind ``room``).
KINDS = ("console", "handoff", "task", "project", "job", "run", "room", "card")

TABLE = "record_names"

_DDL = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    " kind TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,"
    " band TEXT NOT NULL DEFAULT '', created REAL NOT NULL,"
    " PRIMARY KEY (kind, id))",
    f"CREATE UNIQUE INDEX IF NOT EXISTS {TABLE}_name ON {TABLE}(kind, band, name)",
)

_SPLIT = re.compile(r"[\s_\-.]+")


@cache
def _load(fname: str) -> tuple[str, ...]:
    text = files(__package__).joinpath("name_words", fname).read_text(encoding="ascii")
    out = tuple(w.strip() for w in text.splitlines() if w.strip() and not w.startswith("#"))
    if len(out) < 100 or len(set(out)) != len(out) or any(
            not (w.isascii() and w.isalpha() and w.islower()) for w in out):
        raise ValueError(f"invalid name word list {fname}")
    return out


def adjectives() -> tuple[str, ...]:
    return _load("adjectives.txt")


def nouns() -> tuple[str, ...]:
    return _load("nouns.txt")


@cache
def words() -> frozenset[str]:
    return frozenset(adjectives()) | frozenset(nouns())


def generate(rng: "secrets.SystemRandom | None" = None) -> str:
    """A fresh name: adjective-noun-noun, the two nouns different."""
    choice = (rng or secrets.SystemRandom()).choice
    a, n = adjectives(), nouns()
    first = choice(n)
    second = choice(n)
    while second == first:
        second = choice(n)
    return f"{choice(a)}-{first}-{second}"


def normalize(text) -> str | None:
    """``'Brave Otter_lamp'`` -> ``'brave-otter-lamp'``; None unless ``text``
    is exactly three alphabetic words (whether they are list words is not
    checked here)."""
    if not isinstance(text, str):
        return None
    parts = [p for p in _SPLIT.split(text.strip().lower()) if p]
    if len(parts) != 3 or not all(p.isascii() and p.isalpha() for p in parts):
        return None
    return "-".join(parts)


def is_name(text) -> bool:
    """True if ``text`` normalizes to three list words."""
    n = normalize(text)
    return bool(n) and all(w in words() for w in n.split("-"))


def looks_like_name(text) -> bool:
    """Three alphabetic words: worth a name lookup (and fuzzy suggestions)."""
    return normalize(text) is not None


# -- storage ------------------------------------------------------------------

def ensure_table(conn: sqlite3.Connection) -> None:
    for stmt in _DDL:
        conn.execute(stmt)


def name_of(conn: sqlite3.Connection, kind: str, rid: str) -> str | None:
    row = conn.execute(f"SELECT name FROM {TABLE} WHERE kind=? AND id=?",
                       (kind, str(rid))).fetchone()
    return row[0] if row else None


def names_of(conn: sqlite3.Connection, kind: str, ids: Iterable[str]) -> dict[str, str]:
    ids = [str(i) for i in ids if i]
    out: dict[str, str] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT id, name FROM {TABLE} WHERE kind=? AND id IN ({q})",
                (kind, *chunk)):
            out[row[0]] = row[1]
    return out


def assign(conn: sqlite3.Connection, kind: str, rid: str, *, band: str = "",
           rng=None, attempts: int = 64) -> str:
    """The record's name, assigning a fresh one if it has none. Idempotent.
    The caller commits (or runs in autocommit)."""
    rid = str(rid)
    have = name_of(conn, kind, rid)
    if have:
        return have
    for _ in range(attempts):
        name = generate(rng)
        try:
            conn.execute(
                f"INSERT INTO {TABLE}(kind, id, name, band, created) VALUES (?,?,?,?,?)",
                (kind, rid, name, band or "", time.time()))
            return name
        except sqlite3.IntegrityError:
            # Either the name is taken (draw again) or another writer named
            # this record first (use theirs).
            have = name_of(conn, kind, rid)
            if have:
                return have
    raise RuntimeError(f"could not find a free name for {kind} {rid}")


def backfill(conn: sqlite3.Connection, kind: str,
             rows: Iterable[tuple[str, str] | str], *, rng=None) -> int:
    """Name every record in ``rows`` (ids, or ``(id, band)`` pairs) that has
    no name yet. Returns how many were named. Safe to run on every open."""
    have = {r[0] for r in conn.execute(f"SELECT id FROM {TABLE} WHERE kind=?", (kind,))}
    n = 0
    for row in rows:
        rid, band = (row, "") if isinstance(row, str) else (row[0], row[1] or "")
        if rid and str(rid) not in have:
            assign(conn, kind, str(rid), band=band, rng=rng)
            have.add(str(rid))
            n += 1
    return n


def drop(conn: sqlite3.Connection) -> None:
    """Remove every name (the reverse of the migration). Records are untouched."""
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}")


def find(conn: sqlite3.Connection, kind: str, text, *, band: str | None = None) -> list[str]:
    """Ids whose name is exactly ``text`` (normalized); more than one only
    across bands."""
    n = normalize(text)
    if not n:
        return []
    sql = f"SELECT id FROM {TABLE} WHERE kind=? AND name=?"
    args: list = [kind, n]
    if band is not None:
        sql += " AND band=?"
        args.append(band)
    return [r[0] for r in conn.execute(sql, args)]


def _close_word(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True  # plural or a clipped word
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def suggest(conn: sqlite3.Connection, kind: str, text, *, band: str | None = None,
            limit: int = 5) -> list[tuple[str, str]]:
    """``(id, name)`` of near misses for ``text``: names sharing two of the
    three words in place with the third close in spelling, or within a small
    edit distance overall. Best first."""
    n = normalize(text)
    if not n:
        return []
    want = n.split("-")
    sql = f"SELECT id, name FROM {TABLE} WHERE kind=?"
    args: list = [kind]
    if band is not None:
        sql += " AND band=?"
        args.append(band)
    scored = []
    for rid, name in conn.execute(sql, args):
        if name == n:
            continue
        got = name.split("-")
        same = sum(1 for x, y in zip(want, got) if x == y)
        ratio = difflib.SequenceMatcher(None, n, name).ratio()
        if same == 2 and all(_close_word(x, y) for x, y in zip(want, got)):
            scored.append((2 + ratio, rid, name))
        elif ratio >= 0.88:
            scored.append((ratio, rid, name))
    scored.sort(key=lambda t: -t[0])
    return [(rid, name) for _, rid, name in scored[:limit]]


# -- resolution ---------------------------------------------------------------

class NameError_(LookupError):
    """A ref that is neither an id nor an exact name. ``candidates`` holds
    ``(id, name)`` near misses (or the several exact matches when the name
    is ambiguous)."""

    def __init__(self, message: str, candidates: list[tuple[str, str]] | None = None,
                 ambiguous: bool = False):
        super().__init__(message)
        self.candidates = list(candidates or [])
        self.ambiguous = ambiguous

    def payload(self) -> dict:
        out: dict = {"error": str(self)}
        if self.candidates:
            out["candidates"] = [{"id": i, "name": n} for i, n in self.candidates]
        if self.ambiguous:
            out["ambiguous"] = True
        return out


@dataclass
class Resolved:
    id: str
    name: str | None = None
    via: str = "id"            # id | name | fuzzy
    candidates: list = field(default_factory=list)


def resolve(conn: sqlite3.Connection, kind: str, ref, *,
            exists: Callable[[str], bool] | None = None,
            write: bool = False, band: str | None = None,
            noun: str | None = None) -> Resolved:
    """Turn an id or a name into the record's id.

    ``exists(ref)`` says whether ``ref`` is already a known id (checked
    first, so an id always wins). An exact name (any case and separators)
    resolves; several exact matches (only possible across bands) raise with
    the candidates. A near miss resolves only for a read (``write=False``)
    with a single clear candidate; otherwise :class:`NameError_` carries the
    suggestions. A ref that does not look like a name is returned unchanged
    (the caller reports its own "not found")."""
    ref = "" if ref is None else str(ref).strip()
    what = noun or kind
    if not ref:
        return Resolved(id=ref)
    if exists is not None and exists(ref):
        return Resolved(id=ref, name=name_of(conn, kind, ref), via="id")
    if not looks_like_name(ref):
        return Resolved(id=ref)
    hits = find(conn, kind, ref, band=band)
    if len(hits) == 1:
        return Resolved(id=hits[0], name=normalize(ref), via="name")
    if len(hits) > 1:
        raise NameError_(f"{what} name {normalize(ref)!r} matches {len(hits)} records; use the id",
                         [(h, normalize(ref)) for h in hits], ambiguous=True)
    near = suggest(conn, kind, ref, band=band)
    if not write and len(near) == 1:
        return Resolved(id=near[0][0], name=near[0][1], via="fuzzy", candidates=near)
    if write:
        msg = f"no {what} is named {normalize(ref)!r}"
        if near:
            msg += "; a write needs the exact name or id (did you mean " + \
                   ", ".join(repr(n) for _, n in near) + "?)"
        raise NameError_(msg, near)
    if near:
        raise NameError_(f"no {what} is named {normalize(ref)!r}; several are close, pick one",
                         near, ambiguous=True)
    raise NameError_(f"no {what} is named {normalize(ref)!r}", [])


# -- display ------------------------------------------------------------------

def with_name(row: dict, name: str | None, *, key: str = "name") -> dict:
    """``row`` with the speakable name added (beside its id) when known."""
    if name and isinstance(row, dict):
        row[key] = name
    return row


def label(name: str | None, rid: str) -> str:
    """``'brave-otter-lamp (3f2a9c…)'`` for messages; the id alone if unnamed."""
    return f"{name} ({rid})" if name else str(rid)
