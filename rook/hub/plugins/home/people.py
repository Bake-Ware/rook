"""People the house voice knows by voice (docs/design/persistent-agents.md 3.4).

One record per person enrolled through the voice ("learn Robin's voice"): who
they are to the owner, their consent (their own words, as heard), or for a kid
the owner's say-so (his words), when the voiceprint was made and where it lives
(the voice server: voiceprints never leave it), and devices that are obviously
theirs. The voiceprint is identification, never authentication.

A JSON file in the home plugin's data dir, written atomically.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

NAME = re.compile(r"^[A-Za-z][A-Za-z '.-]{0,39}$")
RESERVED = {"guest", "assistant", "unmapped", "unknown", "owner", "voice"}
RELATIONS = ("household", "kid", "guest", "family", "friend")
#: An adult's consent, as heard (the transcript, not the model's account of it).
CONSENT = re.compile(r"\bi\s+(?:agree|consent)\b.{0,60}\bvoice\b", re.I | re.S)
#: The owner's say-so for a kid ("yes, enroll Sam", "go ahead and learn Sam's voice").
SAY_SO = re.compile(r"\b(yes|yeah|yep|go ahead|do it|okay|ok|sure|enrol+|learn|add|set up)\b", re.I)


class PeopleError(ValueError):
    pass


def check_name(name: Any) -> str:
    name = " ".join(str(name or "").split())
    if not NAME.match(name) or name.lower() in RESERVED:
        raise PeopleError("name: a person's first name (letters, up to 40)")
    return name[:1].upper() + name[1:]


def relation_of(text: Any) -> tuple[str, bool]:
    """(relation word, is a kid) from what was said ("my wife", "my son", "a kid")."""
    t = " ".join(str(text or "").lower().split())
    kid = bool(re.search(r"\b(kid|child|son|daughter|boy|girl|teen|baby|toddler)\b", t))
    if kid:
        return "kid", True
    if re.search(r"\b(wife|husband|partner|spouse|mom|mother|dad|father|household|lives here|"
                 r"roommate)\b", t):
        return "household", False
    if re.search(r"\b(family|brother|sister|aunt|uncle|grand\w*|cousin|in-law)\b", t):
        return "family", False
    if re.search(r"\bfriend\b", t):
        return "friend", False
    return ("guest" if not t else t[:40]), False


class People:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
            self._data.setdefault("people", {})
        return self._data

    def _save(self) -> None:
        from ....core.db import atomic_write_json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(self.path), self._load())

    def list(self) -> list[dict]:
        return sorted((dict(p) for p in self._load()["people"].values()),
                      key=lambda p: p.get("name", ""))

    def get(self, name: str) -> dict | None:
        rec = self._load()["people"].get(str(name or "").strip().lower())
        return dict(rec) if rec else None

    def put(self, rec: dict) -> dict:
        key = rec["name"].lower()
        old = self._load()["people"].get(key) or {}
        merged = {**old, **rec, "updated": time.time()}
        merged.setdefault("created", time.time())
        self._load()["people"][key] = merged
        self._save()
        return dict(merged)

    def update(self, name: str, **fields) -> dict | None:
        key = str(name or "").strip().lower()
        rec = self._load()["people"].get(key)
        if rec is None:
            return None
        rec.update(fields, updated=time.time())
        self._save()
        return dict(rec)

    def remove(self, name: str) -> bool:
        key = str(name or "").strip().lower()
        gone = self._load()["people"].pop(key, None) is not None
        if gone:
            self._save()
        return gone
