"""The Rook skill's help topics: ``skills/rook/help/*.md``.

One Markdown file per subject, read by the hub's ``help`` cap (layered:
core model, then a topic, then its reference pages) and by the bridge's
just-in-time notes (:mod:`.helpnotes`). The files ship inside the skill
too, so an agent without a hub can read them directly.

Front matter is a small subset: ``key: value``, ``key: [a, b]`` and
``key:`` followed by ``  - item`` lines. Keys:

* ``title``, ``pitch``: what the topic is, in one line each;
* ``tools``: MCP tools the topic covers (``help topic=rook_task`` finds it);
* ``caps``: cap prefixes it covers (``help topic=shell.exec``);
* ``refs``: skill reference pages served with ``detail=true``;
* ``notes``: ``<key>: <text>`` items, shown once per MCP session the first
  time ``key`` happens: a tool (``rook_task``), a tool and its action
  (``rook_task.claim``), or a reply (``held``, ``timeout``).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import skill

CORE = "core"
_WORD = re.compile(r"[a-z0-9_.]{3,}")


@dataclass(frozen=True)
class Topic:
    name: str
    title: str
    pitch: str
    body: str
    tools: tuple[str, ...] = ()
    caps: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    notes: dict = field(default_factory=dict)

    def brief(self) -> dict:
        return {"topic": self.name, "title": self.title, "pitch": self.pitch}


def _list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [v.strip() for v in value[1:-1].split(",") if v.strip()]
    return [value] if value else []


def parse(name: str, text: str) -> Topic:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n") or (end := text.find("\n---\n", 4)) < 0:
        raise ValueError(f"help/{name}.md: no front matter")
    meta: dict = {}
    key = None
    for line in text[4:end].split("\n"):
        if not line.strip():
            continue
        item = re.match(r"^\s+-\s*(.*)$", line)
        if item and key is not None and isinstance(meta.get(key), list):
            meta[key].append(item.group(1).strip())
            continue
        kv = re.match(r"^([a-z_]+):(.*)$", line)
        if not kv:
            raise ValueError(f"help/{name}.md: bad front matter line {line!r}")
        key, value = kv.group(1), kv.group(2).strip()
        meta[key] = value if key in ("title", "pitch") else _list(value)
    notes = {}
    for item in meta.get("notes", []):
        k, sep, v = item.partition(": ")
        if not sep or not re.fullmatch(r"[a-z_]+(\.[a-z_]+)?", k):
            raise ValueError(f"help/{name}.md: a note must be '<tool[.action]>: <text>', got {item!r}")
        notes[k] = v.strip()
    if not meta.get("title") or not meta.get("pitch"):
        raise ValueError(f"help/{name}.md: title and pitch are required")
    return Topic(name=name, title=meta["title"], pitch=meta["pitch"],
                 body=text[end + 5:].strip() + "\n", tools=tuple(meta.get("tools", ())),
                 caps=tuple(meta.get("caps", ())), refs=tuple(meta.get("refs", ())), notes=notes)


class Library:
    """All help topics, keyed by name; ``core`` is the model every agent needs."""

    def __init__(self, topics: dict[str, Topic], files: dict[str, str]) -> None:
        self.topics = topics
        self.files = files

    @property
    def core(self) -> Topic:
        return self.topics[CORE]

    def menu(self) -> list[dict]:
        return [t.brief() for n, t in self.topics.items() if n != CORE]

    def resolve(self, query: str) -> Topic | None:
        """A topic by name, by a tool it covers, or by the longest cap prefix."""
        q = query.strip().lower().replace(" ", "_") if query else ""
        if q in self.topics:
            return self.topics[q]
        for t in self.topics.values():
            if q in t.tools:
                return t
        best, size = None, 0
        for t in self.topics.values():
            for prefix in t.caps:
                if q.startswith(prefix) and len(prefix) > size:
                    best, size = t, len(prefix)
        return best

    def search(self, query: str, limit: int = 3) -> list[Topic]:
        words = set(_WORD.findall(query.lower()))
        scored = []
        for t in self.topics.values():
            if t.name == CORE:
                continue
            head = f"{t.name} {t.title} {t.pitch} {' '.join(t.tools)}".lower()
            body = t.body.lower()
            score = sum(3 * head.count(w) + body.count(w) for w in words)
            if score:
                scored.append((score, t.name))
        return [self.topics[n] for _, n in sorted(scored, key=lambda s: (-s[0], s[1]))[:limit]]

    def reference(self, topic: Topic) -> dict[str, str]:
        return {r: self.files[r] for r in topic.refs if r in self.files}

    def note(self, keys) -> tuple[str, Topic, str] | None:
        """The first of ``keys`` (most specific first) that some topic has a note for."""
        for k in keys:
            for t in self.topics.values():
                if k in t.notes:
                    return k, t, t.notes[k]
        return None


_cache: dict = {}
RECHECK_S = 5.0


def library() -> Library:
    """The help topics, re-read when a file under help/ changes (checked at
    most every RECHECK_S seconds: every tool reply asks)."""
    now = time.monotonic()
    if "lib" in _cache and now - _cache["checked"] < RECHECK_S:
        return _cache["lib"]
    root = skill.skill_dir() / "help"
    paths = sorted(root.glob("*.md"))
    sig = tuple((p.name, p.stat().st_mtime_ns) for p in paths)
    if _cache.get("sig") != sig:
        topics = {p.stem: parse(p.stem, p.read_text(encoding="utf-8")) for p in paths}
        if CORE not in topics:
            raise FileNotFoundError(f"{root / 'core.md'} is missing")
        _cache.update(sig=sig, lib=Library(topics, skill.base_files()))
    _cache["checked"] = now
    return _cache["lib"]
