"""``serves.*``: what each worker hosts, as hand-written metadata on the hub.

A worker's ``description`` says what a machine is; ``serves`` says what it
hosts: the public **sites** it answers for and the **services** (APIs,
databases, model servers) that run on it. Both are lists of ``{name, url}``
written by a person or an agent (``serves.set``); nothing here probes the
network. The lists show up as the ``serves`` field of ``rook_workers`` rows,
keyed by worker name, so they survive worker restarts and need no worker
update.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from ...core.plugin import Plugin, capability, place

KINDS = ("sites", "services")
MAX_ITEMS = 60
MAX_TEXT = 200


def _items(value: Any, kind: str) -> list[dict]:
    """Normalise a list of ``"url"`` / ``{name, url}`` into ``[{name, url}]``."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{kind} must be a list of {{name, url}} items or strings")
    if len(value) > MAX_ITEMS:
        raise ValueError(f"{kind}: at most {MAX_ITEMS} items")
    out, seen = [], set()
    for item in value:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            raise ValueError(f"{kind} items are {{name, url}} or a string")
        url = str(item.get("url") or "").strip()[:MAX_TEXT]
        name = str(item.get("name") or "").strip()[:MAX_TEXT]
        if not url and not name:
            raise ValueError(f"{kind} items need a name or a url")
        if not name:  # "https://watch.example.com/x" -> "watch.example.com"
            name = url.split("://", 1)[-1].split("/", 1)[0]
        key = (name.lower(), url.lower())
        if key in seen:
            continue
        seen.add(key)
        entry = {"name": name}
        if url:
            entry["url"] = url
        note = str(item.get("note") or "").strip()[:MAX_TEXT]
        if note:
            entry["note"] = note
        out.append(entry)
    return out


class Serves(Plugin):
    NAMESPACE = "serves"
    NAME = "serves"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SKILL = ("### serves\n"
             "What each worker hosts: `serves` on a `rook_workers` row is "
             "`{sites: [{name, url}], services: [{name, url}]}`, written by hand. "
             "`rook_call(worker=\"rook\", cap=\"serves.list\")` returns all of it; "
             "`serves.set(worker, sites?, services?)` replaces the lists it is given; "
             "`serves.clear(worker)` removes the entry.\n")

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._data: dict[str, dict] | None = None

    # -- store ---------------------------------------------------------------
    def _path(self):
        return self.data_dir / "serves.json"

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            try:
                self._data = json.loads(self._path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def _save(self) -> None:
        path = self._path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _key(worker: str) -> str:
        key = str(worker or "").strip().lower()
        if not key:
            raise ValueError("worker is required (its name)")
        return key

    def lookup(self, name: str | None) -> dict | None:
        """The ``serves`` field for a worker's roster row (None when unset)."""
        if not name:
            return None
        with self._lock:
            entry = self._load().get(str(name).strip().lower())
        if not entry:
            return None
        return {k: entry[k] for k in KINDS if entry.get(k)} or None

    # -- caps ----------------------------------------------------------------
    @capability("list", risk="read")
    def list(self, worker: str = "") -> dict:
        """What workers host: ``{worker: {sites, services, updated, by}}``.

        ``worker`` (a name) narrows to one."""
        with self._lock:
            data = dict(self._load())
        if worker:
            key = self._key(worker)
            data = {key: data[key]} if key in data else {}
        return {"serves": data}

    @capability("set", risk="write")
    def set(self, worker: str, sites: list | None = None, services: list | None = None,
            by: str = "") -> dict:
        """Write what a worker hosts. ``sites`` and ``services`` are lists of
        ``{name, url, note?}`` (or plain URL strings); a list that is given
        replaces the stored one, one that is omitted is kept. ``by`` says who
        or what wrote it (for example ``cloudflare tunnel sync``)."""
        key = self._key(worker)
        given = {"sites": sites, "services": services}
        if all(v is None for v in given.values()):
            raise ValueError("give sites, services or both")
        with self._lock:
            data = self._load()
            entry = dict(data.get(key) or {})
            for kind, value in given.items():
                if value is not None:
                    entry[kind] = _items(value, kind)
            entry["updated"] = round(time.time(), 3)
            entry["by"] = str(by or "").strip()[:MAX_TEXT]
            data[key] = entry
            self._save()
        return {"worker": key, **entry}

    @capability("clear", risk="write")
    def clear(self, worker: str) -> dict:
        """Remove a worker's hosting entry."""
        key = self._key(worker)
        with self._lock:
            data = self._load()
            removed = data.pop(key, None) is not None
            if removed:
                self._save()
        return {"worker": key, "removed": removed}


PLUGIN = Serves
