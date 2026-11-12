"""Rook protocol v1: encode, decode and validate messages.

The messages are specified in docs/protocol.md (W1). This module holds no hub
or worker logic: it turns a message (a dict) into bytes and back, and checks
that a message has the fields its type requires.
"""

import base64
import binascii
import json
import re

PROTOCOL = 1


class ProtocolError(ValueError):
    """A message that is not valid Rook protocol v1."""


# Field checkers. Each takes a value and a path (for the error text) and
# raises ProtocolError if the value does not fit.

def _string(value, path):
    if not isinstance(value, str):
        raise ProtocolError(f"{path} must be a string")


def _integer(value, path):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{path} must be an integer")


def _number(value, path):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{path} must be a number")


def _object(value, path):
    if not isinstance(value, dict):
        raise ProtocolError(f"{path} must be an object")


def _any(value, path):
    pass


def _positive(value, path):
    _integer(value, path)
    if value < 1:
        raise ProtocolError(f"{path} must be 1 or more")


_KEY = re.compile(r"[0-9a-f]{64}\Z")


def _key(value, path):
    _string(value, path)
    if not _KEY.match(value):
        raise ProtocolError(f"{path} must be 64 lowercase hex characters (an Ed25519 public key)")


# W3: dot-separated segments; a hyphen only inside a segment.
_CAP_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*(\.[a-z0-9]+(-[a-z0-9]+)*)*\Z")


def _cap_name(value, path):
    _string(value, path)
    if not _CAP_NAME.match(value):
        raise ProtocolError(
            f"{path} {value!r} is not a cap name: use dot-separated lowercase segments "
            "like shell.exec, with hyphens only inside a segment")


def _base64(value, path):
    _string(value, path)
    try:
        base64.b64decode(value, validate=True)
    except binascii.Error:
        raise ProtocolError(f"{path} must be base64") from None


def _one_of(*choices):
    def check(value, path):
        if value not in choices:
            raise ProtocolError(f"{path} must be one of {', '.join(choices)}")
    return check


def _nullable(check):
    def nullable(value, path):
        if value is not None:
            check(value, path)
    return nullable


def _list_of(check):
    def checked_list(value, path):
        if not isinstance(value, list):
            raise ProtocolError(f"{path} must be a list")
        for i, item in enumerate(value):
            check(item, f"{path}[{i}]")
    return checked_list


def _struct(fields):
    """An object whose listed fields are checked. Unknown fields are allowed (U8)."""
    def checked_struct(value, path):
        _object(value, path)
        for name, (check, required) in fields.items():
            if name in value:
                check(value[name], f"{path}.{name}")
            elif required:
                raise ProtocolError(f"{path}.{name} is missing")
    return checked_struct


REQ, OPT = True, False

CAP = _struct({                                   # W3, S1
    "name": (_cap_name, REQ),
    "description": (_string, REQ),
    "schema": (_object, REQ),
    "returns": (_one_of("result", "stream"), REQ),
})

_SIZE = _struct({"total": (_integer, REQ), "free": (_integer, REQ)})

HEALTH = _struct({                                # H4: every field optional
    "cpu": (_number, OPT),
    "ram": (_SIZE, OPT),
    "disk": (_SIZE, OPT),
    "gpu": (_number, OPT),
    "vram": (_SIZE, OPT),
    "npu": (_number, OPT),
    "battery": (_number, OPT),
    "temperature": (_number, OPT),
    "heap_free": (_integer, OPT),
    "uptime": (_number, OPT),
})

MESSAGES = {
    # Worker -> hub over HTTPS (R2, R4, R5).
    "knock": {
        "protocol": (_positive, REQ),
        "name": (_string, REQ),
        "key": (_key, REQ),
        "macs": (_list_of(_string), REQ),
        "caps": (_list_of(CAP), REQ),
    },
    # Hub -> worker, the HTTPS reply to a knock (R2, R5, W7).
    "knock_reply": {
        "protocol": (_positive, REQ),
        "status": (_one_of("pending", "approved"), REQ),
        "guid": (_string, OPT),
        "moniker": (_string, OPT),
        "signature": (_string, OPT),
        "hub_key": (_key, OPT),
    },
    # Worker -> hub (H1, H4, S4).
    "heartbeat": {
        "protocol": (_positive, REQ),
        "identity": (_struct({"name": (_string, REQ), "key": (_key, REQ)}), REQ),
        "facts": (_struct({
            "os": (_string, REQ),
            "arch": (_string, REQ),
            "hostname": (_string, REQ),
            "form": (_string, REQ),
            "version": (_string, REQ),
            "macs": (_list_of(_string), REQ),
        }), REQ),
        "caps": (_list_of(CAP), REQ),
        "band": (_struct({"gateway_mac": (_nullable(_string), REQ)}), REQ),
        "health": (HEALTH, REQ),
        "streams": (_integer, OPT),
    },
    # Hub -> worker (H2).
    "heartbeat_reply": {
        "protocol": (_positive, REQ),
        "interval": (_positive, REQ),
    },
    # Hub -> worker (C3, W5).
    "call": {
        "id": (_string, REQ),
        "cap": (_cap_name, REQ),
        "args": (_object, REQ),
    },
    # Worker -> hub.
    "result": {
        "id": (_string, REQ),
        "result": (_any, REQ),
    },
    # Either direction; also what the hub hands back to a caller (C3, W5).
    "error": {
        "id": (_string, OPT),
        "code": (_string, REQ),
        "message": (_string, REQ),
        "next": (_string, REQ),
    },
    # Worker -> hub, the reply to a call on a stream cap (S1, S3).
    "stream_open": {
        "id": (_string, REQ),
        "stream": (_string, REQ),
        "title": (_string, REQ),
        "description": (_string, REQ),
    },
    # Either direction (S1).
    "stream_data": {
        "stream": (_string, REQ),
        "data": (_base64, REQ),
    },
    # Either direction (S1).
    "stream_close": {
        "stream": (_string, REQ),
    },
}


def validate(message):
    """Check a message against its type. Returns it unchanged, or raises ProtocolError.

    Fields the type does not list are allowed and kept, so a newer peer's extra
    fields never break an older one (U8)."""
    _object(message, "message")
    kind = message.get("type")
    if kind not in MESSAGES:
        raise ProtocolError(f"unknown message type {kind!r}")
    _struct(MESSAGES[kind])(message, kind)
    if kind == "knock_reply" and message["status"] == "approved":
        for name in ("guid", "moniker", "signature", "hub_key"):
            if name not in message:
                raise ProtocolError(f"knock_reply.{name} is required when status is approved")
    return message


def encode(message):
    """Validate a message and encode it: UTF-8 JSON, keys sorted, no whitespace."""
    validate(message)
    try:
        text = json.dumps(message, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ProtocolError(f"message is not encodable as JSON: {e}") from None
    return text.encode("utf-8")


def _no_constants(name):
    raise ValueError(f"{name} is not valid JSON")


def decode(data):
    """Decode bytes into a message and validate it, or raise ProtocolError."""
    try:
        message = json.loads(data.decode("utf-8"), parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError(f"not UTF-8 JSON: {e}") from None
    return validate(message)
