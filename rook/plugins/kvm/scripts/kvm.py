"""PiKVM over kvmd's REST API, from the hub: each call is one or a few HTTPS
requests over a TLS stream from the appliance's delegate (rook.kit.net),
with the instance's user and password (basic auth) and certificate check.
hid.click at a pixel reads the screen size first; api_get and api_post are
the raw escape hatches (a caller-chosen path and query)."""

from __future__ import annotations

import base64
import json
import re
import urllib.parse

from rook.kit.ctx import KitError
from rook.kit.net import http, values

READ_MAX = 16 << 20        # bytes of one kvmd answer
READ_SECS = 30             # seconds one answer may keep coming
_HOST = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._~-]+)(?::(\d{1,5}))?")
KEYS = {"enter": "Enter", "escape": "Escape", "esc": "Escape", "tab": "Tab", "backspace": "Backspace",
        "space": "Space", "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
        "pageup": "PageUp", "pagedown": "PageDown", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
        "right": "ArrowRight", "printscreen": "PrintScreen", "menu": "ContextMenu",
        **{f"f{i}": f"F{i}" for i in range(1, 13)}, **{c: f"Key{c.upper()}" for c in "abcdefghijklmnopqrstuvwxyz"},
        **{str(d): f"Digit{d}" for d in range(10)}}
MODS = {"ctrl": "ControlLeft", "shift": "ShiftLeft", "alt": "AltLeft", "meta": "MetaLeft"}


# -- caps -------------------------------------------------------------------------

async def screen_capture(ctx, preview: bool = False, quality: int | None = None):
    query = {"save": "0", "load": "0", "preview": _bool(preview), **({"quality": quality} if quality else {})}
    _, ctype, body = await _request(ctx, "GET", "/api/streamer/snapshot", query)
    if body.lstrip().startswith(b"{"):        # 200 + a JSON error when there is no video
        raise ctx.Error(f"failed: {body.decode('utf-8', 'replace').strip()[:500]}")
    return ctx.file(data=body, name="snapshot", mime=ctype.split(";")[0].strip() or "image/jpeg")


async def type_(ctx, text: str, enter: bool = False, slow: bool = False):
    await _ok(ctx, "POST", "/api/hid/print", {"limit": "0", "slow": _bool(slow)},
              (text + ("\n" if enter else "")).encode(), "text/plain; charset=utf-8")


async def key(ctx, key: str, mods=()):
    keys = ",".join(MODS[m] for m in mods) + "," + KEYS.get(key, key)   # kvmd drops the empty item
    await _ok(ctx, "POST", "/api/hid/events/send_shortcut", {"keys": keys})


async def move(ctx, x: int, y: int):
    await _ok(ctx, "POST", "/api/hid/events/send_mouse_move", {"to_x": x, "to_y": y})


async def click(ctx, button: str = "left", x: int | None = None, y: int | None = None):
    if (x is None) != (y is None):
        raise ctx.Error("give x and y together, or neither")
    if x is not None:
        width, height = await _resolution(ctx)
        await _request(ctx, "POST", "/api/hid/events/send_mouse_move",
                       {"to_x": _scale(ctx, "x", x, width), "to_y": _scale(ctx, "y", y, height)})
    # No state: kvmd presses and releases.
    await _request(ctx, "POST", "/api/hid/events/send_mouse_button", {"button": button})
    return {"button": button, "x": x, "y": y}


async def power_on(ctx):
    await _ok(ctx, "POST", "/api/atx/power", {"action": "on"})


async def power_off(ctx, force: bool = False):
    await _ok(ctx, "POST", "/api/atx/power", {"action": "off_hard" if force else "off"})


async def power_reset(ctx):
    await _ok(ctx, "POST", "/api/atx/power", {"action": "reset_hard"})


async def power_status(ctx):
    _, _, body = await _request(ctx, "GET", "/api/atx")
    res = _json(ctx, body, "/api/atx").get("result") or {}
    leds = res.get("leds") or {}
    return {"power": leds.get("power"), "hdd": leds.get("hdd"), "busy": res.get("busy")}


async def api_get(ctx, path: str, query=None):
    return _answer(ctx, *await _request(ctx, "GET", _path(ctx, path), _query(ctx, query), raw=True))


async def api_post(ctx, path: str, query=None, body: str | None = None, body_b64: str | None = None,
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
    return _answer(ctx, *await _request(ctx, "POST", _path(ctx, path), _query(ctx, query), data,
                                        body_type if data is not None else None, raw=True))


# -- helpers ----------------------------------------------------------------------

def _bool(v) -> str:
    return "true" if v else "false"


async def _ok(ctx, method, path, query=None, data=None, ctype=None):
    """A kvmd command: its JSON answer (or None for an empty body)."""
    _, _, body = await _request(ctx, method, path, query, data, ctype)
    return json.loads(body) if body.strip() else None


async def _request(ctx, method: str, path: str, query: dict | None = None, data: bytes | None = None,
                   ctype: str | None = None, *, raw: bool = False):
    """One kvmd request: (status, content type, body). A non-2xx answer is an
    error unless ``raw`` (the escape hatches pass it back); redirects are
    never followed, so the password goes nowhere else."""
    a = values(ctx.appliance)
    m = _HOST.fullmatch(str(a.get("host") or ""))
    if not m:
        raise ctx.Error("kvm: this call has no appliance instance (or its host is not a host name)")
    host, port = m[1].strip("[]"), int(m[2] or 443)
    if query:
        path += "?" + urllib.parse.urlencode(query)
    cred = f"{a.get('user') or 'admin'}:{a.get('password') or ''}".encode()
    headers = {"Authorization": "Basic " + base64.b64encode(cred).decode(), **({"Content-Type": ctype} if ctype else {})}
    try:
        status, got, body = await http(ctx, ctx.worker["id"], method, host, port, path, headers=headers,
                                       body=data or b"", tls=True, verify=bool(a.get("verify_tls")),
                                       timeout=READ_SECS, max_bytes=READ_MAX)
    except KitError as e:
        if "larger than" in e.message:
            raise ctx.Error(f"PiKVM's answer is larger than {READ_MAX >> 20} MiB (a stream?)") from None
        if "didn't finish" in e.message:
            raise ctx.Error(f"PiKVM kept sending for over {READ_SECS} s (a stream?)") from None
        raise ctx.Error(f"PiKVM unreachable: {e.message}", retry=True) from None
    if not raw and not 200 <= status < 300:
        raise ctx.Error(f"PiKVM answered {status} to {method} {path.split('?')[0]}")
    return status, got.get("content-type", ""), body


def _json(ctx, body: bytes, what: str):
    try:
        return json.loads(body)
    except ValueError:
        raise ctx.Error(f"PiKVM sent something that isn't JSON for {what}") from None


async def _resolution(ctx) -> tuple[int, int]:
    _, _, body = await _request(ctx, "GET", "/api/streamer")
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
