"""Notification dedupe (docs/design/jobs.md 4.2): a job sends the same text
to the same target at most once per ``limits.notify_window`` (else the hub
setting ``job.notify_window``; ``0`` turns it off).

It covers every way a run reaches a person: ``notify`` and ``ask`` steps,
alerts, ``cap`` / ``fanout`` steps calling ``voice.speak``, ``notify.post``
or ``notify.send``, and an agent step's ``notify_bake``, ``ask_bake`` and
``rook_call`` / ``rook_tool``. The target is the cap and the worker it ran
on. Texts are compared loosely (case, spacing, punctuation and numbers are
ignored), so "failed 68 times" and "failed 69 times" count as the same
message. Only a digest of the text is stored. A suppressed send is not an
error: the step gets ``{"suppressed": true, ...}`` back (an ``ask`` that is
suppressed fails, since nobody answered).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from .cron import iso, parse_duration

NOTICE_CAPS = ("voice.speak", "notify.post", "notify.send")
DEFAULT_WINDOW = "6h"
_NUM = re.compile(r"\d+")
_JUNK = re.compile(r"[^a-z#]+")


def normalize(text: str) -> str:
    t = _NUM.sub("#", str(text or "").lower())
    return _JUNK.sub(" ", t).strip()


def digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:32]


def text_of(cap: str, args: Any) -> str:
    if cap not in NOTICE_CAPS or not isinstance(args, dict):
        return ""
    return " ".join(str(args.get(k) or "") for k in ("title", "text")).strip()


class Notices:
    """The per-job memory of what was sent where (rows in the jobs store)."""

    def __init__(self, store: Any, settings: Callable[[str], Any], clock: Callable[[], float]) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock

    def window(self, job: dict) -> float:
        lim = job.get("limits") if isinstance(job.get("limits"), dict) else {}
        raw = lim.get("notify_window")
        if raw is None:
            raw = self.settings("notify_window")
            if raw in (None, ""):
                raw = DEFAULT_WINDOW
        try:
            return max(0.0, parse_duration(raw))
        except ValueError:
            return parse_duration(DEFAULT_WINDOW)

    def check(self, job: dict, cap: str, target: str, args: Any) -> tuple[dict | None, Callable[[str], None]]:
        """``(suppressed, record)``: ``suppressed`` is the reply to give
        instead of sending (None: send), ``record(run_id)`` notes a send that
        went out."""
        text = text_of(cap, args)
        jid = str(job.get("id") or "")
        noop = lambda _run_id=None: None  # noqa: E731
        if not text or not jid:
            return None, noop
        window = self.window(job)
        if window <= 0:
            return None, noop
        key, where = digest(text), f"{cap}@{target}"
        now = self.clock()
        at = self.store.notice_at(jid, where, key, now - window)
        if at is not None:
            return ({"suppressed": True, "cap": cap, "target": target, "sent_at": iso(at),
                     "note": "the same message already went to this target within limits.notify_window; "
                             "not sent again"}), noop

        def record(run_id: str | None = None) -> None:
            self.store.record_notice(jid, where, key, self.clock(), run_id)
        return None, record
