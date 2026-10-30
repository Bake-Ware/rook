"""Stream events, stream specs and stream metadata (castling-streams).

A stream is an append-only log of typed events. Five event types:

* ``bytes``: raw output (a PTY, a process). The body is ``bytes``.
* ``text``: a chat message. Body ``{"text": str, ...}``.
* ``turn``: one step of an agent session. Body ``{"role": prompt|reply|tool, ...}``.
* ``status``: lifecycle and state (started, exited, closed, a huddle status).
  Body: any JSON object.
* ``input``: something sent *to* the producer: keystrokes or a message
  (``{"text": str}``), a signal (``{"signal": "INT"}``) or a terminal size
  (``{"resize": [cols, rows]}``). Exactly one of the three.

Every body other than ``bytes`` is a JSON object. Stdlib only.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

EVENT_TYPES = ("bytes", "text", "turn", "status", "input")
TURN_ROLES = ("prompt", "reply", "tool")
SIGNALS = ("INT", "TERM", "HUP", "QUIT", "KILL")
MAX_EVENT = 1 << 20           # one event body, bytes; larger output is split by the producer
_KIND = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class StreamError(ValueError):
    """A refused stream operation (unknown stream, wrong type, closed, ...)."""


@dataclass(frozen=True)
class StreamSpec:
    """What a stream carries: the cap manifest's ``stream: {types, input, durable}``."""
    types: tuple[str, ...]
    input: bool = False
    durable: bool = False

    def __post_init__(self) -> None:
        types = tuple(dict.fromkeys(self.types))      # dedupe, keep order
        bad = [t for t in types if t not in EVENT_TYPES]
        if bad:
            raise StreamError(f"unknown event type(s): {', '.join(bad)}")
        # ``input`` in the type list means the same as ``input: true``.
        object.__setattr__(self, "input", bool(self.input) or "input" in types)
        object.__setattr__(self, "types", tuple(t for t in types if t != "input"))
        if not self.types:
            raise StreamError("a stream needs at least one output type")
        object.__setattr__(self, "durable", bool(self.durable))

    @classmethod
    def from_manifest(cls, block: dict) -> "StreamSpec":
        """From a cap's ``stream:`` block in ``plugin.yaml``."""
        if not isinstance(block, dict):
            raise StreamError("stream block must be a mapping")
        return cls(tuple(block.get("types") or ()), block.get("input", False),
                   block.get("durable", False))

    def allows(self, type_: str) -> bool:
        """``status`` is always allowed: open, exit and close are written by core."""
        return type_ in self.types or type_ == "status" or (type_ == "input" and self.input)


# The decided defaults per kind (castling-streams, "Durability"): rooms,
# huddles and agent sessions keep everything; terminals and processes keep a
# bounded buffer. voice and inbox follow sessions and rooms. Any other kind is
# allowed but must say what it carries.
KINDS: dict[str, StreamSpec] = {
    "terminal": StreamSpec(("bytes",), input=True, durable=False),
    "process": StreamSpec(("bytes",), input=True, durable=False),
    "room": StreamSpec(("text",), durable=True),
    "huddle": StreamSpec(("text",), durable=True),
    "session": StreamSpec(("turn", "text"), input=True, durable=True),
    "voice": StreamSpec(("turn",), input=True, durable=True),
    "inbox": StreamSpec(("text",), durable=True),
}


def check_kind(kind: str) -> str:
    if not isinstance(kind, str) or not _KIND.match(kind):
        raise StreamError("kind must match [a-z][a-z0-9-]*, up to 32 chars")
    return kind


def check_body(type_: str, body) -> bytes | dict:
    """Validate an event body for its type; returns the body to store."""
    if type_ == "bytes":
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise StreamError("a bytes event needs a bytes body")
        body = bytes(body)
        size = len(body)
    else:
        if not isinstance(body, dict):
            raise StreamError(f"a {type_} event needs an object body")
        try:
            size = len(json.dumps(body, separators=(",", ":")))
        except (TypeError, ValueError) as e:
            raise StreamError(f"event body is not JSON: {e}") from None
        if type_ == "text" and not isinstance(body.get("text"), str):
            raise StreamError("a text event needs body.text")
        if type_ == "turn" and body.get("role") not in TURN_ROLES:
            raise StreamError(f"a turn event needs body.role in {', '.join(TURN_ROLES)}")
        if type_ == "input":
            _check_input(body)
    if size > MAX_EVENT:
        raise StreamError(f"event larger than {MAX_EVENT} bytes; split it")
    return body


def _check_input(body: dict) -> None:
    keys = [k for k in ("text", "signal", "resize") if k in body]
    if len(keys) != 1:
        raise StreamError("an input event has exactly one of text, signal, resize")
    if keys[0] == "text" and not isinstance(body["text"], str):
        raise StreamError("input text must be a string")
    if keys[0] == "signal" and body["signal"] not in SIGNALS:
        raise StreamError(f"signal must be one of {', '.join(SIGNALS)}")
    if keys[0] == "resize":
        r = body["resize"]
        if not (isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) and x > 0 for x in r)):
            raise StreamError("resize is [cols, rows]")


@dataclass(frozen=True)
class Event:
    seq: int                  # 1, 2, 3 ... per stream; a reader's cursor is the last seq it saw
    at: float
    type: str
    by: str                   # who wrote it: a person, an agent, a producer
    body: bytes | dict

    @property
    def size(self) -> int:
        """Rough bytes held, for bounded buffers (body plus fixed overhead)."""
        n = len(self.body) if isinstance(self.body, bytes) else len(json.dumps(self.body))
        return n + 64

    def plain(self) -> str:
        """Searchable text: output bytes decoded, else the body's text fields."""
        if isinstance(self.body, bytes):
            return self.body.decode("utf-8", "replace")
        return " ".join(str(self.body[k]) for k in ("text", "name", "state") if k in self.body)

    def wire(self) -> dict:
        """JSON-able form for caps and the relay; bytes travel as base64."""
        out = {"seq": self.seq, "at": self.at, "type": self.type, "by": self.by}
        if isinstance(self.body, bytes):
            out["b64"] = base64.b64encode(self.body).decode("ascii")
        else:
            out["data"] = self.body
        return out


@dataclass
class Meta:
    """A stream's metadata: what the hub index lists."""
    id: str
    kind: str
    spec: StreamSpec
    title: str = ""
    host: str = ""                    # who owns the producer/log: a worker name, or "hub"
    producer: str = ""                # what writes output: pty, process, claude, codex, voice, ...
    participants: list[str] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)       # kind-specific: cwd, pid, exit_code, ...
    created: float = 0.0
    closed: float | None = None
    last_seq: int = 0
    input_seq: int = 0                # last input event the producer has taken

    def wire(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "host": self.host,
                "producer": self.producer, "participants": list(self.participants),
                "types": list(self.spec.types), "input": self.spec.input,
                "durable": self.spec.durable, "attrs": dict(self.attrs),
                "created": self.created, "closed": self.closed, "last_seq": self.last_seq}
