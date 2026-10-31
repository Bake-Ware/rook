"""The hub's appliance registry: ``<data>/appliances.json`` (spec 5.7).

Bake (castling-workers-appliances): "The agent only targets a name ... the
hub looks up the appliance's profile, which records its proxy (delegate)
route"; "Onboarding sets the route"; "After: use the known-good worker; if
offline, try other workers with the same reach and remember what worked."

One record per instance::

    {"name": "living-room-tv", "plugin": "tv", "delegates": ["<worker id>", ...],
     "host": "192.168.1.40", "port": 3001, "settings": {"client_key": "appliance.living-room-tv.client_key"},
     "added": 1793300000.0, "added_by": "person:bake"}

``delegates[0]`` is the known-good route. Secret settings hold vault names;
values are only read from the vault for one call (dispatch's carry step).
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")


class Appliances:
    def __init__(self, data: str | Path):
        self.path = Path(data) / "appliances.json"
        self._lock = threading.Lock()
        self.items: dict[str, dict] = {}
        try:
            got = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            got = []
        for rec in got if isinstance(got, list) else []:
            if _valid(rec):
                self.items[rec["name"]] = rec

    def get(self, name: str) -> dict | None:
        rec = self.items.get(name) if isinstance(name, str) else None
        return copy.deepcopy(rec) if rec else None

    def list(self) -> list[dict]:
        return [copy.deepcopy(self.items[n]) for n in sorted(self.items)]

    def put(self, rec: dict) -> None:
        if not _valid(rec):
            raise ValueError("not an appliance record")
        with self._lock:
            self.items[rec["name"]] = copy.deepcopy(rec)
            self._save()

    def remove(self, name: str) -> bool:
        with self._lock:
            if self.items.pop(name, None) is None:
                return False
            self._save()
            return True

    def worked(self, name: str, worker: str) -> None:
        """``worker`` carried a call for ``name``: it becomes the known-good route."""
        with self._lock:
            rec = self.items.get(name)
            if rec is None or worker not in rec["delegates"] or rec["delegates"][0] == worker:
                return
            rec["delegates"].remove(worker)
            rec["delegates"].insert(0, worker)
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps([self.items[n] for n in sorted(self.items)], indent=1))
        os.replace(tmp, self.path)


def _valid(rec) -> bool:
    return (isinstance(rec, dict) and isinstance(rec.get("name"), str) and NAME.fullmatch(rec["name"])
            and isinstance(rec.get("plugin"), str) and rec["plugin"]
            and isinstance(rec.get("delegates"), list) and rec["delegates"]
            and all(isinstance(d, str) and d for d in rec["delegates"])
            and isinstance(rec.get("settings", {}), dict))


def secret_decl(decl) -> bool:
    """A setting declared secret (``secret: true`` or ``type: secret``)."""
    return isinstance(decl, dict) and (decl.get("secret") is True or decl.get("type") == "secret")
