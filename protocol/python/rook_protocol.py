"""Rook protocol v1: encode, decode and validate messages.

The messages are specified in docs/protocol.md (W1). This module holds no hub
or worker logic: it turns a message (a dict) into bytes and back, and checks
that a message has the fields its type requires.

It also holds what both sides of the link exchange compute alike
(docs/link-design.md, section 2): the signed texts of the knock's link key,
`link_open` and `link_accept`, and `session_psk`, the key a connection's link
runs under.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re

from nacl.bindings import crypto_scalarmult
from nacl.exceptions import BadSignatureError
from nacl.exceptions import RuntimeError as NaclRuntimeError
from nacl.signing import VerifyKey

PROTOCOL = 1
MAX_MESSAGE = 16_000_000    # the largest encoded message, either direction (docs/protocol.md, "Sizes")


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


def _link_key(value, path):
    _string(value, path)
    if not _KEY.match(value):
        raise ProtocolError(f"{path} must be 64 lowercase hex characters (an X25519 public key)")


_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z")


def _signature(value, path):
    _string(value, path)
    if not _SIGNATURE.match(value):
        raise ProtocolError(f"{path} must be 128 lowercase hex characters (an Ed25519 signature)")


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
        "link_key": (_link_key, REQ),             # the worker's X25519 key, signed by `key`
        "link_signature": (_signature, REQ),
        "token": (_string, OPT),
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
        "plugins": (_list_of(_struct({                 # PL3: the plugin worker parts it has, or failed to load
            "name": (_string, REQ),
            "version": (_string, REQ),
            "error": (_string, OPT),
        })), OPT),
        "max_message": (_positive, OPT),          # D4: the largest message it can receive
        "open_streams": (_list_of(_string), OPT),  # D5: the streams it opened and still has
    },
    # Hub -> worker (H2).
    "heartbeat_reply": {
        "protocol": (_positive, REQ),
        "interval": (_positive, REQ),
    },
    # Worker -> hub, the body of POST /link: make this connection's key (docs/link-design.md 2).
    "link_open": {
        "protocol": (_positive, REQ),
        "key": (_key, REQ),
        "ephemeral": (_link_key, REQ),
        "signature": (_signature, REQ),
    },
    # Hub -> worker, the HTTPS response to link_open.
    "link_accept": {
        "protocol": (_positive, REQ),
        "ephemeral": (_link_key, REQ),
        "hub_link_key": (_link_key, REQ),
        "signature": (_signature, REQ),
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
    # Hub -> worker, optional: the size of the terminal showing a stream, in characters (Bake 13 Nov).
    "stream_resize": {
        "stream": (_string, REQ),
        "cols": (_positive, REQ),
        "rows": (_positive, REQ),
    },
    # Hub -> worker, optional: every plugin worker part this worker should have now (PL3).
    "plugins": {
        "plugins": (_list_of(_struct({
            "name": (_string, REQ),
            "version": (_string, REQ),
            "source": (_string, REQ),
        })), REQ),
    },
    # Worker -> hub, optional: puts an event on the hub's bus (J2).
    "event": {
        "name": (_string, REQ),
        "payload": (_object, REQ),
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


# The link exchange (docs/link-design.md, section 2). Public keys and signatures are
# lowercase hex, as in the messages; signing keys are nacl SigningKey objects and X25519
# secrets nacl PrivateKey objects. A failed check names the check, never the bytes.

LINK_KEY_TEXT = b"rook-link-key-v1:"
LINK_OPEN_TEXT = b"rook-link-open-v1:"
LINK_ACCEPT_TEXT = b"rook-link-accept-v1:"
LINK_SALT = b"rook-link-v1"


def _signed(text, *keys):
    return text + b"".join(bytes.fromhex(k) for k in keys)


def _verifies(key, signed, signature):
    try:
        VerifyKey(bytes.fromhex(key)).verify(signed, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


def sign_link_key(signing_key, link_key):
    """knock.link_signature: the identity key vouches for the worker's X25519 link key."""
    return signing_key.sign(_signed(LINK_KEY_TEXT, link_key)).signature.hex()


def verify_link_key(knock):
    """True if knock.link_signature verifies under knock.key."""
    return _verifies(knock["key"], _signed(LINK_KEY_TEXT, knock["link_key"]), knock["link_signature"])


def sign_link_open(signing_key, hub_key, ephemeral):
    """link_open.signature, by the worker's identity key, for one hub and one ephemeral."""
    key = signing_key.verify_key.encode().hex()
    return signing_key.sign(_signed(LINK_OPEN_TEXT, hub_key, key, ephemeral)).signature.hex()


def verify_link_open(hub_key, link_open):
    """True if link_open.signature verifies under link_open.key, for this hub's key."""
    signed = _signed(LINK_OPEN_TEXT, hub_key, link_open["key"], link_open["ephemeral"])
    return _verifies(link_open["key"], signed, link_open["signature"])


def sign_link_accept(hub_signing_key, key, worker_ephemeral, ephemeral, hub_link_key):
    """link_accept.signature, by the hub key, answering one worker's one ephemeral."""
    signed = _signed(LINK_ACCEPT_TEXT, key, worker_ephemeral, ephemeral, hub_link_key)
    return hub_signing_key.sign(signed).signature.hex()


def verify_link_accept(hub_key, key, worker_ephemeral, link_accept):
    """True if link_accept.signature verifies under the hub key the worker stored, and
    answers this worker's key and the ephemeral it just sent."""
    signed = _signed(LINK_ACCEPT_TEXT, key, worker_ephemeral, link_accept["ephemeral"],
                     link_accept["hub_link_key"])
    return _verifies(hub_key, signed, link_accept["signature"])


def hkdf_sha256(salt, ikm, info, length):
    """HKDF with SHA-256 (RFC 5869): extract, then expand to `length` bytes."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def _x25519(secret, public, name):
    try:
        shared = crypto_scalarmult(secret.encode(), bytes.fromhex(public))
    except NaclRuntimeError:          # libsodium refuses an all-zero result
        shared = bytes(32)
    if shared == bytes(32):
        raise ProtocolError(f"{name} is all zero: the other side's key is a small-order point")
    return shared


def session_psk(side, key, worker_ephemeral, hub_ephemeral, ephemeral_secret, link_secret, peer_link_key):
    """The PSK one connection's link runs under (docs/link-design.md 2.4), as 64 lowercase
    hex characters. `side` is "worker" or "hub"; `ephemeral_secret` and `link_secret` are that
    side's, `peer_link_key` the other side's link key. `key` is the worker's identity key.
    Both sides get the same PSK. Raises ProtocolError if either X25519 result is all zero."""
    if side not in ("worker", "hub"):
        raise ValueError("side must be worker or hub")
    peer_ephemeral = hub_ephemeral if side == "worker" else worker_ephemeral
    ee = _x25519(ephemeral_secret, peer_ephemeral, "ee")
    ss = _x25519(link_secret, peer_link_key, "ss")
    info = bytes.fromhex(key) + bytes.fromhex(worker_ephemeral) + bytes.fromhex(hub_ephemeral)
    return hkdf_sha256(LINK_SALT, ee + ss, info, 32).hex()
