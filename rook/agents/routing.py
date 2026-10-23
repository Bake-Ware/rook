"""Routing lines in the voice lane's replies (docs/design/persistent-agents.md 3.4).

The voice session has no tools: tools make Haiku 5.5 think before it speaks.
It speaks first and, when the request needs the agent, ends its reply with a
routing line::

    Let me check.
    <<flow {"op": "queue", "request": "what's on my calendar tomorrow"}>>

The session host strips every routing line from what it streams to the voice
client (so it is never spoken or shown) and from the reply text, and hands the
parsed ops to the hub, which runs them with the flow-tool code.

:class:`RoutingFilter` works on the stream: text that might be the start of a
marker (``<``, ``<<fl``, a backtick) is held back until it is clear, so no
part of a routing line ever leaves, however the deltas are split.
"""

from __future__ import annotations

import json
import re
from typing import Any

OPEN = "<<flow"
CLOSE = ">>"
#: Most routing lines taken from one reply; more are dropped.
MAX_OPS = 3
#: An unterminated routing line longer than this is given up on (dropped).
MAX_LINE = 4000
#: Characters held back at the end of the stream while it is unclear whether
#: they start a marker (a backtick may wrap one: ``<<flow …>>``).
_HOLD = "<`"


def _parse(body: str) -> dict | None:
    """The op of a routing line's body (the text between ``<<flow`` and ``>>``)."""
    body = body.strip().strip("`").strip()
    if not body.startswith("{"):
        return None
    try:
        op = json.loads(body)
    except ValueError:
        return None
    if not isinstance(op, dict) or not isinstance(op.get("op"), str) or not op["op"].strip():
        return None
    return op


class RoutingFilter:
    """Feed it deltas; it returns the text safe to pass on and collects the ops."""

    def __init__(self) -> None:
        self.held = ""              # passable text not yet passed on (maybe a marker's start)
        self.line: str | None = None  # inside a routing line: its text so far
        self._scan = 0              # where to look for ">>" in self.line next
        self.ops: list[dict] = []
        self.bad = 0                # routing lines that did not parse
        self.dropped = 0            # parsed ops over MAX_OPS

    def feed(self, text: str) -> str:
        out: list[str] = []
        buf = self.held + str(text or "")
        self.held = ""
        while buf:
            if self.line is not None:
                buf = self._in_line(buf)
                continue
            i = buf.find("<")
            if i < 0:
                keep = len(buf) - len(buf.rstrip("`"))
                out.append(buf[:len(buf) - keep])
                self.held = buf[len(buf) - keep:]
                break
            rest = buf[i:]
            if rest.startswith(OPEN):
                # Drop the backticks and spaces right before the marker.
                pre = buf[:i].rstrip("` ")
                out.append(pre)
                self.line, self._scan = "", 0
                buf = rest[len(OPEN):]
                continue
            if OPEN.startswith(rest):
                # "<", "<<", "<<fl" at the end: wait for more.
                pre = buf[:i]
                keep = len(pre) - len(pre.rstrip("`"))
                out.append(pre[:len(pre) - keep])
                self.held = pre[len(pre) - keep:] + rest
                break
            out.append(buf[:i + 1])
            buf = buf[i + 1:]
        return "".join(out)

    def _in_line(self, buf: str) -> str:
        """Inside a routing line: consume up to its ``>>`` (the first one that ends
        valid JSON); returns what is left after it."""
        assert self.line is not None
        whole = self.line + buf
        start = max(0, self._scan)
        while True:
            j = whole.find(CLOSE, start)
            if j < 0:
                self.line = whole
                # A ">" at the very end may be the first half of ">>".
                self._scan = max(0, len(whole) - 1)
                if len(whole) > MAX_LINE:
                    self._end(None)
                return ""
            candidate = whole[:j]
            op = _parse(candidate)
            if op is not None or not candidate.strip().startswith("{") or \
                    self._balanced(candidate):
                self._end(op)
                return whole[j + len(CLOSE):].lstrip("`")
            start = j + 1          # ">>" inside a JSON string: keep looking

    @staticmethod
    def _balanced(text: str) -> bool:
        """Whether braces outside strings close: a JSON object that ended but
        does not parse is a bad line, not a ``>>`` inside a string."""
        depth, in_str, esc = 0, False, False
        for ch in text.strip():
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return True
        return False

    def _end(self, op: dict | None) -> None:
        self.line = None
        if op is None:
            self.bad += 1
        elif len(self.ops) < MAX_OPS:
            self.ops.append(op)
        else:
            self.dropped += 1

    def finish(self) -> str:
        """End of the reply: an unterminated routing line counts if its JSON is
        whole; held text that could only be a marker's start is dropped."""
        if self.line is not None:
            self._end(_parse(self.line))
        held, self.held = self.held, ""
        if OPEN.startswith(held.lstrip("`")) and held.lstrip("`").startswith("<<"):
            return ""
        return held.rstrip("`") if held.strip("`") == "" else held


def strip(text: str) -> tuple[str, list[dict]]:
    """A whole reply: its text without routing lines, and the ops."""
    f = RoutingFilter()
    out = f.feed(text) + f.finish()
    return clean(out), f.ops


def clean(text: str) -> str:
    """Tidy the spoken text once routing lines are gone (blank lines, trailing space)."""
    text = re.sub(r"[ \t]+\n", "\n", str(text or ""))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def describe(op: Any) -> str:
    """A short label for logs and events (no free text)."""
    return str(op.get("op") if isinstance(op, dict) else "?")[:20]
