"""The kvm caps config can't say: hid.click at a pixel (read the screen size,
move, click: a sequence, coverage A5) and the raw /api/ escape hatches (a
caller-chosen path and query, A12)."""

from __future__ import annotations

import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

READ_MAX = 16 << 20        # bytes of one kvmd answer
READ_SECS = 30             # seconds one answer may keep coming
_HOST = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._~-]+)(:\d{1,5})?")


def click(ctx, button: str = "left", x: int | None = None, y: int | None = None):
    if (x is None) != (y is None):
        raise ctx.Error("give x and y together, or neither")
    if x is not None:
        width, height = _resolution(ctx)
        _request(ctx, "POST", "/api/hid/events/send_mouse_move",
                 {"to_x": _scale(ctx, "x", x, width), "to_y": _scale(ctx, "y", y, height)})
    # No state: kvmd presses and releases.
    _request(ctx, "POST", "/api/hid/events/send_mouse_button", {"button": button})
    return {"button": button, "x": x, "y": y}


def api_get(ctx, path: str, query=None):
    return _answer(ctx, *_request(ctx, "GET", _path(ctx, path), _query(ctx, query), raw=True))


def api_post(ctx, path: str, query=None, body: str | None = None, body_b64: str | None = None,
             body_type: str = "application/json"):
    if body is not None and body_b64 is not None:
        raise ctx.Error("give body or body_b64, not both")
    data = None
    if body is not None:
        data = body.encode("utf-8")
    elif body_b64 is not None:
        try:
            data = base64.b64decode(body_b64, validate=True)
        except ValueError:
            raise ctx.Error("body_b64 is not base64") from None
    return _answer(ctx, *_request(ctx, "POST", _path(ctx, path), _query(ctx, query), data,
                                  body_type if data is not None else None, raw=True))


# -- helpers ----------------------------------------------------------------------

def _appliance(ctx) -> dict:
    # TODO(castling:worker-host): the host doesn't hand run: code the
    # appliance instance yet (spec 5.7: instance settings travel with the
    # call); ctx.appliance stands in: {host, user, password, verify_tls}.
    a = getattr(ctx, "appliance", None) or {}
    if not _HOST.fullmatch(str(a.get("host") or "")):
        raise ctx.Error("kvm: this call has no appliance instance (or its host is not a host name)")
    return a


def _request(ctx, method: str, path: str, query: dict | None = None, data: bytes | None = None,
             ctype: str | None = None, *, raw: bool = False):
    """One kvmd request: (status, content type, body). A non-2xx answer is an
    error unless ``raw`` (the escape hatches pass it back)."""
    a = _appliance(ctx)
    url = f"https://{a['host']}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, data=data, method=method)
    cred = f"{a.get('user') or 'admin'}:{a.get('password') or ''}".encode()
    req.add_header("Authorization", "Basic " + base64.b64encode(cred).decode())
    if ctype:
        req.add_header("Content-Type", ctype)
    tls = ssl.create_default_context()
    if str(a.get("verify_tls", False)).lower() not in ("true", "1"):
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=tls, timeout=15) as r:
            return r.status, r.headers.get("Content-Type", ""), _read(ctx, r)
    except urllib.error.HTTPError as e:
        if raw:
            return e.code, e.headers.get("Content-Type", ""), _read(ctx, e)
        raise ctx.Error(f"PiKVM answered {e.code} {e.reason} to {method} {path}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise ctx.Error(f"PiKVM unreachable: {reason}", retry=True) from None


def _read(ctx, r) -> bytes:
    """The body, at most READ_MAX bytes within READ_SECS: a follow or stream
    query (kvmd's /api/log?follow=1) would read forever."""
    out, deadline = bytearray(), time.monotonic() + READ_SECS
    read = getattr(r, "read1", r.read)        # what has arrived, not a full 64 KiB
    while chunk := read(65536):
        out += chunk
        if len(out) > READ_MAX:
            raise ctx.Error(f"PiKVM's answer is larger than {READ_MAX >> 20} MiB (a stream?)")
        if time.monotonic() > deadline:
            raise ctx.Error(f"PiKVM kept sending for over {READ_SECS} s (a stream?)")
    return bytes(out)


def _json(ctx, body: bytes, what: str):
    try:
        return json.loads(body)
    except ValueError:
        raise ctx.Error(f"PiKVM sent something that isn't JSON for {what}") from None


def _resolution(ctx) -> tuple[int, int]:
    _, _, body = _request(ctx, "GET", "/api/streamer")
    src = ((((_json(ctx, body, "/api/streamer").get("result") or {}).get("streamer") or {})
            .get("source") or {}).get("resolution") or {})
    w, h = src.get("width"), src.get("height")
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0):
        raise ctx.Error("the PiKVM has no video source, so a pixel can't be placed", retry=True)
    return w, h


def _scale(ctx, name: str, v: int, size: int) -> int:
    """A pixel (0..size-1) in kvmd's absolute range (-32768..32767)."""
    if v >= size:
        raise ctx.Error(f"{name}={v} is outside the {size}-pixel screen")
    return round(v * 65535 / max(size - 1, 1)) - 32768


def _path(ctx, path: str) -> str:
    if ".." in path.split("/"):
        raise ctx.Error("path can't contain ..")
    return path


def _query(ctx, query) -> dict | None:
    if query is None:
        return None
    if not isinstance(query, dict) or not all(
            isinstance(v, (str, int, float, bool)) for v in query.values()):
        raise ctx.Error("query is an object of strings, numbers or bools")
    return {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in query.items()}


def _answer(ctx, status: int, ctype: str, body: bytes):
    kind = ctype.split(";")[0].strip().lower()
    if "json" in kind:
        try:
            return {"status": status, "json": json.loads(body)}
        except ValueError:
            pass
    if kind.startswith("text/") or "xml" in kind or "javascript" in kind:
        return {"status": status, "text": body.decode("utf-8", errors="replace")}
    if status >= 300:
        raise ctx.Error(f"PiKVM answered {status} ({kind or 'no content type'})")
    return ctx.file(data=body, name="body", mime=kind or None)
