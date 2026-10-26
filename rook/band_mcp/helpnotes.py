"""Just-in-time help: a one-line ``_help`` note on a tool reply, the first
time in an MCP session that the note's moment comes.

A moment is a tool (``rook_knowledge``), a tool and its ``action``
(``rook_task.claim``), or something a reply says (``held``, ``timeout``).
The notes live in the help topics' front matter (:mod:`.help_topics`), so
the skill itself can stay a stub: an agent hears the hygiene for tasks when
it first touches tasks, not before. ``rook_call`` gets reply notes only; its
per-cap advice is the operator's ``cap:`` guidance (``_tips``).

Advice only: a failure here never changes a reply.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
from collections import OrderedDict
from typing import Any, Callable

from . import envelope, help_topics

log = logging.getLogger("rook.band_mcp.helpnotes")

MAX_SEEN = 10000


def _tool_moments(tool: str, kwargs: dict) -> list[str]:
    if tool == "rook_call":
        return []
    action = kwargs.get("action")
    return ([f"{tool}.{action}"] if isinstance(action, str) and action else []) + [tool]


def _reply_moments(reply: dict) -> list[str]:
    if reply.get("held") is True:
        return ["held"]
    if reply.get("ok") is False and "no reply" in str(reply.get("error") or ""):
        return ["timeout"]
    return []


class HelpNotes:
    def __init__(self, library: Callable[[], help_topics.Library] = help_topics.library) -> None:
        self._library = library
        self._seen: OrderedDict = OrderedDict()

    def annotate(self, session: str, tool: str, kwargs: dict, out: Any) -> Any:
        """``out`` with ``_help`` added when a note is due, else ``out`` as is.
        A reply is parsed only when a note could be due, so big replies pay nothing."""
        if not isinstance(out, str) or not out.lstrip().startswith("{"):
            return out
        lib = self._library()
        due = [k for k in _tool_moments(tool, kwargs)
               if (session, k) not in self._seen and lib.note([k])]
        if not due and '"held"' not in out and "no reply" not in out:
            return out
        try:
            reply = json.loads(out)
        except ValueError:
            return out
        if not isinstance(reply, dict) or "_help" in reply:
            return out
        keys = [k for k in _reply_moments(reply) if (session, k) not in self._seen] + due
        found = lib.note(keys) if keys else None
        if found is None:
            return out
        key, topic, text = found
        self._seen[(session, key)] = None
        while len(self._seen) > MAX_SEEN:
            self._seen.popitem(last=False)
        reply["_help"] = (f"{text} More: rook_call(worker=\"rook\", cap=\"help\", "
                          f"args={{\"topic\": \"{topic.name}\"}})")
        return envelope.dumps(reply)


def install(mcp: Any, session: Callable[[], str], notes: HelpNotes | None = None) -> HelpNotes:
    """Wrap every registered tool so its replies can carry ``_help``."""
    notes = notes or HelpNotes()
    for name, tool in list(mcp._tool_manager._tools.items()):
        fn = tool.fn
        if getattr(fn, "_rook_helpnotes", False):
            continue

        def _note(out, kwargs, _name=name):
            try:
                return notes.annotate(session() or "", _name, kwargs, out)
            except Exception:
                log.exception("help note for %s failed", _name)
                return out

        if inspect.iscoroutinefunction(fn) or tool.is_async:
            @functools.wraps(fn)
            async def wrapped(*args, _fn=fn, _n=_note, **kwargs):
                return _n(await _fn(*args, **kwargs), kwargs)
        else:
            @functools.wraps(fn)
            def wrapped(*args, _fn=fn, _n=_note, **kwargs):
                return _n(_fn(*args, **kwargs), kwargs)
        wrapped._rook_helpnotes = True
        tool.fn = wrapped
    return notes
